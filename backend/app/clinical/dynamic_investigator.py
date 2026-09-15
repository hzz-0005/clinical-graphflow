from __future__ import annotations

import json
from datetime import datetime, timezone

from app.agent.models import Evidence, EvidenceType, Hypothesis, HypothesisKind, HypothesisStatus, InvestigationState, InvestigationStatus, InvestigationStep, StepStatus
from app.clinical.registry import ClinicalToolRegistry
from app.clinical.runtime_llm import ClinicalRuntimeLLM, FakeClinicalRuntimeLLM, classify_question
from app.clinical.runtime_models import CallToolAction, RuntimeContext
from app.clinical.observation import ClinicalObservationInterpreter
from app.clinical.coverage import CoverageVerifier
from app.clinical.analysis_plan import AnalysisTask, AnswerRequirement, InvestigationPlan, apply_revision
from app.clinical.plan_executor import PlanExecutionResult, PlanExecutor, coalesce_executions
from app.clinical.plan_validator import DIMENSION_ALIASES, PlanValidator
from app.clinical.question_compiler import ClinicalQuestionCompiler
from app.clinical.catalog import (
    CatalogSnapshot,
    ClinicalDataCatalog,
    SEMANTIC_DIMENSION_FIELDS,
    SEMANTIC_MEASURE_FIELDS,
    semantic_candidates,
)


KIND={"efficacy":HypothesisKind.CLINICAL_EFFICACY,"safety":HypothesisKind.CLINICAL_SAFETY,"exposure":HypothesisKind.CLINICAL_SITE_QUALITY,"site_quality":HypothesisKind.CLINICAL_SITE_QUALITY,"data_quality":HypothesisKind.CLINICAL_DATA_QUALITY,"general":HypothesisKind.TEMPORAL_EFFECT}
ETYPE={"compare_treatment_effect":EvidenceType.CLINICAL_OUTCOME,"run_sensitivity_analysis":EvidenceType.CLINICAL_OUTCOME,"analyze_safety_trend":EvidenceType.CLINICAL_OUTCOME,"rank_sites":EvidenceType.CLINICAL_SITE,"inspect_data_quality":EvidenceType.CLINICAL_MISSINGNESS,"inspect_trial":EvidenceType.BASELINE,"check_randomization_balance":EvidenceType.BASELINE,"inspect_treatment_exposure":EvidenceType.CLINICAL_EXPOSURE}

# A structured observation only moves a hypothesis through an explicit, auditable mapping: the
# signal names the fact, the flag says whether that fact supports or refutes it. Nothing here lets
# the model assert a causal claim on its own.
SUPPORT_FLAG={"site_ranking":"supports_site_cause","exposure":"supports_site_cause","protocol_quality":"supports_site_cause","missingness":"supports_data_quality_cause","visit_windows":"supports_data_quality_cause","safety_trend":"supports_safety_signal"}
REFUTABLE_SIGNALS=frozenset({"exposure","protocol_quality","missingness","visit_windows","safety_trend"})
# Signals that carry no directional statement: they say "we could not measure this", so they may
# never be booked as support or refutation.
UNATTRIBUTED_SIGNALS=frozenset({"no_data","insufficient_data","unattributed"})
# A site-execution fact must not be filed against the data-quality hypothesis (and vice versa).
SIGNAL_TARGET_KIND={"site_ranking":HypothesisKind.CLINICAL_SITE_QUALITY,"exposure":HypothesisKind.CLINICAL_SITE_QUALITY,"protocol_quality":HypothesisKind.CLINICAL_SITE_QUALITY,"missingness":HypothesisKind.CLINICAL_DATA_QUALITY,"visit_windows":HypothesisKind.CLINICAL_DATA_QUALITY,"safety_trend":HypothesisKind.CLINICAL_SAFETY}

