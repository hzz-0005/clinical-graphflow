from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from dataclasses import dataclass
from functools import lru_cache
from time import perf_counter
from typing import Any

from pydantic_graph import BaseNode, End, GraphBuilder, GraphRunContext

from app.agent.models import (
    Evidence,
    EvidenceType,
    Hypothesis,
    HypothesisKind,
    HypothesisStatus,
    InvestigationState,
    InvestigationStatus,
    InvestigationStep,
    StepStatus,
)
from app.clinical.analysis_plan import AnalysisTask, InvestigationPlan, PlanAnswer
from app.clinical.coverage import CoverageVerifier
from app.clinical.mcp_gateway import ClinicalMCPGateway, MCPToolResult
from app.clinical.observation import ClinicalObservationInterpreter
from app.clinical.plan_validator import PlanValidator, ValidatedInvestigationPlan
from app.clinical.runtime_llm import ClinicalRuntimeLLM
from app.clinical.runtime_ports import RuntimeEventSink, RuntimeIdempotency, WorkflowCheckpointStore
from app.clinical.operations import OPERATIONS
from app.clinical.telemetry import NoopTelemetry, RuntimeTelemetry
from app.clinical.v17_contracts import (
    CoverageDecision,
    InvestigationGraphState,
    PendingToolExecution,
    TaskExecutionCursor,
    ToolCallRequest,
    ToolObservation,
)


UNATTRIBUTED_SIGNALS = frozenset({"no_data", "insufficient_data", "unattributed"})


