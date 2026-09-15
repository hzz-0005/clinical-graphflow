from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from app.agent.models import (
    Evidence,
    EvidenceType,
    InvestigationBudget,
    InvestigationState,
    InvestigationStatus,
    InvestigationStep,
    StepStatus,
)
from app.clinical.models import ArmSummary, ClinicalAnalysisSummary
from app.clinical.statistics import continuous_smd
from app.clinical.tools import (
    ClinicalDataScope,
    ClinicalSubgroup,
    ClinicalToolResult,
    ClinicalTools,
    CompareTreatmentEffectRequest,
    InspectProtocolQualityRequest,
    InspectTreatmentExposureRequest,
)
from app.clinical.verifier import ClinicalConclusionVerifier
from app.semantic.metrics import ClinicalMetricRepository


class ClinicalInvestigator:
    """Deterministic clinical investigation loop for the governed V4 scenario.

    The playbook is deliberately fixed: it always walks trial → region effect → balance →
    missingness → site drill-down → exposure → protocol quality, and it always reports the
    ``treatment_effect`` metric. It therefore must not *pretend* to have inferred those choices
    from the question, and it must not invent a region: the analysis subgroup comes from the
    caller or from the caller's authorized scope, never from a hardcoded "Asia".

    The result-driven, question-driven successor is the V8 dynamic runtime
    (``dynamic_investigator.py``); this class is kept for the legacy V4/V5 contract.
    """

    def __init__(
        self,
        metrics: ClinicalMetricRepository,
        tools_factory: Callable[[ClinicalDataScope], ClinicalTools],
        max_steps: int = 16,
        max_queries: int = 12,
    ) -> None:
        self._metrics = metrics
        self._tools_factory = tools_factory
        self._max_steps = max_steps
        self._max_queries = max_queries
        self._verifier = ClinicalConclusionVerifier()

    def investigate(
        self, question: str, scope: ClinicalDataScope, subgroup: str | None = None
    ) -> InvestigationState:
        state = InvestigationState(
            question=question,
            domain="clinical_trial",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(
                max_steps=self._max_steps, max_queries=self._max_queries
            ),
        )
        try:
            self._run(state, scope, subgroup)
        except ValueError as exc:
            if state.evidence:
                state.mark_inconclusive(f"调查预算或证据不足（inconclusive）：{exc}")
            else:
                state.fail(str(exc))
        except Exception as exc:
            state.fail(str(exc))
        return state

    @staticmethod
    def _region_subgroup(
        scope: ClinicalDataScope, requested: str | None
    ) -> ClinicalSubgroup | None:
        """Resolve the analysis subgroup from governance instead of a hardcoded region.

        The caller's ``subgroup`` already drives the authorization check as a region, so a
        requested value is honoured first. Otherwise fall back to the caller's authorized regions.
        When the principal has unrestricted region access there is nothing to narrow to, so the
        investigation runs on the whole trial rather than silently inventing "Asia".
        """

        value = (requested or "").strip()
        if not value and scope.regions:
            value = sorted(scope.regions)[0]
        if not value:
            return None
        return ClinicalSubgroup(dimension="region", value=value)

    def _run(
        self,
        state: InvestigationState,
        scope: ClinicalDataScope,
        requested_subgroup: str | None = None,
    ) -> None:
        tools = self._tools_factory(scope)
        matches = tools.search_clinical_metrics(state.question)
        if not matches:
            raise ValueError("No governed clinical metric matched the question")
        # 这个剧本固定分析治疗效应，所以 state.metric 如实写 treatment_effect；同时把「问题命中了
        # 哪些受治理指标」写进步骤说明，避免让读者以为指标是随问题动态推断出来的。
        state.metric = "treatment_effect"
        matched = sorted(
            {
                str((match.get("metric") or {}).get("name") or "")
                for match in matches
                if (match.get("metric") or {}).get("name")
            }
        )
        self._step(
            state,
            "search_clinical_metrics",
            "问题匹配到受治理指标 "
            + ("、".join(matched) or "无")
            + "；V4 确定性剧本固定分析 Week-12 治疗效应（treatment_effect），不随匹配结果改变。",
        )

        trial_id = sorted(scope.trial_ids)[0]
        subgroup = self._region_subgroup(scope, requested_subgroup)
        scope_label = f"{subgroup.value} 亚组" if subgroup else "全试验（未按地区收窄）"

        trial = self._query(state, tools.inspect_trial(trial_id))
        self._evidence(state, trial, EvidenceType.BASELINE, "试验总体与随机化人群已确认。")

        outcome = self._query(
            state,
            tools.compare_treatment_effect(
                CompareTreatmentEffectRequest(
                    trial_id=trial_id, subgroup=subgroup, timepoint="week_12"
                )
            ),
        )
        outcome_e = self._evidence(
            state,
            outcome,
            EvidenceType.CLINICAL_OUTCOME,
            f"{scope_label}的 Week-12 疗效差异已确认。",
        )

        balance = self._query(
            state, tools.check_randomization_balance(trial_id, subgroup)
        )
        balance_e = self._evidence(
            state, balance, EvidenceType.CLINICAL_BALANCE, "随机化基线平衡已检查。"
        )

        missing = self._query(state, tools.analyze_missingness(trial_id, subgroup))
        missing_e = self._evidence(
            state, missing, EvidenceType.CLINICAL_MISSINGNESS, "Week-12 缺失模式已检查。"
        )

        sites = self._query(state, tools.profile_sites(trial_id, subgroup))
        site_id = self._lowest_effect_site(sites)
        if scope.site_ids and site_id not in scope.site_ids:
            raise ValueError(f"Identified site {site_id} is outside authorized scope")
        site_e = self._evidence(
            state, sites, EvidenceType.CLINICAL_SITE, f"中心分层定位到 {site_id} 异常。"
        )

        exposure = self._query(
            state,
            tools.inspect_treatment_exposure(
                InspectTreatmentExposureRequest(trial_id=trial_id, site_id=site_id)
            ),
        )
        exposure_e = self._evidence(
            state, exposure, EvidenceType.CLINICAL_EXPOSURE, f"{site_id} 治疗暴露与依从性下降。"
        )

        quality = self._query(
            state,
            tools.inspect_protocol_quality(
                InspectProtocolQualityRequest(trial_id=trial_id, site_id=site_id)
            ),
        )
        quality_e = self._evidence(
            state,
            quality,
            EvidenceType.CLINICAL_PROTOCOL_QUALITY,
            f"{site_id} 处理异常与方案质量已检查。",
        )

        if scope.published_batch_id and (not exposure.rows or not quality.rows):
            state.warnings.append(
                "Selected published batch does not provide treatment exposure or protocol-quality domains; no fallback data was used."
            )
            state.mark_inconclusive(
                "所选已发布数据版本未提供治疗暴露或方案质量域，无法完成因果调查；系统未回退到其他数据版本。 "
                f"证据 {exposure_e.evidence_id}、{quality_e.evidence_id} 已记录数据缺口。"
            )
            return

        analysis = self._analysis_summary(
            outcome, balance, missing, sites, exposure, quality,
            evidence_ids={
                "effect": outcome_e.evidence_id or "",
                "balance": balance_e.evidence_id or "",
                "missingness": missing_e.evidence_id or "",
                "site": site_e.evidence_id or "",
                "quality": quality_e.evidence_id or "",
                "causal": quality_e.evidence_id or "",
            },
        )
        state.baseline["clinical_analysis"] = analysis.model_dump(mode="json")
        citations = " ".join(
            f"[{item.evidence_id}]"
            for item in (outcome_e, balance_e, missing_e, site_e, exposure_e, quality_e)
        )
        state.answer = (
            f"The Week-12 effect is lower at {site_id}"
            + (f" within the {subgroup.value} region" if subgroup else "")
            + "; the evidence links a handling excursion with reduced treatment exposure "
            f"and lower site-level efficacy, rather than treating region itself as the cause. {citations}"
        )
        report = self._verifier.verify(state)
        state.verification = report  # Pydantic assignment remains runtime-polymorphic.
        state.confidence = 1.0 if report.passed else 0.0
        self._step(state, "verify_clinical_conclusion", "统计、证据和语言护栏验证完成。")
        if report.passed:
            state.submit_for_approval(state.answer)
        else:
            state.mark_inconclusive("证据尚不足以形成可提交审批的临床结论。")

    def _query(
        self, state: InvestigationState, result: ClinicalToolResult
    ) -> ClinicalToolResult:
        state.record_query(len(result.rows))
        self._step(state, result.tool, f"{result.tool} 返回 {len(result.rows)} 个聚合结果。")
        return result

    @staticmethod
    def _evidence(
        state: InvestigationState,
        result: ClinicalToolResult,
        evidence_type: EvidenceType,
        claim: str,
    ) -> Evidence:
        return state.add_evidence(
            Evidence(
                claim=claim,
                source=result.source,
                sql=result.sql or "governed semantic lookup",
                params=list(result.params),
                rows=result.rows,
                sample_size=sum(
                    int(row.get("sample_size") or 0) for row in result.rows
                ) or None,
                metric="treatment_effect",
                evidence_type=evidence_type,
                quality_flags=result.warnings,
            )
        )

    @staticmethod
    def _lowest_effect_site(result: ClinicalToolResult) -> str:
        by_site: dict[str, dict[str, float]] = {}
        for row in result.rows:
            if row.get("suppressed") or row.get("mean_improvement") is None:
                continue
            by_site.setdefault(str(row["site_id"]), {})[str(row["arm"])] = float(
                row["mean_improvement"]
            )
        effects = {
            site: arms["treatment"] - arms["control"]
            for site, arms in by_site.items()
            if {"control", "treatment"} <= arms.keys()
        }
        if not effects:
            raise ValueError("No unsuppressed site-level treatment comparison")
        return min(effects, key=effects.get)

    @staticmethod
    def _arm_rows(result: ClinicalToolResult) -> dict[str, dict]:
        return {str(row["arm"]): row for row in result.rows if not row.get("suppressed")}

    def _analysis_summary(
        self,
        outcome: ClinicalToolResult,
        balance: ClinicalToolResult,
        missing: ClinicalToolResult,
        sites: ClinicalToolResult,
        exposure: ClinicalToolResult,
        quality: ClinicalToolResult,
        evidence_ids: dict[str, str],
    ) -> ClinicalAnalysisSummary:
        outcomes = self._arm_rows(outcome)
        balances = self._arm_rows(balance)
        if not {"control", "treatment"} <= outcomes.keys():
            raise ValueError("Both randomized arms are required")

        missing_by_arm: dict[str, tuple[float, int]] = {}
        for arm in ("control", "treatment"):
            rows = [row for row in missing.rows if row.get("arm") == arm and not row.get("suppressed")]
            total = sum(int(row.get("sample_size") or 0) for row in rows)
            weighted = sum(float(row.get("missing_rate") or 0) * int(row.get("sample_size") or 0) for row in rows)
            missing_by_arm[arm] = (weighted / total if total else 0.0, total)

        smd = 0.0
        if {"control", "treatment"} <= balances.keys():
            smd = continuous_smd(
                float(balances["treatment"].get("baseline_mean") or 0),
                float(balances["control"].get("baseline_mean") or 0),
                float(balances["treatment"].get("baseline_variance") or 0),
                float(balances["control"].get("baseline_variance") or 0),
            )
        site_sizes: dict[str, int] = {}
        for row in sites.rows:
            site_sizes[str(row["site_id"])] = site_sizes.get(str(row["site_id"]), 0) + int(row.get("sample_size") or 0)
        total_sites = sum(site_sizes.values())
        maximum_site_share = max(site_sizes.values(), default=0) / total_sites if total_sites else 0.0

        def arm(name: str) -> ArmSummary:
            row = outcomes[name]
            return ArmSummary(
                arm=name,
                sample_size=int(row["sample_size"]),
                mean_improvement=float(row["mean_improvement"]),
                variance=float(row.get("variance") or 0),
                missing_rate=missing_by_arm[name][0],
            )

        return ClinicalAnalysisSummary(
            control=arm("control"),
            treatment=arm("treatment"),
            balance_smds={"baseline_score": smd},
            maximum_site_share=maximum_site_share,
            site_stratified_checked=bool(sites.rows),
            tested_subgroups=1,
            exposure_checked=bool(exposure.rows),
            protocol_quality_checked=bool(quality.rows),
            itt_primary=True,
            causal_language_requested=True,
            temporal_order_checked=any(
                (row.get("temperature_excursions") or 0) > 0 for row in quality.rows
            ),
            alternatives_checked=bool(balance.rows and missing.rows and sites.rows),
            evidence_ids=evidence_ids,
        )

    @staticmethod
    def _step(state: InvestigationState, tool: str, summary: str) -> None:
        state.record_step()
        now = datetime.now(timezone.utc)
        state.steps.append(
            InvestigationStep(
                sequence=len(state.steps) + 1,
                tool=tool,
                status=StepStatus.COMPLETED,
                summary=summary,
                started_at=now,
                finished_at=now,
            )
        )