class DynamicClinicalInvestigator:
    def __init__(self,registry:ClinicalToolRegistry,llm:ClinicalRuntimeLLM,available_domains:set[str],max_steps:int=10,max_queries:int=8,data_catalog:ClinicalDataCatalog|None=None):
        self.registry,self.llm,self.available_domains=registry,llm,set(available_domains); self.max_steps=max_steps; self.max_queries=max_queries; self.interpreter=ClinicalObservationInterpreter(); self.data_catalog=data_catalog; self._last_catalog_error: str|None=None

    def investigate(self,question:str,trial_id:str,published_batch_id:str|None=None)->InvestigationState:
        compiled=ClinicalQuestionCompiler().compile(question,trial_id)
        brief=classify_question(question,trial_id)
        state=InvestigationState(question=question,domain="clinical_trial",status=InvestigationStatus.RUNNING,provider=self.llm.provider,model=self.llm.model,external_model_called=self.llm.provider!="fake")
        state.audit_metadata["published_batch_id"] = published_batch_id
        state.audit_metadata["question_plan"] = compiled.model_dump(mode="json")
        state.budget.max_steps=self.max_steps; state.budget.max_queries=self.max_queries
        if brief.intent=="unsupported":
            state.add_evidence(Evidence(claim="该请求属于患者级诊疗或剂量建议，超出研究分析边界",source="clinical_governance_policy",sql="",rows=[],evidence_type=EvidenceType.ALTERNATIVE,quality_flags=["medical_advice_blocked"]))
            state.mark_inconclusive("该请求涉及患者级医疗建议，InsightFlow 仅提供研究数据调查，不能给出诊疗或剂量建议。[E01]")
            return state
        catalog_snapshot=self._discover_catalog(trial_id, published_batch_id)
        if catalog_snapshot is not None:
            state.audit_metadata["data_catalog"] = catalog_snapshot.model_dump(mode="json")
        elif self._last_catalog_error:
            state.audit_metadata["data_catalog_error"] = self._last_catalog_error
            state.warnings.append(f"真实数据目录探测失败，已退回注册能力目录：{self._last_catalog_error}")
        if self.llm.provider != "fake" and hasattr(self.llm, "plan") and hasattr(self.llm, "synthesize_plan"):
            return self._investigate_structured(question, trial_id, state, catalog_snapshot, compiled)
        direct_site_ranking=brief.metric=="treatment_effect" and "site_id" in brief.requested_dimensions
        direct_quality_review=compiled.intent=="efficacy" and compiled.operation=="compare" and compiled.quality_requested
        direct_exposure_review=compiled.intent=="exposure"
        direct_safety_summary_review=compiled.intent=="safety" and compiled.operation=="compare"
        direct_balance_review=compiled.metric=="baseline_balance"
        statements={
            "efficacy":"治疗组与对照组的 Week-12 疗效差异可能与中心执行、治疗暴露或结局缺失有关",
            "safety":"治疗组的安全性事件发生率可能存在可核对的时间或组间变化",
            "exposure":"各地区的治疗暴露依从性可能存在可核对的差异",
            "site_quality":"至少一个研究中心可能存在可核对的执行质量异常",
            "data_quality":"结局缺失或访视窗口偏离可能影响当前分析结果",
            "general":"当前问题可能由已发布临床数据中的可核对聚合信号解释",
        }
        statement=("治疗组与对照组治疗前基线可能存在影响结果解释的差异" if direct_balance_review else "至少一个研究中心具有可计算且低于其他中心的 Week-12 治疗效应" if direct_site_ranking else statements[brief.intent])
        hypothesis=state.add_hypothesis(Hypothesis(statement=statement,kind=KIND[brief.intent],priority=.8))
        state.start_hypothesis(hypothesis.hypothesis_id); active_hypothesis_id=hypothesis.hypothesis_id
        hypothesis_trace=[
            {
                "stage":"hypothesis_proposed",
                "hypothesis_id":hypothesis.hypothesis_id,
                "statement":hypothesis.statement,
            }
        ]
        state.audit_metadata["hypothesis_trace"] = hypothesis_trace
        eligible=self.registry.available(brief.intent,self.available_domains)
        candidate_order={name:index for index,name in enumerate(compiled.candidate_tools)}
        tools=tuple(sorted((tool for tool in eligible if tool.name in candidate_order),key=lambda tool:candidate_order[tool.name]))
        available=tuple(x.name for x in tools)
        quality_review_tools=tuple(name for name in ("compare_treatment_effect","analyze_missingness","analyze_visit_windows","inspect_data_quality") if name in available)
        if not available:
            state.add_evidence(Evidence(claim="当前发布数据域不足以回答该问题",source="domain_registry",sql="",rows=[],evidence_type=EvidenceType.ALTERNATIVE,quality_flags=["missing_domain"]))
            self._close_testing_inconclusive(state,"当前数据域不足")
            state.mark_inconclusive("当前数据域不足，无法完成受治理调查。[E01]"); return state
        signatures=set(); prior=[]
        while state.cost.steps<self.max_steps and state.cost.queries<self.max_queries:
            if direct_quality_review and set(quality_review_tools).issubset(set(prior)):
                return self._finish_efficacy_quality_review(state,quality_review_tools)
            context=RuntimeContext(brief=brief,available_tools=available,tool_specs=tuple({"name":item.name,"description":item.description,"arguments":item.argument_model.model_json_schema() if item.argument_model else {"type":"object","properties":{}}} for item in tools),hypotheses=tuple(f"{h.hypothesis_id}: {h.statement} ({h.status})" for h in state.hypotheses),evidence_summaries=tuple(f"{e.evidence_id}: {e.claim}" for e in state.evidence),observations=tuple(state.observations),prior_actions=tuple(prior),remaining_steps=self.max_steps-state.cost.steps,remaining_queries=self.max_queries-state.cost.queries,data_gaps=tuple(f"缺少 {x} 数据域" for x in self.registry.unavailable_domains(self.available_domains)),data_catalog=self._planner_catalog(catalog_snapshot, self.registry.capability_specs(self.available_domains)))
            try: decision=self.llm.decide(context)
            except Exception as exc:
                self._close_testing_inconclusive(state,"模型决策失败")
                if state.evidence: state.mark_inconclusive(f"模型决策失败，调查保留现有证据：[E01]。原因：{exc}")
                else: state.fail(f"模型决策失败：{exc}")
                return state
            action=decision.action
            # A direct ranking question has a deterministic analytical target. The model may
            # explain why the governed action is useful, but it may not turn the request into an
            # open-ended root-cause tour. This guard also makes provider behavior reproducible.
            if direct_balance_review and "check_randomization_balance" not in prior:
                action=CallToolAction(
                    tool="check_randomization_balance",
                    arguments={"trial_id":trial_id},
                    rationale="比较随机化组治疗前基线分布",
                    hypothesis_id=active_hypothesis_id,
                )
            elif direct_site_ranking and "profile_sites" not in prior:
                action=CallToolAction(
                    tool="profile_sites",
                    arguments={"trial_id": trial_id},
                    rationale="按研究中心和治疗臂计算 Week-12 疗效差并排序",
                    hypothesis_id=active_hypothesis_id,
                )
            elif direct_quality_review and any(name not in prior for name in quality_review_tools):
                next_tool=next(name for name in quality_review_tools if name not in prior)
                action=CallToolAction(
                    tool=next_tool,
                    arguments={"trial_id":trial_id},
                    rationale="依次回答疗效差异并核查可能影响解释的数据质量因素",
                    hypothesis_id=active_hypothesis_id,
                )
            elif direct_exposure_review and "inspect_treatment_exposure" not in prior and "inspect_treatment_exposure" in available:
                action=CallToolAction(
                    tool="inspect_treatment_exposure",
                    arguments={"trial_id": trial_id, "group_by": "region"},
                    rationale="按地区比较实际剂量、计划剂量和漏服情况",
                    hypothesis_id=active_hypothesis_id,
                )
            elif direct_safety_summary_review and "inspect_safety_summary" not in prior and "inspect_safety_summary" in available:
                action=CallToolAction(
                    tool="inspect_safety_summary",
                    arguments={"trial_id": trial_id},
                    rationale="按治疗组比较试验级安全性事件比例并核对严重事件",
                    hypothesis_id=active_hypothesis_id,
                )
            elif self.llm.provider!="fake":
                core_tool={"efficacy":"compare_treatment_effect","safety":"analyze_safety_trend","exposure":"inspect_treatment_exposure","data_quality":"inspect_data_quality"}.get(brief.intent)
                if core_tool and core_tool in available and core_tool not in prior:
                    action=CallToolAction(
                        tool=core_tool,
                        arguments={"trial_id":trial_id},
                        rationale="先验证问题所述核心信号是否真实存在",
                        hypothesis_id=active_hypothesis_id,
                    )
                repeated=action.type=="call_tool" and action.tool in prior
                premature_finish=action.type=="finish" and not any(e.supports or e.contradicts for e in state.evidence)
                if (repeated or premature_finish) and (not core_tool or core_tool in prior):
                    # External providers are advisory planners. When they repeat a completed tool
                    # or try to conclude from neutral context only, advance with the deterministic
                    # governed policy instead of burning the budget or accepting an empty answer.
                    fallback=FakeClinicalRuntimeLLM().decide(context).action
                    if fallback.type=="finish" or fallback.tool not in prior:
                        action=fallback
            if action.type=="finish":
                cited=[x for x in action.evidence_ids if x in {e.evidence_id for e in state.evidence}]
                if not cited:
                    self._close_testing_inconclusive(state,"模型未引用有效证据")
                    state.mark_inconclusive("没有足够且可引用的证据，调查无法形成结论。[E01]" if state.evidence else "没有可用证据") if state.evidence else state.fail("模型在无证据时结束")
                    return state
                resolved=self._resolve_testing_hypotheses(state)
                supported=any(status is HypothesisStatus.SUPPORTED for status in resolved)
                if not supported or action.inconclusive:
                    # A conclusion may only be submitted for approval when at least one piece of
                    # evidence supports a hypothesis without unresolved counter-evidence.
                    reason="现有证据未形成无反证支持的假设；因此不能把混合证据升级为确定结论"
                    state.warnings.extend(action.limitations)
                    state.mark_inconclusive(f"{reason}，因此不作结论并保留现有证据。 " + " ".join(f"[{x}]" for x in cited[:1] if cited))
                    return state
                conclusion=action.conclusion
                if not any(f"[{x}]" in conclusion for x in cited): conclusion += " " + " ".join(f"[{x}]" for x in cited)
                state.submit_for_approval(conclusion); state.warnings.extend(action.limitations); return state
            signature=action.tool+":"+json.dumps(action.arguments,sort_keys=True,ensure_ascii=False)
            if signature in signatures:
                self._close_testing_inconclusive(state,"重复动作未产生新信息")
                state.mark_inconclusive("检测到重复调查动作，为避免循环已停止；现有证据不足以支持最终结论。[E01]"); return state
            signatures.add(signature); prior.append(action.tool); state.record_step(); state.record_query(0)
            hypothesis_trace.append({"stage":"query_executed","hypothesis_id":active_hypothesis_id,"tool":action.tool})
            step=InvestigationStep(sequence=len(state.steps)+1,tool=action.tool,inputs=action.arguments,status=StepStatus.RUNNING); state.steps.append(step)
            try: result=self.registry.invoke(action.tool,action.arguments)
            except Exception as exc:
                step.status=StepStatus.FAILED; step.summary=str(exc); step.finished_at=datetime.now(timezone.utc); state.warnings.append(f"工具 {action.tool} 执行失败：{exc}"); continue
            state.cost.returned_rows+=len(result.rows)
            descriptor=self.registry.plugin(action.tool)
            degraded=descriptor.missing_domains(self.available_domains)
            degraded_flags=[]
            if degraded:
                degraded_flags=[f"degraded_missing_domain:{domain}" for domain in sorted(degraded)]
                state.warnings.append(f"{descriptor.description}依赖 {'、'.join(sorted(degraded))} 支撑数据域，当前发布批次未提供，只能给出受限结论")
            observation=self.interpreter.interpret(action.tool,result.rows,measure=compiled.metric)
            state.observations.append(observation)
            if observation.get("focus_site_id") and not any(h.kind is HypothesisKind.CLINICAL_SITE_QUALITY for h in state.hypotheses):
                site_hypothesis=state.add_hypothesis(Hypothesis(statement=f"中心 {observation['focus_site_id']} 的执行质量可能解释当前差异",kind=HypothesisKind.CLINICAL_SITE_QUALITY,priority=.7))
                state.start_hypothesis(site_hypothesis.hypothesis_id); active_hypothesis_id=site_hypothesis.hypothesis_id
            meaning=f"{descriptor.description}：{observation.get('human_summary', f'返回 {len(result.rows)} 组聚合结果')}"
            evidence=state.add_evidence(Evidence(claim=meaning,source=result.source,sql=result.sql or "",params=list(result.params),rows=result.rows,sample_size=max((int(r.get("sample_size",0) or 0) for r in result.rows),default=None),evidence_type=ETYPE.get(action.tool,EvidenceType.ALTERNATIVE),quality_flags=[*result.warnings,*degraded_flags],observation_signal=observation.get("signal")))
            signal=observation.get("signal")
            target_kind=SIGNAL_TARGET_KIND.get(signal)
            if target_kind is not None:
                # Only a hypothesis of the *matching* kind may be moved by this fact, otherwise a
                # site-execution finding would be booked against a data-quality hypothesis.
                target=next((h for h in reversed(state.hypotheses) if h.kind is target_kind),None)
            else:
                target=next((h for h in state.hypotheses if h.hypothesis_id==active_hypothesis_id),None)
            if target is not None and signal not in UNATTRIBUTED_SIGNALS:
                flag=SUPPORT_FLAG.get(signal)
                if flag and observation.get(flag) is True:
                    evidence.supports.append(target.hypothesis_id or "")
                if signal in REFUTABLE_SIGNALS and flag and observation.get(flag) is False:
                    evidence.contradicts.append(target.hypothesis_id or "")
                if signal=="treatment_effect" and (observation.get("effect_delta") or 0)<0:
                    evidence.supports.append(target.hypothesis_id or "")
                if signal=="treatment_effect" and observation.get("effect_delta") is not None and observation["effect_delta"]>=0:
                    evidence.contradicts.append(target.hypothesis_id or "")
            if signal in UNATTRIBUTED_SIGNALS:
                # Absence of data — whether the query returned nothing at all or every cell was
                # withheld by small-sample suppression — is an unknown, not a refutation. It is
                # recorded as an explicit limitation so the conclusion can never treat
                # "not measured" as "measured and negative".
                evidence.quality_flags.append("no_data_unattributed")
            hypothesis_trace.append({
                "stage":"evidence_recorded",
                "hypothesis_id":active_hypothesis_id,
                "evidence_id":evidence.evidence_id,
                "resolution":(
                    "supported" if active_hypothesis_id in evidence.supports
                    else "rejected" if active_hypothesis_id in evidence.contradicts
                    else "inconclusive" if signal in UNATTRIBUTED_SIGNALS
                    else "observed"
                ),
            })
            step.status=StepStatus.COMPLETED; step.summary=f"形成证据 {evidence.evidence_id}：{meaning}"; step.finished_at=datetime.now(timezone.utc)
            if direct_balance_review and action.tool=="check_randomization_balance":
                return self._finish_baseline_balance(state,evidence,observation)
            if direct_exposure_review and action.tool == "inspect_treatment_exposure":
                return self._finish_exposure_review(state, evidence, observation)
            if direct_safety_summary_review and action.tool == "inspect_safety_summary":
                return self._finish_safety_summary_review(state, evidence, observation)
            if brief.intent=="efficacy" and not direct_site_ranking and not direct_quality_review and compiled.decline_premise and action.tool=="compare_treatment_effect":
                delta=observation.get("effect_delta")
                if delta is None:
                    self._close_testing_inconclusive(state,"治疗组或对照组聚合值不可用，无法验证疗效下降前提")
                    state.mark_inconclusive(
                        "当前数据无法验证题目所述的 Week-12 疗效下降：治疗组或对照组缺少满足最小样本量要求的聚合值。"
                        f"因此不继续进行根因下钻。[{evidence.evidence_id}]"
                    )
                    return state
                if delta>=0:
                    self._resolve_testing_hypotheses(state)
                    state.submit_for_approval(
                        "当前发布数据没有观察到题目所述的疗效下降："
                        f"治疗组相对对照组的 Week-12 平均改善差为 {delta:+.2f} 分。"
                        f"问题前提未成立，因此不继续搜索所谓根因。[{evidence.evidence_id}]"
                    )
                    return state
            if direct_site_ranking and action.tool=="profile_sites":
                if signal in UNATTRIBUTED_SIGNALS or not observation.get("focus_site_id"):
                    self._close_testing_inconclusive(state,"中心疗效单元格受最小样本量保护，无法完成排名")
                    state.mark_inconclusive(
                        "当前发布数据无法判断哪个研究中心的 Week-12 治疗效果最弱："
                        "所有可用的中心 × 治疗臂单元格均低于 10 例，疗效聚合值已被抑制。"
                        f"请增加合规样本量或选择覆盖更完整的数据版本。[{evidence.evidence_id}]"
                    )
                    return state
                self._resolve_testing_hypotheses(state)
                focus=observation["focus_site_id"]
                delta=observation.get("focus_site_effect_delta")
                state.submit_for_approval(
                    f"在当前具有可计算聚合值的研究中心中，{focus} 的 Week-12 治疗组相对对照组效应差最低，"
                    f"为 {delta:.2f} 分；这是中心层面的描述性比较，不代表该中心导致疗效变化。[{evidence.evidence_id}]"
                )
                return state
        self._close_testing_inconclusive(state,"达到调查预算")
        state.mark_inconclusive("调查达到预算上限，保留现有证据但不强行给出结论。[E01]")
        return state

    def _discover_catalog(self, trial_id: str, published_batch_id: str | None) -> CatalogSnapshot | None:
        if self.data_catalog is None:
            return None
        self._last_catalog_error=None
        try:
            snapshot=self.data_catalog.snapshot(trial_id=trial_id,published_batch_id=published_batch_id)
        except Exception as exc:
            # A metadata probe must never make the governed investigation fail.
            # The error is recorded by the caller as an audit warning; planning
            # falls back to the registered capability catalog.
            self._last_catalog_error=str(exc)
            return None
        allowed=self.available_domains
        if not allowed:
            return snapshot
        datasets=tuple(item for item in snapshot.datasets if item.domain.upper() in allowed)
        observed_measures=tuple(sorted({field for item in datasets for field in item.measures}))
        observed_dimensions=tuple(sorted({field for item in datasets for field in item.dimensions}))
        return snapshot.model_copy(update={
            "datasets":datasets,
            "published_domains":tuple(sorted({item.domain for item in datasets})),
            "measures":observed_measures,
            "dimensions":observed_dimensions,
            "data_gaps":tuple(item for item in snapshot.data_gaps if any(dataset.domain in item for dataset in datasets)),
        })

    @staticmethod
    def _planner_catalog(snapshot: CatalogSnapshot|None, specs: tuple[dict, ...]) -> dict:
        static_measures=tuple(sorted({measure for item in specs for measure in item["measures"]}))
        static_dimensions=tuple(sorted({dimension for item in specs for dimension in item["dimensions"]}))
        if snapshot is None or not snapshot.datasets:
            return {"measures":list(static_measures),"dimensions":list(static_dimensions),"published_domains":[]}
        # Some governed measures are derived from status fields rather than a
        # numeric column (for example ``missing_rate`` from the boolean
        # ``week12_missing`` field).  Use the union for capability discovery,
        # while still exposing raw measures and dimensions separately below.
        observed_fields=tuple(sorted(set(snapshot.measures)|set(snapshot.dimensions)))
        measures=semantic_candidates(observed_fields,static_measures,SEMANTIC_MEASURE_FIELDS)
        dimensions=semantic_candidates(snapshot.dimensions,static_dimensions,SEMANTIC_DIMENSION_FIELDS)
        # If a semantic alias has no explicit mapping, keep it only when the
        # exact field was observed. This avoids promising an absent dimension.
        return {
            "measures":list(measures),
            "dimensions":list(dimensions),
            "observed_fields":list(observed_fields),
            "datasets":[item.model_dump(mode="json") for item in snapshot.datasets],
            "published_domains":list(snapshot.published_domains),
            "data_gaps":list(snapshot.data_gaps),
        }

    def _investigate_structured(self,question:str,trial_id:str,state:InvestigationState,catalog_snapshot:CatalogSnapshot|None=None,compiled=None)->InvestigationState:
        """Execute an LLM-authored plan after resolving every task to a governed capability."""

        compiled = compiled or ClinicalQuestionCompiler().compile(question, trial_id)
        specs=self.registry.capability_specs(self.available_domains)
        # Concrete adapter names are intentionally hidden from the model. It plans with reusable
        # analytical capabilities; only the validator is allowed to bind those to tools.
        planning_specs=tuple({key:value for key,value in item.items() if key!="tool"} for item in specs)
        catalog=self._planner_catalog(catalog_snapshot,specs)
        catalog["published_domains"]=sorted(self.available_domains)
        catalog["question_contract"]={
            "intent": compiled.intent,
            "operation": compiled.operation,
            "metric": compiled.metric,
            "dimensions": list(compiled.dimensions),
            "duration_requested": compiled.duration_requested,
            "primary_capability": (
                "assess_exposure" if compiled.intent == "exposure"
                else "summarize_safety" if compiled.intent == "safety" and compiled.operation == "compare"
                else None
            ),
            "candidate_tools": list(compiled.candidate_tools),
        }
        try:
            plan=self.llm.plan(question,trial_id,planning_specs,catalog)
            self._validate_question_contract(plan, compiled)
            validated=PlanValidator(self.registry).validate(plan,self.available_domains,catalog=catalog)
        except Exception as exc:
            state.audit_metadata["planning_error"]=str(exc)
            repaired=self._repair_plan(compiled,question,trial_id,catalog)
            if repaired is None:
                evidence=state.add_evidence(Evidence(claim=f"调查计划无法映射到当前已发布数据能力：{exc}",source="capability_catalog",sql="",rows=[],evidence_type=EvidenceType.ALTERNATIVE,quality_flags=["plan_validation_failed"]))
                state.mark_inconclusive(f"系统理解了问题，但生成的分析计划无法由当前数据和工具安全执行；具体缺口：{exc}。[{evidence.evidence_id}]")
                return state
            try:
                plan=repaired
                validated=PlanValidator(self.registry).validate(plan,self.available_domains,catalog=catalog)
                state.audit_metadata["plan_repair"]={
                    "reason": (
                        "baseline_balance_semantic_contract"
                        if compiled.metric == "baseline_balance"
                        else "sensitivity_semantic_contract"
                        if compiled.intent == "efficacy" and compiled.operation == "sensitivity"
                        else "visit_window_semantic_contract"
                        if compiled.metric == "visit_window_deviation"
                        else "exposure_semantic_contract"
                        if compiled.intent == "exposure"
                        else "site_population_semantic_contract"
                        if compiled.intent == "site_quality" and compiled.metric == "site_population"
                        else "protocol_quality_semantic_contract"
                        if compiled.intent == "site_quality" and compiled.metric == "site_quality_burden"
                        else "safety_summary_semantic_contract"
                    ),
                    "original_error":str(exc),
                    "replacement_plan":plan.model_dump(mode="json"),
                }
                state.warnings.append(
                    "模型计划与问题语义不一致，已按受治理语义契约改为"
                    + (
                        "随机化基线与疾病持续时间比较"
                        if compiled.metric == "baseline_balance"
                        else "疗效敏感性分析与受试者质量过滤"
                        if compiled.intent == "efficacy" and compiled.operation == "sensitivity"
                        else "访视窗口偏离分布比较"
                        if compiled.metric == "visit_window_deviation"
                        else "治疗暴露比较"
                        if compiled.intent == "exposure"
                        else "研究中心样本与治疗臂构成比较"
                        if compiled.intent == "site_quality" and compiled.metric == "site_population"
                        else "研究中心/地区方案质量分布比较"
                        if compiled.intent == "site_quality" and compiled.metric == "site_quality_burden"
                        else "治疗组安全性比例比较"
                    )
                    + "；原计划未执行"
                )
            except Exception as repair_exc:
                evidence=state.add_evidence(Evidence(claim=f"调查计划无法映射到当前已发布数据能力：{repair_exc}",source="capability_catalog",sql="",rows=[],evidence_type=EvidenceType.ALTERNATIVE,quality_flags=["plan_validation_failed"]))
                state.mark_inconclusive(f"系统理解了问题，但生成的分析计划无法由当前数据和工具安全执行；具体缺口：{repair_exc}。[{evidence.evidence_id}]")
                return state

        state.audit_metadata["initial_analysis_plan"]=plan.model_dump(mode="json")
        current_plan=plan
        completed:set[str]=set()
        executions=[]
        revisions=[]
        task_hypotheses:dict[str,str]={}
        hypothesis_trace:list[dict[str,object]]=[]
        # This trace is intentionally part of the returned audit metadata.  It lets a reviewer
        # distinguish a provider plan from a query result and verify that every executed task had
        # a proposed hypothesis before its governed tool was called.
        state.audit_metadata["hypothesis_trace"] = hypothesis_trace
        executor=PlanExecutor(self.registry)
        while len(completed)<len(current_plan.tasks) and len(executions)<self.max_queries:
            validated=PlanValidator(self.registry).validate(current_plan,self.available_domains,catalog=catalog)
            ready=next((task for task in current_plan.tasks if task.task_id not in completed and set(task.depends_on).issubset(completed)),None)
            if ready is None:
                break
            hypothesis = state.add_hypothesis(
                Hypothesis(
                    statement=self._task_hypothesis(compiled, ready),
                    kind=KIND.get(compiled.intent, HypothesisKind.TEMPORAL_EFFECT),
                    priority=.7,
                    rationale=f"由分析计划 {ready.task_id} 在查询前提出，等待受治理工具验证",
                )
            )
            state.start_hypothesis(hypothesis.hypothesis_id)
            task_hypotheses[ready.task_id] = str(hypothesis.hypothesis_id)
            hypothesis_trace.append(
                {
                    "stage": "hypothesis_proposed",
                    "task_id": ready.task_id,
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "statement": hypothesis.statement,
                }
            )
            execution=executor.execute_task(validated,ready.task_id)
            executions.append(execution);completed.add(ready.task_id)
            hypothesis_trace.append(
                {
                    "stage": "query_executed",
                    "task_id": ready.task_id,
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "tool": execution.tool,
                    "signal": execution.observation.get("signal"),
                }
            )
            if hasattr(self.llm,"revise_plan") and len(completed)<len(current_plan.tasks):
                try:
                    revision=self.llm.revise_plan(current_plan,tuple(task.task_id for task in current_plan.tasks if task.task_id in completed),tuple(item.observation for item in executions),planning_specs,self.max_queries-len(executions))
                    revisions.append(revision.model_dump(mode="json"))
                    if revision.action=="finish":
                        unfinished = sorted(
                            task.task_id
                            for task in current_plan.tasks
                            if task.task_id not in completed
                        )
                        if unfinished:
                            reason = "计划中仍有未执行的必需任务：" + ",".join(unfinished)
                            revisions.append({"action": "rejected", "rationale": reason})
                            state.warnings.append(
                                f"模型请求提前结束调查，但计划任务尚未全部执行，继续执行：{reason}"
                            )
                            continue
                        covered = {
                            requirement_id
                            for task in current_plan.tasks
                            if task.task_id in completed
                            for requirement_id in task.answers
                        }
                        required = {
                            requirement.requirement_id
                            for requirement in current_plan.answer_requirements
                        }
                        missing = sorted(required - covered)
                        if missing:
                            reason = "仍有问题部分尚未取得证据：" + ",".join(missing)
                            revisions.append({"action": "rejected", "rationale": reason})
                            state.warnings.append(
                                f"模型请求提前结束调查，但未通过答案覆盖校验，继续执行计划：{reason}"
                            )
                        else:
                            break
                    if revision.action=="replace_remaining":
                        candidate=apply_revision(current_plan,completed,revision)
                        # A replacement is allowed to change only the future branch, but it must
                        # still satisfy the deterministic question contract.  Otherwise a model can
                        # legally replace an unexecuted required ranking task with a convenient
                        # summary task and make the original contract appear covered.
                        self._validate_question_contract(candidate, compiled)
                        PlanValidator(self.registry).validate(candidate,self.available_domains,catalog=catalog)
                        current_plan=candidate
                except Exception as exc:
                    revisions.append({"action":"rejected","rationale":str(exc)})
                    state.warnings.append(f"模型计划修订未通过治理校验，继续使用上一个有效计划：{exc}")
        plan=current_plan
        state.audit_metadata["analysis_plan"]=plan.model_dump(mode="json")
        state.audit_metadata["plan_revisions"]=revisions
        executions=list(coalesce_executions(tuple(executions)))
        result=PlanExecutionResult(executions=tuple(executions),coverage={requirement.requirement_id:tuple(execution.task_id for execution in executions if requirement.requirement_id in execution.answers) for requirement in plan.answer_requirements})
        state.audit_metadata["answer_coverage"]={key:list(value) for key,value in result.coverage.items()}
        synthesis_evidence=[]
        tasks={task.task_id:task for task in plan.tasks}
        for execution in result.executions:
            task=tasks[execution.task_id]
            execution_task_ids=execution.task_ids or (execution.task_id,)
            hypothesis_ids: list[str]=[]
            for task_id in execution_task_ids:
                task_for_hypothesis=tasks.get(task_id,task)
                hypothesis_id=task_hypotheses.get(task_id)
                # A cached/coalesced execution can refer to a task whose proposal was not retained
                # by an older provider/runtime.  Create the fallback immediately before resolution,
                # while keeping the normal path (proposal before query) explicit and auditable.
                if hypothesis_id is None:
                    hypothesis = state.add_hypothesis(
                        Hypothesis(
                            statement=self._task_hypothesis(compiled, task_for_hypothesis),
                            kind=KIND.get(compiled.intent, HypothesisKind.TEMPORAL_EFFECT),
                            priority=.7,
                            rationale=f"为执行结果 {task_id} 补建待验证命题",
                        )
                    )
                    state.start_hypothesis(hypothesis.hypothesis_id)
                    hypothesis_id = str(hypothesis.hypothesis_id)
                    task_hypotheses[task_id] = hypothesis_id
                    hypothesis_trace.append(
                        {
                            "stage": "hypothesis_proposed_fallback",
                            "task_id": task_id,
                            "hypothesis_id": hypothesis_id,
                            "statement": hypothesis.statement,
                        }
                    )
                hypothesis_ids.append(hypothesis_id)
            hypothesis_id=hypothesis_ids[0]
            state.record_step(); state.record_query(len(execution.rows))
            step=InvestigationStep(sequence=len(state.steps)+1,tool=execution.tool,inputs={"trial_id":trial_id,"hypothesis_id":hypothesis_id,"hypothesis_ids":hypothesis_ids,**task.filters},status=StepStatus.COMPLETED,summary=execution.observation.get("human_summary"),finished_at=datetime.now(timezone.utc))
            state.steps.append(step)
            state.observations.append(execution.observation)
            evidence=state.add_evidence(Evidence(claim=execution.observation.get("human_summary",f"{execution.tool} 返回 {len(execution.rows)} 组结果"),source=execution.source,sql="",rows=list(execution.rows),sample_size=max((int(row.get("sample_size",0) or 0) for row in execution.rows),default=None),evidence_type=ETYPE.get(execution.tool,EvidenceType.ALTERNATIVE),quality_flags=[execution.gap] if execution.gap else [],observation_signal=execution.observation.get("signal")))
            if execution.gap:
                for item in hypothesis_ids:
                    state.resolve_hypothesis(item,HypothesisStatus.INCONCLUSIVE,evidence_ids=[evidence.evidence_id],rationale=f"分析任务存在数据缺口：{execution.gap}")
            else:
                evidence.supports.extend(hypothesis_ids)
                for item in hypothesis_ids:
                    state.resolve_hypothesis(item,HypothesisStatus.SUPPORTED,evidence_ids=[evidence.evidence_id],rationale="受治理工具返回了可解释观测")
            for task_id,item in zip(execution_task_ids,hypothesis_ids):
                hypothesis_trace.append(
                    {
                        "stage": "evidence_recorded",
                        "task_id": task_id,
                        "hypothesis_id": item,
                        "evidence_id": evidence.evidence_id,
                        "resolution": "inconclusive" if execution.gap else "supported",
                    }
                )
            synthesis_evidence.append({"evidence_id":evidence.evidence_id,"task_id":task.task_id,"answers":execution.answers,"summary":evidence.claim,"rows":evidence.rows,"gap":execution.gap})

        try:
            answer=self.llm.synthesize_plan(plan,tuple(synthesis_evidence))
            answer = self._ensure_answer_language(plan, answer)
            CoverageVerifier().verify(plan,answer,{item.evidence_id for item in state.evidence if item.evidence_id})
            state.warnings.extend(answer.limitations)
            state.audit_metadata["requirement_coverage"]=[item.model_dump(mode="json") for item in answer.coverage]
            cited=tuple(dict.fromkeys(evidence_id for item in answer.coverage for evidence_id in item.evidence_ids))
            conclusion=answer.conclusion
            if cited and not any(f"[{evidence_id}]" in conclusion for evidence_id in cited):
                conclusion += " " + " ".join(f"[{evidence_id}]" for evidence_id in cited)
            state.submit_for_approval(conclusion)
            state.refresh_report(
                synthesis_mode="external_verified",
                key_findings=answer.key_findings,
                limitations=answer.limitations,
                follow_up=answer.follow_up,
                evidence_ids=cited,
            )
        except Exception as exc:
            state.audit_metadata["synthesis_error"]=str(exc)
            state.mark_inconclusive(f"分析任务已执行，但模型生成的答案未通过逐项覆盖或证据引用校验：{exc}。[E01]")
        return state

    @staticmethod
    def _ensure_answer_language(plan: InvestigationPlan, answer):
        """Keep the final prose visibly aligned with the user's requested parts.

        Evidence coverage is structural, but a provider can satisfy it while using a
        vague synonym that hides an important requested boundary (for example, saying
        ``不能据此`` without ever labelling the requested ``限制``).  This small
        presentation guard does not add facts or citations: it reuses the provider's
        own limitations and only adds a labelled sentence when the plan explicitly
        asked for one.
        """

        conclusion = answer.conclusion
        requested_parts = " ".join(item.question_part for item in plan.answer_requirements)
        if "限制" in requested_parts and "限制" not in conclusion:
            limitations = "；".join(answer.limitations)
            suffix = limitations or "当前证据为描述性比较，不能单独证明因果关系。"
            conclusion = f"{conclusion} 限制：{suffix}"
        if any(token in requested_parts for token in ("月度", "按月", "每月")) and not any(
            token in conclusion for token in ("月度", "按月", "每月", "月份")
        ):
            conclusion = f"{conclusion} 月度口径：当前结论按月份聚合结果解释。"
        if any(task.capability == "sensitivity_analysis" for task in plan.tasks) and not any(
            token in conclusion for token in ("敏感性", "敏感分析")
        ):
            conclusion = f"{conclusion} 敏感性分析：已按计划的替代人群规则重新计算，结果见上述证据。"
        # A model may already say "无法" while omitting the user's requested
        # object (the missing *fields*).  Check the noun itself, not only an
        # impossibility synonym, so every required part remains visible.
        if "缺少" in requested_parts and "字段" not in conclusion:
            conclusion = (
                f"{conclusion} 字段限制：缺少受试者级方案偏离标识、治疗臂或可关联的结局字段时，不能重新计算该疗效。"
            )
        if any(term in requested_parts for term in ("因果", "causal")) and "因果" not in conclusion:
            conclusion = (
                f"{conclusion} 因果边界：当前证据只能做描述性关联，不能据此证明质量负担导致疗效差异。"
            )
        if conclusion == answer.conclusion:
            return answer
        return answer.model_copy(update={"conclusion": conclusion})

    @staticmethod
    def _task_hypothesis(compiled, task: AnalysisTask) -> str:
        """Return the provider's pre-query hypothesis, with a safe legacy fallback."""

        if task.hypothesis:
            return task.hypothesis
        measure = task.measure or task.capability
        dimensions = "、".join(task.dimensions)
        operation = {
            "discover": "是否存在可用的",
            "describe": "的实际情况是否符合问题描述：",
            "compare": "是否存在可解释的组间差异：",
            "rank": "是否存在可核查的分层差异：",
            "trend": "是否存在可核查的时间变化：",
            "stratify": "是否存在可核查的亚组差异：",
            "correlate": "是否存在可核查的关联：",
            "sensitivity": "在替代分析下是否仍保持：",
            "quality_check": "是否存在会影响解释的数据质量问题：",
        }.get(task.operation, "是否存在可核查的信号：")
        prefix = f"{dimensions} 的" if dimensions else "当前数据的"
        return f"核对{prefix}{measure}{operation}"

    @staticmethod
    def _validate_question_contract(plan: InvestigationPlan, compiled) -> None:
        """Reject a semantically wrong but structurally valid provider plan."""

        if compiled.metric == "baseline_balance":
            allowed_capabilities = {"compare_group_measure", "describe_population", "discover_metric"}
            unrelated = sorted(
                {task.capability for task in plan.tasks if task.capability not in allowed_capabilities}
            )
            if unrelated:
                raise ValueError(
                    "question_contract_violation: baseline balance question received unrelated capabilities: "
                    + ",".join(unrelated)
                )
            balance_tasks = [
                task for task in plan.tasks
                if task.capability == "compare_group_measure"
                and "treatment_arm" in {
                    DIMENSION_ALIASES.get(dimension, dimension)
                    for dimension in task.dimensions
                }
            ]
            required_measures = {task.measure for task in balance_tasks}
            if "baseline_score" not in required_measures:
                raise ValueError(
                    "question_contract_violation: baseline balance questions require baseline_score by treatment_arm"
                )
            if compiled.duration_requested and "disease_duration_months" not in required_measures:
                raise ValueError(
                    "question_contract_violation: the question requests disease duration; plan must compare disease_duration_months by treatment_arm"
                )
        elif compiled.intent == "efficacy" and compiled.operation == "sensitivity":
            allowed_capabilities = {
                "compare_group_measure",
                "assess_protocol_quality",
                "rank_groups",
                "sensitivity_analysis",
                "describe_population",
                "discover_metric",
            }
            unrelated = sorted(
                {task.capability for task in plan.tasks if task.capability not in allowed_capabilities}
            )
            if unrelated:
                raise ValueError(
                    "question_contract_violation: sensitivity question received unrelated capabilities: "
                    + ",".join(unrelated)
                )
            effect_valid = any(
                task.capability == "compare_group_measure"
                and task.measure in {"treatment_effect", "week_12_improvement"}
                and "treatment_arm" in {
                    DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions
                }
                for task in plan.tasks
            )
            sensitivity_valid = any(
                task.capability == "sensitivity_analysis"
                and task.measure in {"treatment_effect", "week_12_improvement"}
                and {DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions}
                >= {"analysis_method", "treatment_arm"}
                for task in plan.tasks
            )
            if not effect_valid:
                raise ValueError(
                    "question_contract_violation: sensitivity questions require a treatment-arm effect comparison"
                )
            if not sensitivity_valid:
                raise ValueError(
                    "question_contract_violation: sensitivity questions require a governed sensitivity_analysis task"
                )
            if compiled.quality_burden_requested and not any(
                task.capability == "rank_groups"
                and task.measure == "site_quality_burden"
                and "site_id"
                in {DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions}
                for task in plan.tasks
            ):
                raise ValueError(
                    "question_contract_violation: quality-burden exclusion requires a governed site ranking"
                )
            if compiled.protocol_filter_requested and not any(
                task.capability == "assess_protocol_quality"
                and task.measure == "protocol_deviation"
                and "site_id"
                in {DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions}
                for task in plan.tasks
            ):
                raise ValueError(
                    "question_contract_violation: protocol-filtered sensitivity requires participant quality evidence"
                )
        elif compiled.intent == "exposure":
            requested_scope = "site_id" if "site_id" in compiled.dimensions else "region"
            valid = any(
                task.capability == "assess_exposure"
                and task.measure == "adherence_rate"
                and requested_scope
                in {DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions}
                for task in plan.tasks
            )
            if not valid:
                raise ValueError(
                    f"question_contract_violation: exposure questions require assess_exposure/adherence_rate by {requested_scope}"
                )
        elif compiled.intent == "site_quality" and compiled.metric == "site_population":
            allowed_capabilities = {"stratify_measure", "describe_population", "discover_metric"}
            unrelated = sorted(
                {task.capability for task in plan.tasks if task.capability not in allowed_capabilities}
            )
            if unrelated:
                raise ValueError(
                    "question_contract_violation: site population question received unrelated capabilities: "
                    + ",".join(unrelated)
                )
            if not any(
                task.capability == "describe_population" and task.operation == "describe"
                for task in plan.tasks
            ):
                raise ValueError(
                    "question_contract_violation: site population questions require a governed trial overview"
                )
            valid = any(
                task.capability == "stratify_measure"
                and task.measure == "site_population"
                and {
                    DIMENSION_ALIASES.get(dimension, dimension)
                    for dimension in task.dimensions
                }
                >= {"site_id", "treatment_arm"}
                for task in plan.tasks
            )
            if not valid:
                raise ValueError(
                    "question_contract_violation: site population questions require profile_sites/site_population by site and treatment_arm"
                )
        elif compiled.metric == "visit_window_deviation":
            allowed_capabilities = {"assess_visit_window", "describe_population", "discover_metric"}
            unrelated = sorted(
                {task.capability for task in plan.tasks if task.capability not in allowed_capabilities}
            )
            if unrelated:
                raise ValueError(
                    "question_contract_violation: visit-window questions received unrelated capabilities: "
                    + ",".join(unrelated)
                )
            if not any(
                task.capability in {"assess_visit_window", "visit_window_deviation"}
                and task.measure == "visit_window_deviation"
                and {DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions}
                >= {"visit", "treatment_arm"}
                for task in plan.tasks
            ):
                raise ValueError(
                    "question_contract_violation: visit-window questions require visit and treatment_arm breakdowns"
                )
        elif compiled.intent == "site_quality" and compiled.metric == "site_quality_burden":
            allowed_capabilities = {
                "assess_protocol_quality",
                "rank_groups",
                "describe_population",
                "discover_metric",
            }
            unrelated = sorted(
                {
                    task.capability
                    for task in plan.tasks
                    if task.capability not in allowed_capabilities
                }
            )
            if unrelated:
                raise ValueError(
                    "question_contract_violation: protocol quality question received unrelated capabilities: "
                    + ",".join(unrelated)
                )
            protocol_tasks = [
                task for task in plan.tasks
                if task.capability == "assess_protocol_quality"
            ]
            required_measures = {task.measure for task in protocol_tasks}
            required_dimensions = {
                (task.measure, DIMENSION_ALIASES.get(dimension, dimension))
                for task in protocol_tasks
                for dimension in task.dimensions
            }
            if {"protocol_deviation", "temperature_excursion"} - required_measures:
                raise ValueError(
                    "question_contract_violation: protocol quality questions require protocol_deviation and temperature_excursion measures"
                )
            if any(
                (measure, dimension) not in required_dimensions
                for measure in ("protocol_deviation", "temperature_excursion")
                for dimension in ("site_id", "region")
            ):
                raise ValueError(
                    "question_contract_violation: protocol quality questions require site and region breakdowns for each measure"
                )
            if not any(
                task.capability == "rank_groups"
                and task.measure == "site_quality_burden"
                and any(DIMENSION_ALIASES.get(dimension, dimension) == "site_id" for dimension in task.dimensions)
                for task in plan.tasks
            ):
                raise ValueError(
                    "question_contract_violation: protocol quality questions require a governed site ranking"
                )
            if not any(
                task.capability == "assess_protocol_quality"
                and task.measure == "temperature_excursion"
                and "treatment_arm" in {
                    DIMENSION_ALIASES.get(dimension, dimension)
                    for dimension in task.dimensions
                }
                for task in plan.tasks
            ):
                raise ValueError(
                    "question_contract_violation: protocol quality questions require an arm-level temperature attribution check"
                )
        elif compiled.intent == "safety" and compiled.operation == "trend" and compiled.quality_requested:
            allowed_capabilities = {
                "trend_group_measure",
                "assess_data_quality",
                "assess_missingness",
                "describe_population",
                "discover_metric",
            }
            unrelated = sorted(
                {task.capability for task in plan.tasks if task.capability not in allowed_capabilities}
            )
            if unrelated:
                raise ValueError(
                    "question_contract_violation: safety trend quality question received unrelated capabilities: "
                    + ",".join(unrelated)
                )
            trend_valid = any(
                task.capability == "trend_group_measure"
                and task.measure == "serious_adverse_event_rate"
                and {DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions}
                >= {"time", "treatment_arm"}
                for task in plan.tasks
            )
            quality_valid = any(
                task.capability == "assess_data_quality"
                and task.measure == "missing_rate"
                and "treatment_arm"
                in {DIMENSION_ALIASES.get(dimension, dimension) for dimension in task.dimensions}
                for task in plan.tasks
            )
            if not trend_valid or not quality_valid:
                raise ValueError(
                    "question_contract_violation: safety trend quality questions require monthly serious-event trend and inspect_data_quality missingness"
                )
        elif compiled.intent == "safety" and compiled.operation == "compare":
            valid = any(
                task.capability == "summarize_safety"
                and task.measure == "adverse_event_rate"
                and "treatment_arm" in task.dimensions
                for task in plan.tasks
            )
            if not valid:
                raise ValueError(
                    "question_contract_violation: arm safety proportion questions require summarize_safety/adverse_event_rate by treatment_arm"
                )

    @staticmethod
    def _repair_plan(compiled, question: str, trial_id: str, catalog: dict) -> InvestigationPlan | None:
        """Build a narrow, auditable plan for a deterministic semantic contract."""

        if compiled.metric == "baseline_balance":
            available = set(catalog.get("measures", ()) or ())
            required = {"baseline_score"}
            if compiled.duration_requested:
                required.add("disease_duration_months")
            if not required.issubset(available):
                return None
            requirements = [
                AnswerRequirement(requirement_id="R1", question_part="比较治疗组和对照组的基线评分"),
            ]
            tasks = [
                AnalysisTask(
                    task_id="A1",
                    operation="compare",
                    measure="baseline_score",
                    dimensions=("treatment_arm",),
                    capability="compare_group_measure",
                    hypothesis="随机分组后两组的治疗前基线评分可能存在会影响疗效解释的差异",
                    answers=("R1", "R3") if compiled.duration_requested else ("R1", "R2"),
                ),
            ]
            if compiled.duration_requested:
                requirements.extend(
                    (
                        AnswerRequirement(requirement_id="R2", question_part="比较治疗组和对照组的疾病持续时间"),
                        AnswerRequirement(requirement_id="R3", question_part="说明基线或疾病持续时间差异对后续疗效解释的限制"),
                    )
                )
                tasks.append(
                    AnalysisTask(
                        task_id="A2",
                        operation="compare",
                        measure="disease_duration_months",
                        dimensions=("treatment_arm",),
                        capability="compare_group_measure",
                        hypothesis="两组疾病持续时间可能存在需要在疗效解释中保留的组间差异",
                        answers=("R2", "R3"),
                    )
                )
            else:
                requirements.append(
                    AnswerRequirement(requirement_id="R2", question_part="说明基线比较对后续疗效解释的限制")
                )
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=tuple(requirements),
                tasks=tuple(tasks),
            )

        if compiled.intent == "efficacy" and compiled.operation == "sensitivity":
            available = set(catalog.get("measures", ()) or ())
            required = {"treatment_effect"}
            if compiled.quality_burden_requested:
                required.add("site_quality_burden")
            if compiled.protocol_filter_requested:
                required.add("protocol_deviation")
            if not required.issubset(available):
                return None
            requirements = [
                AnswerRequirement(
                    requirement_id="R1",
                    question_part="比较治疗组和对照组的当前 Week-12 疗效差",
                ),
                AnswerRequirement(
                    requirement_id="R2",
                    question_part=(
                        "定位质量负担最高的研究中心并作为排除规则"
                        if compiled.quality_burden_requested
                        else "检查重大方案偏离是否有可关联到受试者的质量证据"
                    ),
                ),
                AnswerRequirement(
                    requirement_id="R3",
                    question_part="说明受治理敏感性分析后的疗效方向以及缺失字段时不能重算的限制",
                ),
            ]
            tasks = []
            if compiled.quality_burden_requested:
                tasks.append(
                    AnalysisTask(
                        task_id="A1",
                        operation="rank",
                        measure="site_quality_burden",
                        dimensions=("site_id",),
                        capability="rank_groups",
                        hypothesis="质量负担最高的研究中心可能改变排除后的疗效方向",
                        answers=("R2",),
                    )
                )
            else:
                tasks.append(
                    AnalysisTask(
                        task_id="A1",
                        operation="compare",
                        measure="protocol_deviation",
                        dimensions=("site_id",),
                        capability="assess_protocol_quality",
                        hypothesis="重大方案偏离可能存在可与受试者疗效记录关联的质量信号",
                        answers=("R2",),
                    )
                )
            effect_task_id = "A2"
            tasks.append(
                AnalysisTask(
                    task_id=effect_task_id,
                    operation="compare",
                    measure="treatment_effect",
                    dimensions=("treatment_arm",),
                    capability="compare_group_measure",
                    hypothesis="当前治疗组与对照组存在可计算的 Week-12 疗效差",
                    answers=("R1",),
                )
            )
            method = (
                "exclude_highest_quality_burden"
                if compiled.quality_burden_requested
                else "exclude_major_protocol_deviation"
            )
            tasks.append(
                AnalysisTask(
                    task_id="A3",
                    operation="sensitivity",
                    measure="treatment_effect",
                    dimensions=("analysis_method", "treatment_arm"),
                    filters={"analysis_method": method},
                    capability="sensitivity_analysis",
                    hypothesis="按问题指定规则排除后，治疗效应方向可能保持或发生改变",
                    depends_on=(effect_task_id,),
                    answers=("R3",),
                )
            )
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=tuple(requirements),
                tasks=tuple(tasks),
            )

        if compiled.metric == "visit_window_deviation":
            if "visit_window_deviation" not in set(catalog.get("measures", ()) or ()):
                return None
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=(
                    AnswerRequirement(
                        requirement_id="R1",
                        question_part="比较不同治疗臂和访视周的访视窗口外记录并指出最高组合",
                    ),
                ),
                tasks=(
                    AnalysisTask(
                        task_id="A1",
                        operation=compiled.operation,
                        measure="visit_window_deviation",
                        dimensions=("visit", "treatment_arm"),
                        capability="assess_visit_window",
                        hypothesis="访视窗口外记录可能在某个治疗臂和访视周集中",
                        answers=("R1",),
                    ),
                ),
            )

        if compiled.intent == "exposure":
            if "adherence_rate" not in set(catalog.get("measures", ())):
                return None
            scope = "site_id" if "site_id" in compiled.dimensions else "region"
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=(
                    AnswerRequirement(
                        requirement_id="R1",
                        question_part=(
                            "比较各研究中心治疗暴露依从性并指出最低中心"
                            if scope == "site_id"
                            else "比较各地区治疗暴露依从性"
                        ),
                    ),
                ),
                tasks=(
                    AnalysisTask(
                        task_id="A1",
                        operation=compiled.operation,
                        measure="adherence_rate",
                        dimensions=(scope,),
                        capability="assess_exposure",
                        answers=("R1",),
                    ),
                ),
            )
        if compiled.intent == "site_quality" and compiled.metric == "site_population":
            if "site_population" not in set(catalog.get("measures", ()) or ()):
                return None
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=(
                    AnswerRequirement(
                        requirement_id="R1",
                        question_part="比较各研究中心受试者规模、治疗臂构成并指出样本量最小的中心",
                    ),
                ),
                tasks=(
                    AnalysisTask(
                        task_id="A1",
                        operation="describe",
                        capability="describe_population",
                        hypothesis="试验总体应包含可用于解释中心规模比较的研究范围",
                        answers=("R1",),
                    ),
                    AnalysisTask(
                        task_id="A2",
                        operation="rank",
                        measure="site_population",
                        dimensions=("site_id", "treatment_arm"),
                        capability="stratify_measure",
                        hypothesis="各研究中心的受试者规模和治疗臂构成可能不均衡",
                        answers=("R1",),
                    ),
                ),
            )
        if compiled.intent == "site_quality" and compiled.metric == "site_quality_burden":
            if not {
                "protocol_deviation",
                "temperature_excursion",
                "site_quality_burden",
            }.issubset(set(catalog.get("measures", ()) or ())):
                return None
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=(
                    AnswerRequirement(requirement_id="R1", question_part="按研究中心比较重大方案偏离的分布"),
                    AnswerRequirement(requirement_id="R2", question_part="按地区比较重大方案偏离的分布"),
                    AnswerRequirement(requirement_id="R3", question_part="按研究中心比较冷链温控异常的分布"),
                    AnswerRequirement(requirement_id="R4", question_part="按地区比较冷链温控异常的分布"),
                    AnswerRequirement(requirement_id="R5", question_part="指出没有数据归属或不能归属治疗臂的部分"),
                ),
                tasks=(
                    AnalysisTask(
                        task_id="A1",
                        operation="compare",
                        measure="protocol_deviation",
                        dimensions=("site_id",),
                        capability="assess_protocol_quality",
                        hypothesis="重大方案偏离可能集中在少数研究中心",
                        answers=("R1",),
                    ),
                    AnalysisTask(
                        task_id="A2",
                        operation="compare",
                        measure="protocol_deviation",
                        dimensions=("region",),
                        capability="assess_protocol_quality",
                        hypothesis="重大方案偏离可能在地区之间存在差异",
                        answers=("R2",),
                    ),
                    AnalysisTask(
                        task_id="A3",
                        operation="compare",
                        measure="temperature_excursion",
                        dimensions=("site_id",),
                        capability="assess_protocol_quality",
                        hypothesis="冷链温控异常可能集中在少数研究中心，但只能按中心归属",
                        answers=("R3",),
                    ),
                    AnalysisTask(
                        task_id="A4",
                        operation="compare",
                        measure="temperature_excursion",
                        dimensions=("region",),
                        capability="assess_protocol_quality",
                        hypothesis="冷链温控异常可能在地区之间存在差异，但不能分摊到治疗臂",
                        answers=("R4",),
                    ),
                    AnalysisTask(
                        task_id="A5",
                        operation="rank",
                        measure="site_quality_burden",
                        dimensions=("site_id",),
                        capability="rank_groups",
                        hypothesis="质量负担最高的中心可能集中承载上述异常",
                        answers=("R1", "R3"),
                    ),
                    AnalysisTask(
                        task_id="A6",
                        operation="compare",
                        measure="temperature_excursion",
                        dimensions=("treatment_arm",),
                        capability="assess_protocol_quality",
                        hypothesis="冷链温控异常可能没有治疗臂归属，需要用受治理查询确认这一数据边界",
                        answers=("R5",),
                    ),
                ),
            )
        if compiled.intent == "safety" and compiled.operation == "trend" and compiled.quality_requested:
            if not {"serious_adverse_event_rate", "missing_rate"}.issubset(
                set(catalog.get("measures", ()) or ())
            ):
                return None
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=(
                    AnswerRequirement(
                        requirement_id="R1",
                        question_part="按月份说明严重安全性事件的观察到趋势",
                    ),
                    AnswerRequirement(
                        requirement_id="R2",
                        question_part="按治疗臂说明记录缺失及其对月度趋势判断的限制",
                    ),
                ),
                tasks=(
                    AnalysisTask(
                        task_id="A1",
                        operation="trend",
                        measure="serious_adverse_event_rate",
                        dimensions=("time", "treatment_arm"),
                        capability="trend_group_measure",
                        hypothesis="严重安全性事件率可能在某些月份出现上升或下降",
                        answers=("R1",),
                    ),
                    AnalysisTask(
                        task_id="A2",
                        operation="quality_check",
                        measure="missing_rate",
                        dimensions=("treatment_arm",),
                        capability="assess_data_quality",
                        hypothesis="安全性记录缺失可能与月度事件趋势同时出现并限制解释",
                        answers=("R2",),
                    ),
                ),
            )

        if compiled.intent == "safety" and compiled.operation == "compare":
            if "adverse_event_rate" not in set(catalog.get("measures", ())):
                return None
            return InvestigationPlan(
                question=question,
                trial_id=trial_id,
                answer_requirements=(AnswerRequirement(requirement_id="R1", question_part="比较治疗组和对照组安全性事件比例并核对严重事件"),),
                tasks=(
                    AnalysisTask(
                        task_id="A1",
                        operation="compare",
                        measure="adverse_event_rate",
                        dimensions=("treatment_arm",),
                        capability="summarize_safety",
                        answers=("R1",),
                    ),
                ),
            )
        return None

    @classmethod
    def _finish_baseline_balance(cls,state:InvestigationState,evidence:Evidence,observation:dict)->InvestigationState:
        by_arm={str(row.get("arm","")).lower():row for row in evidence.rows}
        treatment,control=by_arm.get("treatment",{}),by_arm.get("control",{})
        treatment_mean=treatment.get("baseline_mean")
        control_mean=control.get("baseline_mean")
        delta=observation.get("baseline_delta")
        cls._close_testing_inconclusive(state,"本题是基线平衡描述性审查，不进行根因归因")
        if treatment_mean is None or control_mean is None or delta is None:
            state.mark_inconclusive(f"治疗组或对照组的基线聚合值不足，无法评价随机化平衡。[{evidence.evidence_id}]")
            return state
        state.submit_for_approval(
            f"治疗组基线均值 {float(treatment_mean):.2f}，对照组基线均值 {float(control_mean):.2f}，相差 {float(delta):+.2f} 分。"
            "基线差异可能影响 Week-12 组间结果解释，但没有预设平衡标准、标准化差异或统计不确定性时，"
            f"不能仅凭均值差认定随机化不平衡，也不能认定它造成了疗效差异。[{evidence.evidence_id}]"
        )
        return state

    @classmethod
    def _finish_exposure_review(cls, state: InvestigationState, evidence: Evidence, observation: dict) -> InvestigationState:
        """Answer a direct exposure comparison without forcing root-cause support semantics.

        Exposure questions ask for a descriptive regional comparison.  The generic dynamic loop
        normally requires a supported hypothesis before approval, which is appropriate for causal
        investigations but would incorrectly turn a healthy adherence comparison into
        ``inconclusive``.  This path keeps the evidence and limitations while answering exactly
        what was asked; it never claims that a regional difference caused an outcome.
        """

        rows = evidence.rows
        comparable = []
        for row in rows:
            region = str(row.get("region") or row.get("site_id") or "未分组")
            rate = row.get("adherence_rate")
            try:
                rate_value = float(rate) if rate is not None else None
            except (TypeError, ValueError):
                rate_value = None
            actual, planned = row.get("actual_dose"), row.get("planned_dose")
            try:
                ratio = float(actual) / float(planned) if actual is not None and planned not in (None, 0) else None
            except (TypeError, ValueError):
                ratio = None
            if rate_value is not None and ratio is None:
                ratio = rate_value
            comparable.append((region, rate_value, ratio, row.get("missed_doses"), row.get("sample_size")))

        measured = [item for item in comparable if item[1] is not None]
        if not measured:
            cls._close_testing_inconclusive(state, "治疗暴露查询未返回可比较的依从性测量值")
            state.mark_inconclusive(f"当前数据没有可比较的地区依从性测量值，无法回答实际剂量、计划剂量和漏服差异。[{evidence.evidence_id}]")
            return state

        def percent(value: float | None) -> str:
            return "未提供" if value is None else f"{value * 100:.1f}%"

        details = []
        for region, rate, ratio, missed, sample_size in measured:
            dose_text = f"实际/计划剂量比 {ratio:.3f}" if ratio is not None else "实际/计划剂量比未提供"
            missed_text = f"漏服 {missed} 次" if missed is not None else "漏服次数未提供"
            n_text = f"（样本量 {sample_size}）" if sample_size is not None else ""
            details.append(f"{region}：依从性 {percent(rate)}，{dose_text}，{missed_text}{n_text}")
        lowest = min(measured, key=lambda item: item[1])
        highest = max(measured, key=lambda item: item[1])
        gap = lowest[1] - highest[1]
        flagged = [item[0] for item in measured if item[1] < 0.9]
        follow_up = (
            [f"优先核查 {', '.join(flagged)} 的给药记录、漏服原因和访视窗口" ]
            if flagged
            else ["各地区依从性均未低于 90% 阈值；如需解释疗效差异，建议结合结局缺失、方案偏离和实际暴露时序进一步核查"]
        )
        cls._close_testing_inconclusive(state, "本题是地区治疗暴露的描述性比较，不把依从性差异直接归因于疗效")
        state.submit_for_approval(
            "按地区比较结果：" + "；".join(details) +
            f"。最低依从性为 {lowest[0]}（{percent(lowest[1])}），最高为 {highest[0]}（{percent(highest[1])}），"
            f"两者相差 {abs(gap) * 100:.1f} 个百分点。"
            + (f"低于 90% 的地区为：{', '.join(flagged)}。" if flagged else "未发现低于 90% 阈值的地区。")
            + f"这些是描述性暴露证据，不能单独证明造成疗效或安全性差异。[{evidence.evidence_id}]"
        )
        state.open_questions.extend(follow_up)
        state.refresh_report(follow_up=follow_up, evidence_ids=[evidence.evidence_id or ""])
        return state

    @classmethod
    def _finish_safety_summary_review(
        cls, state: InvestigationState, evidence: Evidence, observation: dict
    ) -> InvestigationState:
        """Answer an arm-level safety proportion question descriptively.

        The summary mart has one row per treatment arm.  This path prevents the generic
        causal-investigation gate from replacing a directly requested rate comparison with an
        inconclusive hypothesis message, while preserving the explicit no-causality boundary.
        """

        if observation.get("signal") == "insufficient_data":
            cls._close_testing_inconclusive(state, "安全性汇总的测量值被小样本规则抑制")
            state.mark_inconclusive(f"当前数据无法提供治疗组与对照组的安全性事件比例，不能据此判断严重事件是否出现。[{evidence.evidence_id}]")
            return state
        summary = str(observation.get("human_summary") or "安全性汇总已完成")
        cls._close_testing_inconclusive(state, "本题是治疗组与对照组安全性比例的描述性比较，不进行因果归因")
        state.submit_for_approval(
            summary + f" [这是试验级描述性比例，不代表药物导致事件，也不用于个体诊疗建议。][{evidence.evidence_id}]"
        )
        state.refresh_report(
            key_findings=[summary],
            limitations=["安全性事件比例是试验级描述性统计；不能由该汇总结果单独推断因果或个体风险"],
            evidence_ids=[evidence.evidence_id or ""],
        )
        return state

    @classmethod
    def _finish_efficacy_quality_review(cls,state:InvestigationState,quality_review_tools:tuple[str,...])->InvestigationState:
        effect=next((item for item in state.observations if item.get("signal")=="treatment_effect"),{})
        delta=effect.get("effect_delta")
        evidence_by_tool={step.tool:state.evidence[index] for index,step in enumerate(state.steps) if index<len(state.evidence)}
        effect_evidence=evidence_by_tool.get("compare_treatment_effect")
        effect_rows=effect_evidence.rows if effect_evidence else []
        arm_effects={str(row.get("arm","")).lower():row.get("mean_improvement") for row in effect_rows}
        treatment=arm_effects.get("treatment")
        control=arm_effects.get("control")
        if delta is None:
            effect_text="治疗组或对照组的可发布聚合值不足，无法计算 Week-12 差异"
        elif treatment is not None and control is not None:
            effect_text=(
                f"治疗组平均改善 {float(treatment):.2f} 分，对照组平均改善 {float(control):.2f} 分；"
                f"治疗组相对对照组的 Week-12 平均改善差为 {delta:+.2f} 分"
            )
        else:
            effect_text=f"治疗组相对对照组的 Week-12 平均改善差为 {delta:+.2f} 分"
        quality_parts=[]
        for tool,label,field in (
            ("analyze_missingness","结局缺失率","missing_rate"),
            ("analyze_visit_windows","访视窗口偏离率","outside_window_rate"),
            ("inspect_data_quality","总体数据质量",""),
        ):
            item=evidence_by_tool.get(tool)
            if not item:
                continue
            values=[]
            if field:
                values_by_arm={}
                for row in item.rows:
                    value=row.get(field)
                    if value is None:
                        continue
                    arm=str(row.get("arm","")).lower()
                    values_by_arm[arm]=max(float(value),values_by_arm.get(arm,float("-inf")))
                for arm,value in values_by_arm.items():
                    arm_label={"treatment":"治疗组","control":"对照组"}.get(arm,arm or "总体")
                    values.append(f"{arm_label}{label} {value*100:.1f}%")
            observation=next((entry for entry in state.observations if entry.get("signal") in ({"missingness"} if tool in {"analyze_missingness","inspect_data_quality"} else {"visit_windows"})),{})
            assessment=observation.get("human_summary")
            detail="、".join(values)
            if detail and assessment:
                quality_parts.append(f"{detail}；{assessment} [{item.evidence_id}]")
            elif detail:
                quality_parts.append(f"{detail} [{item.evidence_id}]")
            elif assessment:
                quality_parts.append(f"{assessment} [{item.evidence_id}]")
            else:
                quality_parts.append(f"{label}已完成核查，详见 [{item.evidence_id}]")
        cls._close_testing_inconclusive(state,"本题是描述性比较与质量复核，不进行下降根因归因")
        state.submit_for_approval(
            f"{effect_text}，详见 [{effect_evidence.evidence_id if effect_evidence else 'E01'}]。"
            f"可能影响结论解释的数据质量项目包括：{'；'.join(quality_parts) or '当前发布数据没有提供可核查的质量工具'}。"
            "这些是描述性核查结果，不代表数据质量问题已经造成疗效差异。"
        )
        return state

    @staticmethod
    def _resolve_testing_hypotheses(state: InvestigationState) -> list[HypothesisStatus]:
        """Resolve from attributed evidence, never from the model's desire to finish."""

        statuses: list[HypothesisStatus] = []
        for item in state.hypotheses:
            if item.status is not HypothesisStatus.TESTING:
                continue
            supporting = [e.evidence_id for e in state.evidence if item.hypothesis_id in e.supports]
            contradicting = [e.evidence_id for e in state.evidence if item.hypothesis_id in e.contradicts]
            if supporting and not contradicting:
                status = HypothesisStatus.SUPPORTED
                rationale = "受治理观测支持该假设，且未发现已归因反证"
            elif contradicting and not supporting:
                status = HypothesisStatus.REJECTED
                rationale = "受治理观测反驳该假设，且没有支持证据"
            else:
                status = HypothesisStatus.INCONCLUSIVE
                rationale = "支持与反证并存，或没有可归因观测"
            state.resolve_hypothesis(item.hypothesis_id, status, evidence_ids=supporting, counter_evidence_ids=contradicting, rationale=rationale)
            statuses.append(status)
        return statuses

    @staticmethod
    def _close_testing_inconclusive(state: InvestigationState, rationale: str) -> None:
        evidence_ids = [item.evidence_id for item in state.evidence]
        for hypothesis in state.hypotheses:
            if hypothesis.status is HypothesisStatus.TESTING:
                state.resolve_hypothesis(hypothesis.hypothesis_id, HypothesisStatus.INCONCLUSIVE, evidence_ids=evidence_ids, rationale=rationale)