class ClinicalInvestigationGraph:
    """Typed, dependency-aware investigation graph used by the V17 runtime.

    The domain node methods remain ordinary and deterministic, while the installed Pydantic Graph
    runner owns the typed transition edges.  This keeps the migration safe and gives every
    transition an explicit event before a future durable runner is enabled.
    """

    def __init__(
        self,
        *,
        planner: ClinicalRuntimeLLM,
        gateway: ClinicalMCPGateway,
        catalog: Mapping[str, Any] | Callable[[str | None, str | None], Mapping[str, Any]] | None = None,
        max_steps: int = 10,
        max_queries: int = 8,
        event_sink: RuntimeEventSink | None = None,
        checkpoint_store: WorkflowCheckpointStore | None = None,
        idempotency_store: RuntimeIdempotency | None = None,
        telemetry: RuntimeTelemetry | None = None,
    ) -> None:
        self.planner = planner
        self.gateway = gateway
        self.catalog_source = catalog
        self.max_steps = max_steps
        self.max_queries = max_queries
        self.interpreter = ClinicalObservationInterpreter()
        self.event_sink = event_sink
        self.checkpoint_store = checkpoint_store
        self.idempotency_store = idempotency_store
        self.telemetry = telemetry or NoopTelemetry()
        # Events are emitted as soon as they are recorded.  Keeping the cursor on the graph
        # instance prevents the finalization step (and a caller that reuses the graph object) from
        # appending the same event more than once.
        self._emitted_event_sequences: set[int] = set()
        self._resuming_terminal = False
        self._active_claims: dict[str, str] = {}
        self._node_started: dict[tuple[str, str], float] = {}

    def run(self, state: InvestigationGraphState) -> InvestigationGraphState:
        """Run the typed graph and return the same state envelope.

        The node implementation is intentionally kept in this module so the domain logic remains
        easy to unit-test.  ``pydantic_graph`` supplies the actual transition runner, type-checks
        each node's return edge, and gives us a stable place to add checkpoint/telemetry adapters
        later without changing the clinical tools.
        """

        self._prepare_state(state)
        state.legacy.audit_metadata["graph_engine"] = "pydantic_graph"
        with self.telemetry.span(
            "clinical_investigation",
            {"provider": state.legacy.provider or "", "runtime": "v17"},
        ):
            try:
                result = _build_pydantic_graph().run_sync(
                    state=state,
                    deps=_RuntimeGraphDeps(runner=self),
                    inputs=_RouteQuestionNode(),
                )
                # A graph node must return the same state envelope; reject an accidental alternate
                # output rather than silently serializing a partial result.
                if not isinstance(result, InvestigationGraphState):
                    raise TypeError("pydantic graph returned an invalid investigation state")
                state = result
            except (ValueError, TypeError) as exc:
                self._record_error(state, state.node, exc)
                self._close_with_gap(state, "error")
            except Exception as exc:
                # Infrastructure failures (database/network/provider outages) must reach the
                # Temporal Activity/worker retry policy.  Only deterministic contract failures
                # are converted into an inconclusive clinical result.
                OPERATIONS.record_integrity("retry", "graph_transient")
                self._record_error(state, state.node, exc)
                self._release_task_claims()
                raise
            finally:
                self._finish(state)
        return state

    def _prepare_state(self, state: InvestigationGraphState) -> None:
        self._resuming_terminal = bool(
            state.legacy.answer
            and state.legacy.report
            and state.legacy.evidence
            and state.legacy.status
            in {
                InvestigationStatus.COMPLETED,
                InvestigationStatus.PENDING_APPROVAL,
                InvestigationStatus.INCONCLUSIVE,
            }
        )
        if not self._resuming_terminal:
            state.legacy.status = InvestigationStatus.RUNNING
        state.legacy.provider = getattr(self.planner, "provider", None)
        state.legacy.model = getattr(self.planner, "model", None)
        state.legacy.external_model_called = state.legacy.provider not in {None, "fake"}
        state.legacy.budget.max_steps = self.max_steps
        state.legacy.budget.max_queries = self.max_queries

    def _route_question(self, state: InvestigationGraphState) -> None:
        self._record_event(state, "route_question", "entered", {"space": state.space})
        state.legacy.audit_metadata["runtime_route"] = {
            "space": state.space,
            "trial_id": state.trial_id,
            "published_batch_id": state.published_batch_id,
        }
        self._record_event(state, "route_question", "completed", {"space": state.space})

    def _load_context(self, state: InvestigationGraphState) -> None:
        self._record_event(state, "load_context", "entered")
        catalog = self._catalog(state)
        descriptors = self.gateway.list_tools()
        state.legacy.audit_metadata["data_catalog"] = catalog
        state.legacy.audit_metadata["available_tools"] = [item.name for item in descriptors]
        state.legacy.audit_metadata["capability_specs"] = [
            {
                "capability": item.capability,
                "capability_aliases": (),
                "description": item.description,
                "operations": item.operations,
                "measures": item.measures,
                "dimensions": item.dimensions,
                "required_domains": item.required_domains,
                "arguments": item.input_schema,
            }
            for item in descriptors
        ]
        self._record_event(
            state,
            "load_context",
            "completed",
            {
                "tool_count": len(descriptors),
                "published_domains": sorted(self.gateway.available_domains),
            },
        )

    def _generate_plan(self, state: InvestigationGraphState) -> InvestigationPlan:
        self._record_event(
            state,
            "generate_plan",
            "entered",
            {"resumed": state.plan is not None},
        )
        if state.plan is not None:
            self._record_event(
                state,
                "generate_plan",
                "completed",
                {"task_ids": [task.task_id for task in state.plan.tasks], "reused": True},
            )
            return state.plan
        specs = tuple(state.legacy.audit_metadata.get("capability_specs", ()))
        catalog = dict(state.legacy.audit_metadata.get("data_catalog", {}))
        catalog.setdefault("published_domains", sorted(self.gateway.available_domains))
        plan = self.planner.plan(state.question, state.trial_id, specs, catalog)
        if not isinstance(plan, InvestigationPlan):
            raise ValueError("planner returned an invalid InvestigationPlan")
        state.plan = plan
        state.legacy.audit_metadata["initial_analysis_plan"] = plan.model_dump(mode="json")
        self._record_event(
            state,
            "generate_plan",
            "completed",
            {"task_ids": [task.task_id for task in plan.tasks]},
        )
        return plan

    def _validate_plan(self, state: InvestigationGraphState) -> ValidatedInvestigationPlan:
        self._record_event(state, "validate_plan", "entered")
        if state.plan is None:
            raise ValueError("an investigation plan is required before validation")
        catalog = dict(state.legacy.audit_metadata.get("data_catalog", {}))
        validated = PlanValidator(self.gateway.registry).validate(
            state.plan,
            set(self.gateway.available_domains),
            catalog=catalog,
        )
        state.legacy.audit_metadata["validated_bindings"] = [
            {"task_id": item.task_id, "tool": item.tool} for item in validated.bindings
        ]
        self._record_event(
            state,
            "validate_plan",
            "completed",
            {"bindings": state.legacy.audit_metadata["validated_bindings"]},
        )
        return validated

    def _select_ready_task(
        self,
        state: InvestigationGraphState,
        validated: ValidatedInvestigationPlan,
    ) -> AnalysisTask | None:
        """Select one dependency-ready task and persist its next typed node.

        Task execution used to live in one while-loop.  The loop made a tool result and its
        interpretation share one retry boundary.  Selection is now a graph node of its own, so a
        checkpoint always identifies the exact task transition that is safe to resume.
        """

        if state.plan is None:
            raise ValueError("an investigation plan is required before execution")

        self._record_event(state, "select_task", "entered")
        completed = set(state.completed_task_ids)
        bindings = {item.task_id: item.tool for item in validated.bindings}
        tasks = {item.task_id: item for item in state.plan.tasks}

        # A checkpoint can be loaded directly into the graph's start node after a worker restart.
        # Never choose a different task while a task cursor or a raw result is still in flight.
        if state.pending_execution is not None or state.task_cursor is not None:
            self._record_event(
                state,
                "select_task",
                "completed",
                {"resumed": True, "task_id": state.active_task_id},
            )
            return None
        if len(completed) >= len(tasks):
            self._record_event(state, "select_task", "completed", {"decision": "all_completed"})
            return None
        if state.legacy.cost.steps >= self.max_steps or state.legacy.cost.queries >= self.max_queries:
            if "调查预算已用尽" not in state.data_gaps:
                state.data_gaps.append("调查预算已用尽")
            self._record_event(state, "select_task", "completed", {"decision": "budget_exhausted"})
            return None
        ready = next(
            (
                item
                for item in state.plan.tasks
                if item.task_id not in completed
                and set(item.depends_on).issubset(completed)
            ),
            None,
        )
        if ready is None:
            if "调查计划存在未满足的任务依赖" not in state.data_gaps:
                state.data_gaps.append("调查计划存在未满足的任务依赖")
            self._record_event(state, "select_task", "completed", {"decision": "dependency_gap"})
            return None
        state.active_task_id = ready.task_id
        state.task_cursor = TaskExecutionCursor(
            task_id=ready.task_id,
            phase="propose_hypothesis",
            tool_name=bindings[ready.task_id],
        )
        self._record_event(
            state,
            "select_task",
            "completed",
            {"task_id": ready.task_id, "tool": bindings[ready.task_id]},
        )
        return ready

    def _propose_hypothesis(self, state: InvestigationGraphState, task: AnalysisTask) -> str:
        self._record_event(state, "propose_hypothesis", "entered", {"task_id": task.task_id})
        statement = task.hypothesis or self._fallback_hypothesis(task)
        hypothesis = state.legacy.add_hypothesis(
            Hypothesis(
                statement=statement,
                kind=HypothesisKind.CLINICAL_EFFICACY,
                priority=0.7,
                rationale=f"由分析任务 {task.task_id} 在查询前提出，等待受治理工具验证",
            )
        )
        state.legacy.start_hypothesis(hypothesis.hypothesis_id)
        state.active_hypothesis_id = hypothesis.hypothesis_id
        cursor = state.task_cursor
        if cursor is None or cursor.task_id != task.task_id:
            raise ValueError("a task cursor is required before proposing a hypothesis")
        state.task_cursor = TaskExecutionCursor(
            task_id=task.task_id,
            phase="execute_task",
            hypothesis_id=str(hypothesis.hypothesis_id),
            tool_name=cursor.tool_name,
        )
        trace = state.legacy.audit_metadata.setdefault("hypothesis_trace", [])
        trace.append(
            {
                "stage": "hypothesis_proposed",
                "task_id": task.task_id,
                "hypothesis_id": hypothesis.hypothesis_id,
                "statement": statement,
            }
        )
        self._record_event(
            state,
            "propose_hypothesis",
            "completed",
            {"task_id": task.task_id, "hypothesis_id": hypothesis.hypothesis_id},
        )
        return str(hypothesis.hypothesis_id)

    def _execute_task(
        self,
        state: InvestigationGraphState,
        task: AnalysisTask,
        tool_name: str,
        hypothesis_id: str,
    ) -> None:
        """Call the governed gateway exactly once and checkpoint the raw result.

        Interpretation is intentionally not performed here.  A process restart after this method
        returns resumes at ``_InterpretObservationNode`` from ``pending_execution`` and therefore
        cannot issue a second gateway call for the same task.
        """

        self._record_event(
            state,
            "execute_task",
            "entered",
            {"task_id": task.task_id, "tool": tool_name, "hypothesis_id": hypothesis_id},
        )
        if state.pending_execution is not None:
            pending = state.pending_execution
            if pending.task_id != task.task_id or pending.hypothesis_id != hypothesis_id:
                raise ValueError("pending tool execution does not match the active task")
            OPERATIONS.record_integrity("duplicate", "tool_execution_reused")
            return
        if self.idempotency_store is not None:
            claim_key = f"{state.investigation_id}:{task.task_id}"
            if not self.idempotency_store.try_claim(
                claim_key,
                owner=f"graph-{id(self)}",
                ttl_seconds=300,
            ):
                OPERATIONS.record_integrity("duplicate", "task_claim_conflict")
                raise RuntimeError("another worker owns the active Graph task claim")
            self._active_claims[claim_key] = f"graph-{id(self)}"
        arguments = self._arguments(state, task, tool_name)
        request = ToolCallRequest(
            task_id=task.task_id,
            hypothesis_id=hypothesis_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        result = self.gateway.call(request)
        state.legacy.record_step()
        state.legacy.record_query(len(result.rows))
        state.pending_execution = PendingToolExecution(
            task_id=task.task_id,
            hypothesis_id=hypothesis_id,
            tool_name=tool_name,
            source=result.source,
            sql=result.sql,
            params=result.params,
            rows=list(result.rows),
            warnings=result.warnings,
            minimum_cell_size=result.minimum_cell_size,
            missing_supporting_domains=result.missing_supporting_domains,
        )
        state.task_cursor = TaskExecutionCursor(
            task_id=task.task_id,
            phase="interpret_observation",
            hypothesis_id=hypothesis_id,
            tool_name=tool_name,
            arguments=arguments,
        )
        self._record_event(
            state,
            "execute_task",
            "completed",
            {"task_id": task.task_id, "rows": len(result.rows)},
        )

    def _interpret_observation(self, state: InvestigationGraphState, task: AnalysisTask) -> str:
        """Turn the checkpointed tool result into typed observation and legacy evidence."""

        pending = state.pending_execution
        cursor = state.task_cursor
        if pending is None or cursor is None:
            raise ValueError("an active pending tool execution is required for interpretation")
        if pending.task_id != task.task_id or cursor.task_id != task.task_id:
            raise ValueError("pending tool execution does not match the active task")
        if cursor.hypothesis_id != pending.hypothesis_id or cursor.tool_name != pending.tool_name:
            raise ValueError("task cursor does not match the pending tool execution")
        self._record_event(
            state,
            "interpret_observation",
            "entered",
            {"task_id": task.task_id, "hypothesis_id": pending.hypothesis_id},
        )

        # If a checkpoint was taken after evidence mutation but before cursor advancement, the
        # task is already interpreted.  Reusing its evidence is what makes interpretation retry
        # idempotent without making the gateway idempotency responsibility leak into this layer.
        existing = state.legacy.audit_metadata.get("graph_task_evidence", {}).get(task.task_id)
        if existing:
            OPERATIONS.record_integrity("duplicate", "interpretation_reused")
            state.pending_execution = None
            state.task_cursor = TaskExecutionCursor(
                task_id=task.task_id,
                phase="advance_task",
                hypothesis_id=pending.hypothesis_id,
                tool_name=pending.tool_name,
                arguments=cursor.arguments,
            )
            self._record_event(
                state,
                "interpret_observation",
                "completed",
                {"task_id": task.task_id, "evidence_id": existing, "reused": True},
            )
            return str(existing)

        result = MCPToolResult(
            tool_name=pending.tool_name,
            source=pending.source,
            sql=pending.sql,
            params=pending.params,
            rows=list(pending.rows),
            warnings=pending.warnings,
            minimum_cell_size=pending.minimum_cell_size,
            missing_supporting_domains=pending.missing_supporting_domains,
        )
        interpreted = self.interpreter.interpret(pending.tool_name, list(result.rows))
        signal = interpreted.get("signal")
        # The legacy interpreter uses ``treatment_effect`` for both a computed delta and a
        # missing delta.  The graph contract must make the latter explicit so it can never be
        # interpreted as supporting evidence.
        if signal == "treatment_effect" and interpreted.get("effect_delta") is None:
            signal = "insufficient_data"
        observation = ToolObservation(
            task_id=task.task_id,
            hypothesis_id=pending.hypothesis_id,
            tool_name=pending.tool_name,
            source=result.source,
            sql=result.sql,
            params=result.params,
            rows=list(result.rows),
            signal=signal,
            summary=interpreted.get("human_summary"),
            warnings=result.warnings,
        )
        state.observations.append(observation)
        state.legacy.observations.append(interpreted)
        quality_flags = list(result.warnings)
        if signal in UNATTRIBUTED_SIGNALS:
            quality_flags.append(signal)
        evidence = state.legacy.add_evidence(
            Evidence(
                claim=interpreted.get("human_summary", f"{pending.tool_name} 返回 {len(result.rows)} 组结果"),
                source=result.source,
                sql=result.sql,
                params=list(result.params),
                rows=list(result.rows),
                sample_size=max(
                    (int(row.get("sample_size", 0) or 0) for row in result.rows),
                    default=None,
                ),
                evidence_type=EvidenceType.CLINICAL_OUTCOME,
                quality_flags=quality_flags,
                observation_signal=signal,
            )
        )
        if signal in UNATTRIBUTED_SIGNALS:
            state.legacy.resolve_hypothesis(
                pending.hypothesis_id,
                HypothesisStatus.INCONCLUSIVE,
                evidence_ids=[str(evidence.evidence_id)],
                rationale="查询结果缺少可解释测量值，不能把缺失当成支持或反驳",
            )
        else:
            evidence.supports.append(pending.hypothesis_id)
            state.legacy.resolve_hypothesis(
                pending.hypothesis_id,
                HypothesisStatus.SUPPORTED,
                evidence_ids=[str(evidence.evidence_id)],
                rationale="受治理工具返回了与该任务对应的可解释观测",
            )
        trace = state.legacy.audit_metadata.setdefault("hypothesis_trace", [])
        trace.extend(
            [
                {
                    "stage": "query_executed",
                    "task_id": task.task_id,
                    "hypothesis_id": pending.hypothesis_id,
                    "tool": pending.tool_name,
                    "signal": signal,
                },
                {
                    "stage": "evidence_recorded",
                    "task_id": task.task_id,
                    "hypothesis_id": pending.hypothesis_id,
                    "evidence_id": evidence.evidence_id,
                    "resolution": "inconclusive" if signal in UNATTRIBUTED_SIGNALS else "supported",
                },
            ]
        )
        state.legacy.audit_metadata.setdefault("graph_task_evidence", {})[task.task_id] = evidence.evidence_id
        step = InvestigationStep(
            sequence=len(state.legacy.steps) + 1,
            tool=pending.tool_name,
            inputs={"task_id": task.task_id, "hypothesis_id": pending.hypothesis_id, **cursor.arguments},
            status=StepStatus.COMPLETED,
            summary=evidence.claim,
            finished_at=datetime.now(timezone.utc),
        )
        state.legacy.steps.append(step)
        state.pending_execution = None
        state.task_cursor = TaskExecutionCursor(
            task_id=task.task_id,
            phase="advance_task",
            hypothesis_id=pending.hypothesis_id,
            tool_name=pending.tool_name,
            arguments=cursor.arguments,
        )
        self._record_event(
            state,
            "interpret_observation",
            "completed",
            {"task_id": task.task_id, "evidence_id": evidence.evidence_id, "signal": signal},
        )
        return str(evidence.evidence_id)

    def _advance_task(self, state: InvestigationGraphState, task: AnalysisTask) -> None:
        """Commit task completion and clear the in-flight cursor before selecting the next task."""

        self._record_event(state, "advance_task", "entered", {"task_id": task.task_id})
        if task.task_id not in state.completed_task_ids:
            completed = set(state.completed_task_ids)
            completed.add(task.task_id)
            state.completed_task_ids = tuple(
                item.task_id for item in (state.plan.tasks if state.plan is not None else ())
                if item.task_id in completed
            )
        state.task_cursor = None
        state.pending_execution = None
        state.active_task_id = None
        state.active_hypothesis_id = None
        self._record_event(
            state,
            "advance_task",
            "completed",
            {"task_id": task.task_id, "completed": len(state.completed_task_ids)},
        )

    def _verify_coverage(self, state: InvestigationGraphState) -> str:
        self._record_event(state, "verify_coverage", "entered")
        if state.plan is None:
            raise ValueError("an investigation plan is required for coverage verification")
        task_evidence = state.legacy.audit_metadata.get("graph_task_evidence", {})
        evidence_by_id = {item.evidence_id: item for item in state.legacy.evidence}
        decisions: list[CoverageDecision] = []
        pending = False
        for requirement in state.plan.answer_requirements:
            task_ids = [task.task_id for task in state.plan.tasks if requirement.requirement_id in task.answers]
            evidence_ids = tuple(
                str(task_evidence[task_id])
                for task_id in task_ids
                if task_id in task_evidence and task_evidence[task_id] in evidence_by_id
            )
            if evidence_ids:
                decisions.append(
                    CoverageDecision(
                        status="complete",
                        requirement_id=requirement.requirement_id,
                        evidence_ids=evidence_ids,
                    )
                )
                continue
            if any(task_id not in state.completed_task_ids for task_id in task_ids):
                pending = True
                decisions.append(
                    CoverageDecision(
                        status="continue",
                        requirement_id=requirement.requirement_id,
                        rationale="仍有任务尚未执行",
                    )
                )
                continue
            gap = "该问题部分没有可引用的已发布数据证据"
            state.data_gaps.append(gap)
            decisions.append(
                CoverageDecision(status="gap", requirement_id=requirement.requirement_id, gap=gap)
            )
        state.coverage = decisions
        if pending:
            decision = "continue"
        elif any(item.status == "gap" for item in decisions):
            decision = "gap"
        else:
            decision = "complete"
        self._record_event(state, "verify_coverage", "completed", {"decision": decision})
        return decision

    def _synthesize_report(self, state: InvestigationGraphState) -> None:
        self._record_event(
            state,
            "synthesize_report",
            "entered",
            {"reused": self._resuming_terminal},
        )
        if self._resuming_terminal:
            self._record_event(state, "synthesize_report", "completed", {"reused": True})
            return
        if state.plan is None:
            raise ValueError("an investigation plan is required for synthesis")
        evidence_payload = tuple(
            {
                "evidence_id": item.evidence_id,
                "task_id": task_id,
                "summary": item.claim,
                "rows": item.rows,
            }
            for task_id, evidence_id in state.legacy.audit_metadata.get("graph_task_evidence", {}).items()
            for item in state.legacy.evidence
            if item.evidence_id == evidence_id
        )
        answer = self.planner.synthesize_plan(state.plan, evidence_payload)
        if not isinstance(answer, PlanAnswer):
            raise ValueError("planner returned an invalid PlanAnswer")
        CoverageVerifier().verify(
            state.plan,
            answer,
            {item.evidence_id for item in state.legacy.evidence if item.evidence_id},
        )
        state.legacy.audit_metadata["requirement_coverage"] = [
            item.model_dump(mode="json") for item in answer.coverage
        ]
        cited = tuple(
            dict.fromkeys(
                evidence_id
                for item in answer.coverage
                for evidence_id in item.evidence_ids
            )
        )
        conclusion = answer.conclusion
        if cited and not any(f"[{item}]" in conclusion for item in cited):
            conclusion += " " + " ".join(f"[{item}]" for item in cited)
        state.legacy.submit_for_approval(conclusion)
        state.legacy.warnings.extend(answer.limitations)
        state.legacy.refresh_report(
            synthesis_mode="external_verified" if state.legacy.external_model_called else "deterministic_fallback",
            key_findings=answer.key_findings,
            limitations=answer.limitations,
            follow_up=answer.follow_up,
            evidence_ids=cited,
        )
        self._record_event(
            state,
            "synthesize_report",
            "completed",
            {"evidence_ids": cited},
        )

    def _close_with_gap(self, state: InvestigationGraphState, reason: str) -> None:
        if state.legacy.status in {
            InvestigationStatus.COMPLETED,
            InvestigationStatus.PENDING_APPROVAL,
            InvestigationStatus.INCONCLUSIVE,
            InvestigationStatus.FAILED,
        }:
            return
        if state.legacy.evidence:
            evidence_id = state.legacy.evidence[0].evidence_id or "E01"
            message = (
                "当前证据不足以覆盖问题的全部要求，系统不会强行给出结论。"
                f"调查状态：{reason}。[{evidence_id}]"
            )
            state.legacy.mark_inconclusive(message)
        else:
            state.legacy.fail(f"调查未产生可引用证据：{reason}")

    def _finish(self, state: InvestigationGraphState) -> None:
        self._record_event(state, "finish", "completed", {"status": state.legacy.status.value})
        state.legacy.audit_metadata["graph_events"] = [
            item.model_dump(mode="json") for item in state.events
        ]
        state.legacy.audit_metadata["runtime_graph_node"] = state.node
        # Final metadata is a state mutation after the terminal event; advance the checkpoint
        # cursor so a monotonic checkpoint store does not confuse it with a conflicting rewrite.
        state.checkpoint_version += 1
        self._save_checkpoint(state)

    def _record_error(self, state: InvestigationGraphState, node: str, exc: Exception) -> None:
        started = self._node_started.pop((state.investigation_id, node), None)
        OPERATIONS.record_graph_node(
            node,
            "failure",
            (perf_counter() - started) * 1000 if started is not None else 0,
        )
        state.legacy.audit_metadata.setdefault("graph_errors", []).append(
            {"node": node, "type": type(exc).__name__}
        )
        self._record_event(
            state,
            "finish",
            "error",
            {"node": node, "error_type": type(exc).__name__},
        )

    @staticmethod
    def _sanitize_event_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """Keep runtime events useful without copying question text, SQL, or result rows.

        Graph events are coordination telemetry, not an evidence channel.  The complete governed
        result remains in the protected investigation record; event sinks receive only identifiers,
        counts and small operational decisions.  Nested mappings are filtered recursively so a
        future node cannot accidentally smuggle a patient row through a new payload key.
        """

        # Events are an operational allowlist, not a clinical payload with a denylist.  Unknown
        # keys are dropped so a future node cannot accidentally emit patient identifiers or raw
        # tool arguments under a new field name.
        allowed = frozenset(
            {
                "space", "resumed", "tool_count", "published_domains", "task_ids", "bindings",
                "task_id", "hypothesis_id", "tool", "evidence_id", "signal", "decision", "status",
                "completed", "error_type", "node", "rows_returned", "claim_count", "version",
            }
        )
        sensitive = frozenset({"question", "query", "sql", "params", "rows", "observation", "observations", "evidence", "arguments", "error", "summary"})
        redacted = False

        def clean(value: Any, key: str | None = None) -> Any:
            nonlocal redacted
            if key is not None and key.lower() in sensitive:
                redacted = True
                return None
            if key is not None and key.lower() not in allowed:
                redacted = True
                return None
            if isinstance(value, Mapping):
                return {
                    str(child_key): cleaned
                    for child_key, child_value in value.items()
                    if (cleaned := clean(child_value, str(child_key))) is not None
                }
            if isinstance(value, (list, tuple, set, frozenset)):
                cleaned_items = []
                for item in value:
                    cleaned_item = clean(item)
                    if cleaned_item is not None:
                        cleaned_items.append(cleaned_item)
                return cleaned_items
            if isinstance(value, str):
                return value[:200]
            if isinstance(value, (int, float, bool)) or value is None:
                return value
            return str(value)[:200]

        cleaned = clean(dict(payload or {}))
        if redacted:
            OPERATIONS.record_integrity("privacy", "event_payload_redacted")
        return cleaned if isinstance(cleaned, dict) else {}

    def _record_event(
        self,
        state: InvestigationGraphState,
        node: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        metric_key = (state.investigation_id, node)
        if event_type == "entered":
            self._node_started[metric_key] = perf_counter()
        event = state.record_event(node, event_type, self._sanitize_event_payload(payload))
        if event_type in {"completed", "error"}:
            started = self._node_started.pop(metric_key, None)
            OPERATIONS.record_graph_node(
                node,
                "failure" if event_type == "error" else "success",
                (perf_counter() - started) * 1000 if started is not None else 0,
            )
        if self.event_sink is not None and event.sequence not in self._emitted_event_sequences:
            self.event_sink.emit(event)
            self._emitted_event_sequences.add(event.sequence)
        self._save_checkpoint(state)

    def _save_checkpoint(self, state: InvestigationGraphState) -> None:
        if self.checkpoint_store is not None:
            self.checkpoint_store.save(state)

    def _release_task_claims(self) -> None:
        if self.idempotency_store is None:
            return
        release = getattr(self.idempotency_store, "release", None)
        if not callable(release):
            return
        for key, owner in self._active_claims.items():
            release(key, owner=owner)
        self._active_claims.clear()

    def _catalog(self, state: InvestigationGraphState) -> dict[str, Any]:
        if self.catalog_source is None:
            return {"published_domains": sorted(self.gateway.available_domains)}
        if callable(self.catalog_source):
            return dict(self.catalog_source(state.trial_id, state.published_batch_id))
        return dict(self.catalog_source)

    def _arguments(self, state: InvestigationGraphState, task: AnalysisTask, tool_name: str) -> dict[str, Any]:
        plugin = self.gateway.registry.plugin(tool_name)
        schema = plugin.argument_model.model_json_schema() if plugin.argument_model else {"properties": {}}
        allowed = set(schema.get("properties", {}))
        arguments: dict[str, Any] = {}
        if "trial_id" in allowed and state.trial_id is not None:
            arguments["trial_id"] = state.trial_id
        if "group_by" in allowed:
            group_schema = schema.get("properties", {}).get("group_by", {})
            choices = tuple(group_schema.get("enum", ()))
            if "treatment_arm" in choices and "treatment_arm" in task.dimensions:
                arguments["group_by"] = "treatment_arm"
            elif choices and {"site", "region"}.issubset(choices):
                # ``rank_sites`` and protocol-quality tools call the center level ``site``;
                # ``site_id`` is the semantic dimension exposed by the catalog.  Translate only
                # at the governed argument boundary instead of sending an invalid enum value.
                arguments["group_by"] = "region" if "region" in task.dimensions else "site"
            elif choices:
                arguments["group_by"] = next(
                    (dimension for dimension in choices if dimension in task.dimensions),
                    choices[0],
                )
        if "query" in allowed:
            # Metric discovery is the one governed tool whose required input is free text rather
            # than the trial id.  The question itself is the user-owned search term; falling back
            # to a capability name would make every unrelated question return the same metrics.
            arguments["query"] = task.measure or state.question
        if "measure" in allowed and task.measure:
            arguments["measure"] = task.measure
        arguments.update({key: value for key, value in task.filters.items() if key in allowed})
        return arguments

    @staticmethod
    def _fallback_hypothesis(task: AnalysisTask) -> str:
        measure = task.measure or task.capability
        dimensions = "、".join(task.dimensions) if task.dimensions else "总体"
        return f"核对 {dimensions} 的 {measure} 是否存在可解释差异"


@dataclass(frozen=True)
class _RuntimeGraphDeps:
    runner: ClinicalInvestigationGraph


class _RouteQuestionNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _LoadContextNode:
        ctx.deps.runner._route_question(ctx.state)
        return _LoadContextNode()


class _LoadContextNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _GeneratePlanNode:
        ctx.deps.runner._load_context(ctx.state)
        return _GeneratePlanNode()


class _GeneratePlanNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _ValidatePlanNode:
        ctx.deps.runner._generate_plan(ctx.state)
        return _ValidatePlanNode()


class _ValidatePlanNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _SelectTaskNode:
        validated = ctx.deps.runner._validate_plan(ctx.state)
        return _SelectTaskNode(validated)


class _SelectTaskNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    def __init__(self, validated: ValidatedInvestigationPlan) -> None:
        self.validated = validated

    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> (
        _ProposeHypothesisNode
        | _ExecuteTaskNode
        | _InterpretObservationNode
        | _AdvanceTaskNode
        | _VerifyCoverageNode
    ):
        state = ctx.state
        runner = ctx.deps.runner
        if state.plan is None:
            raise ValueError("an investigation plan is required before task selection")

        # A pending result always wins over the cursor's phase.  This makes an old checkpoint
        # taken between gateway return and cursor persistence safe to resume without a duplicate
        # call; the identity checks in the next node still fail closed on a corrupted payload.
        if state.pending_execution is not None:
            return _InterpretObservationNode(state.pending_execution.task_id, self.validated)

        cursor = state.task_cursor
        if cursor is not None:
            if cursor.phase == "propose_hypothesis":
                return _ProposeHypothesisNode(cursor.task_id, self.validated)
            if cursor.phase == "execute_task":
                return _ExecuteTaskNode(cursor.task_id, self.validated)
            if cursor.phase == "interpret_observation":
                return _InterpretObservationNode(cursor.task_id, self.validated)
            if cursor.phase == "advance_task":
                return _AdvanceTaskNode(cursor.task_id, self.validated)
            raise ValueError(f"unsupported task cursor phase: {cursor.phase}")

        task = runner._select_ready_task(state, self.validated)
        if task is None:
            return _VerifyCoverageNode()
        return _ProposeHypothesisNode(task.task_id, self.validated)


class _ProposeHypothesisNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    def __init__(self, task_id: str, validated: ValidatedInvestigationPlan) -> None:
        self.task_id = task_id
        self.validated = validated

    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _ExecuteTaskNode:
        state = ctx.state
        if state.plan is None:
            raise ValueError("an investigation plan is required before hypothesis proposal")
        task = next((item for item in state.plan.tasks if item.task_id == self.task_id), None)
        if task is None:
            raise ValueError(f"task {self.task_id} is not present in the investigation plan")
        cursor = state.task_cursor
        if cursor is None or cursor.task_id != self.task_id:
            raise ValueError("a matching task cursor is required before hypothesis proposal")
        if cursor.phase == "execute_task" and cursor.hypothesis_id:
            # A retry after the proposal checkpoint can safely skip the mutation.
            return _ExecuteTaskNode(self.task_id, self.validated)
        if cursor.phase != "propose_hypothesis":
            raise ValueError(f"task {self.task_id} is not ready for hypothesis proposal")
        ctx.deps.runner._propose_hypothesis(state, task)
        return _ExecuteTaskNode(self.task_id, self.validated)


class _ExecuteTaskNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    def __init__(self, task_id: str, validated: ValidatedInvestigationPlan) -> None:
        self.task_id = task_id
        self.validated = validated

    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _InterpretObservationNode:
        state = ctx.state
        if state.plan is None:
            raise ValueError("an investigation plan is required before task execution")
        task = next((item for item in state.plan.tasks if item.task_id == self.task_id), None)
        if task is None:
            raise ValueError(f"task {self.task_id} is not present in the investigation plan")
        cursor = state.task_cursor
        if (
            cursor is None
            or cursor.task_id != self.task_id
            or cursor.phase not in {"execute_task", "interpret_observation"}
            or not cursor.hypothesis_id
            or not cursor.tool_name
        ):
            raise ValueError("a complete task cursor is required before task execution")
        # An interpretation-phase cursor is only resumable when the checkpoint also contains
        # the gateway result.  Calling the gateway again with a missing pending result would turn
        # a corrupted/partial checkpoint into a duplicate clinical query; fail closed instead.
        if cursor.phase == "interpret_observation" and state.pending_execution is None:
            raise ValueError("interpretation cursor is missing its pending tool execution")
        ctx.deps.runner._execute_task(state, task, cursor.tool_name, cursor.hypothesis_id)
        return _InterpretObservationNode(self.task_id, self.validated)


class _InterpretObservationNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    def __init__(self, task_id: str, validated: ValidatedInvestigationPlan) -> None:
        self.task_id = task_id
        self.validated = validated

    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _AdvanceTaskNode:
        state = ctx.state
        if state.plan is None:
            raise ValueError("an investigation plan is required before observation interpretation")
        task = next((item for item in state.plan.tasks if item.task_id == self.task_id), None)
        if task is None:
            raise ValueError(f"task {self.task_id} is not present in the investigation plan")
        ctx.deps.runner._interpret_observation(state, task)
        return _AdvanceTaskNode(self.task_id, self.validated)


class _AdvanceTaskNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    def __init__(self, task_id: str, validated: ValidatedInvestigationPlan) -> None:
        self.task_id = task_id
        self.validated = validated

    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _SelectTaskNode:
        state = ctx.state
        if state.plan is None:
            raise ValueError("an investigation plan is required before task advancement")
        task = next((item for item in state.plan.tasks if item.task_id == self.task_id), None)
        if task is None:
            raise ValueError(f"task {self.task_id} is not present in the investigation plan")
        ctx.deps.runner._advance_task(state, task)
        return _SelectTaskNode(self.validated)


class _VerifyCoverageNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _SynthesizeReportNode | _CloseWithGapNode:
        decision = ctx.deps.runner._verify_coverage(ctx.state)
        if decision == "complete":
            return _SynthesizeReportNode()
        return _CloseWithGapNode(decision)


class _SynthesizeReportNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _FinishNode:
        ctx.deps.runner._synthesize_report(ctx.state)
        return _FinishNode()


class _CloseWithGapNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> _FinishNode:
        ctx.deps.runner._close_with_gap(ctx.state, self.reason)
        return _FinishNode()


class _FinishNode(
    BaseNode[InvestigationGraphState, _RuntimeGraphDeps, InvestigationGraphState]
):
    async def run(
        self,
        ctx: GraphRunContext[InvestigationGraphState, _RuntimeGraphDeps],
    ) -> End[InvestigationGraphState]:
        return End(ctx.state)


@lru_cache(maxsize=1)
def _build_pydantic_graph():
    """Build the executable Pydantic Graph once; node instances carry only transition data."""

    builder = GraphBuilder(
        name="insightflow_clinical_investigation",
        state_type=InvestigationGraphState,
        deps_type=_RuntimeGraphDeps,
        input_type=_RouteQuestionNode,
        output_type=InvestigationGraphState,
    )
    builder.add(builder.edge_from(builder.start_node).to(_RouteQuestionNode))
    builder.add(builder.node(_RouteQuestionNode))
    builder.add(builder.node(_LoadContextNode))
    builder.add(builder.node(_GeneratePlanNode))
    builder.add(builder.node(_ValidatePlanNode))
    builder.add(builder.node(_SelectTaskNode))
    builder.add(builder.node(_ProposeHypothesisNode))
    builder.add(builder.node(_ExecuteTaskNode))
    builder.add(builder.node(_InterpretObservationNode))
    builder.add(builder.node(_AdvanceTaskNode))
    builder.add(builder.node(_VerifyCoverageNode))
    builder.add(builder.node(_SynthesizeReportNode))
    builder.add(builder.node(_CloseWithGapNode))
    builder.add(builder.node(_FinishNode))
    return builder.build()

