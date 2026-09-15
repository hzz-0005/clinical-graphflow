from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from app.clinical.observation import ClinicalObservationInterpreter
from app.clinical.plan_validator import TaskBinding, ValidatedInvestigationPlan
from app.clinical.registry import ClinicalToolRegistry


@dataclass(frozen=True)
class TaskExecution:
    task_id: str
    tool: str
    answers: tuple[str, ...]
    source: str
    rows: tuple[dict[str, Any], ...]
    observation: dict[str, Any]
    gap: str | None = None
    # When two plan tasks resolve to the same governed query, keep every task id so callers can
    # resolve each task's hypothesis instead of silently leaving a duplicate in ``testing``.
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanExecutionResult:
    executions: tuple[TaskExecution, ...]
    coverage: dict[str, tuple[str, ...]]


def coalesce_executions(executions: tuple[TaskExecution, ...]) -> tuple[TaskExecution, ...]:
    """Collapse identical governed result packets while preserving answer coverage."""

    def ids(item: TaskExecution) -> tuple[str, ...]:
        return item.task_ids or (item.task_id,)

    merged: list[TaskExecution] = []
    indexes: dict[tuple[str, str], int] = {}
    for execution in executions:
        key = (execution.source, json.dumps(execution.rows, ensure_ascii=False, sort_keys=True, default=str))
        if key not in indexes:
            indexes[key] = len(merged)
            merged.append(
                TaskExecution(
                    task_id=execution.task_id,
                    tool=execution.tool,
                    answers=execution.answers,
                    source=execution.source,
                    rows=execution.rows,
                    observation=execution.observation,
                    gap=execution.gap,
                    task_ids=ids(execution),
                )
            )
            continue
        index = indexes[key]
        prior = merged[index]
        merged[index] = TaskExecution(
            task_id=prior.task_id,
            tool=prior.tool,
            answers=tuple(dict.fromkeys((*prior.answers, *execution.answers))),
            source=prior.source,
            rows=prior.rows,
            observation=prior.observation,
            gap=prior.gap,
            task_ids=tuple(dict.fromkeys((*ids(prior), *ids(execution)))),
        )
    return tuple(merged)


class PlanExecutor:
    """Run a validated DAG only through the governed registry."""

    def __init__(self, registry: ClinicalToolRegistry) -> None:
        self.registry = registry
        self.interpreter = ClinicalObservationInterpreter()
        self._query_cache: dict[tuple[str, str, str], TaskExecution] = {}

    def execute(self, validated: ValidatedInvestigationPlan) -> PlanExecutionResult:
        tasks = {task.task_id: task for task in validated.plan.tasks}
        bindings = {binding.task_id: binding for binding in validated.bindings}
        pending = set(tasks)
        completed: set[str] = set()
        executions: list[TaskExecution] = []
        execution_indexes: dict[tuple[str, str, str], int] = {}

        while pending:
            ready = [task for task in validated.plan.tasks if task.task_id in pending and set(task.depends_on).issubset(completed)]
            if not ready:
                raise ValueError("analysis plan contains a dependency cycle")
            for task in ready:
                binding = bindings[task.task_id]
                arguments = self._arguments(validated, task.task_id)
                signature = (binding.tool, task.measure or "", json.dumps(arguments, ensure_ascii=False, sort_keys=True))
                if signature in execution_indexes:
                    index = execution_indexes[signature]
                    prior = executions[index]
                    executions[index] = TaskExecution(
                        task_id=prior.task_id,
                        tool=prior.tool,
                        answers=tuple(dict.fromkeys((*prior.answers, *task.answers))),
                        source=prior.source,
                        rows=prior.rows,
                        observation=prior.observation,
                        gap=prior.gap,
                        task_ids=tuple(dict.fromkeys((*(prior.task_ids or (prior.task_id,)), task.task_id))),
                    )
                else:
                    execution_indexes[signature] = len(executions)
                    executions.append(self.execute_task(validated,task.task_id))
                pending.remove(task.task_id)
                completed.add(task.task_id)

        coverage = {
            requirement.requirement_id: tuple(
                execution.task_id for execution in executions if requirement.requirement_id in execution.answers
            )
            for requirement in validated.plan.answer_requirements
        }
        return PlanExecutionResult(executions=tuple(executions), coverage=coverage)

    def execute_task(self,validated:ValidatedInvestigationPlan,task_id:str)->TaskExecution:
        task=next(task for task in validated.plan.tasks if task.task_id==task_id)
        binding:TaskBinding=next(binding for binding in validated.bindings if binding.task_id==task_id)
        arguments=self._arguments(validated,task_id)
        cache_key=(binding.tool,task.measure or "",json.dumps(arguments,ensure_ascii=False,sort_keys=True,default=str))
        if cache_key in self._query_cache:
            prior=self._query_cache[cache_key]
            return TaskExecution(task_id=task.task_id,tool=prior.tool,answers=task.answers,source=prior.source,rows=prior.rows,observation=prior.observation,gap=prior.gap,task_ids=(task.task_id,))
        try:
            result=self.registry.invoke(binding.tool,arguments)
            rows=tuple(result.rows)
            observation=self.interpreter.interpret(binding.tool,list(rows),measure=task.measure)
            if isinstance(observation.get("baseline_delta"),float):
                observation["baseline_delta"]=round(observation["baseline_delta"],2)
            gap=observation.get("signal") if observation.get("signal") in {"no_data","insufficient_data","unattributed"} else None
            execution=TaskExecution(task_id=task.task_id,tool=binding.tool,answers=task.answers,source=result.source,rows=rows,observation=observation,gap=gap,task_ids=(task.task_id,))
            self._query_cache[cache_key]=execution
            return execution
        except Exception as exc:
            return TaskExecution(task_id=task.task_id,tool=binding.tool,answers=task.answers,source="tool_execution",rows=(),observation={"tool":binding.tool,"signal":"execution_error","human_summary":str(exc)},gap="execution_error",task_ids=(task.task_id,))

    def _arguments(self, validated: ValidatedInvestigationPlan, task_id: str) -> dict[str, Any]:
        task=next(task for task in validated.plan.tasks if task.task_id==task_id)
        binding:TaskBinding=next(binding for binding in validated.bindings if binding.task_id==task_id)
        plugin=self.registry.plugin(binding.tool)
        schema=plugin.argument_model.model_json_schema() if plugin.argument_model else {"properties":{}}
        allowed=set(schema.get("properties",{}))
        arguments:dict[str,Any]={}
        if "trial_id" in allowed and validated.plan.trial_id is not None:
            arguments["trial_id"]=validated.plan.trial_id
        if "group_by" in allowed:
            # The request schema stores the allowed granularity in the enum of
            # ``group_by``; it is not a top-level property named ``site``.
            # Providers may use the catalog dimension name ``site_id`` while
            # the adapter contract intentionally calls that choice ``site``.
            group_by_enum = set(
                schema.get("properties", {}).get("group_by", {}).get("enum", ())
            )
            if "site" in group_by_enum:
                arguments["group_by"] = "region" if "region" in task.dimensions else "site"
            else:
                supported_dimensions = ("region", "site_id", "age_band", "sex", "severity_band")
                arguments["group_by"] = next(
                    (dimension for dimension in supported_dimensions if dimension in task.dimensions),
                    "treatment_arm" if "treatment_arm" in allowed else "region",
                )
        if "measure" in allowed and task.measure:
            arguments["measure"] = task.measure
        # ``group_by`` is a semantic dimension, not a free-form adapter value.
        # Providers often echo ``site_id`` from the catalog even though the
        # governed request schema uses ``site``.  Keep the normalized mapping
        # above authoritative and never let a raw filter overwrite it.
        arguments.update({key:value for key,value in task.filters.items() if key in allowed and key != "group_by"})
        return arguments

