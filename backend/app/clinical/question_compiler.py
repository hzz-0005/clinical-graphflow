from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


Intent = Literal["efficacy", "safety", "exposure", "site_quality", "data_quality", "general", "unsupported"]
Operation = Literal["compare", "rank", "trend", "explain", "inspect", "discover", "sensitivity", "blocked"]


class CompiledClinicalQuestion(BaseModel):
    """Deterministic semantic contract between a user question and the agent planner."""

    model_config = ConfigDict(frozen=True)
    question: str
    trial_id: str | None = None
    intent: Intent
    operation: Operation
    metric: str | None = None
    dimensions: tuple[str, ...] = ()
    required_domains: frozenset[str] = frozenset()
    candidate_tools: tuple[str, ...] = ()
    causal_claim_allowed: bool = False
    quality_requested: bool = False
    decline_premise: bool = False
    duration_requested: bool = False
    sensitivity_requested: bool = False
    quality_burden_requested: bool = False
    protocol_filter_requested: bool = False
    block_reason: str | None = None


class ClinicalQuestionCompiler:
    """Compile common clinical language into a governed, testable analysis request.

    This component deliberately does not generate SQL. It narrows the tool search space and makes
    missing-domain checks possible before an LLM is allowed to choose the next action.
    """

    def compile(self, question: str, trial_id: str | None = None) -> CompiledClinicalQuestion:
        q = question.casefold()
        if self._has(q, "推荐治疗", "推荐剂量", "开药", "diagnose patient", "recommended dose", "treatment recommendation"):
            return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="unsupported", operation="blocked", block_reason="individual_treatment_recommendation")

        site = self._has(q, "中心", "site")
        baseline = self._has(q, "基线", "治疗前", "随机化平衡", "baseline", "randomization balance")
        duration = self._has(q, "疾病持续时间", "病程", "duration", "disease duration")
        efficacy = self._has(q, "疗效", "治疗效果", "治疗效应", "效应差", "效果差", "疗效差异", "平均改善", "改善更高", "终点", "efficacy", "endpoint", "mean improvement")
        safety = self._has(q, "安全", "不良事件", "不良反应", "adverse", "safety")
        quality = self._has(q, "缺失", "质量", "窗口", "偏离", "missing", "quality", "deviation")
        quality_burden = self._has(q, "质量负担", "quality burden", "最高负担")
        sensitivity = self._has(
            q,
            "剔除",
            "排除",
            "敏感性",
            "敏感分析",
            "重新计算",
            "稳健性",
            "leave-one-out",
            "leave one out",
            "only analyze",
            "仅分析",
            "只分析",
        )
        protocol_filter = self._has(
            q,
            "没有重大方案偏离",
            "无重大方案偏离",
            "不含重大方案偏离",
            "重大方案偏离的受试者",
            "major protocol deviation",
        )
        protocol_quality = self._has(
            q,
            "方案偏离",
            "方案违背",
            "温控",
            "冷链",
            "温度偏离",
            "温度异常",
            "protocol deviation",
            "temperature excursion",
            "cold chain",
        )
        visit_window = self._has(
            q,
            "访视窗口",
            "窗口外",
            "窗口偏离",
            "访视偏离",
            "outside visit window",
            "visit window",
            "window deviation",
        )
        exposure = self._has(
            q,
            "依从性",
            "服药依从",
            "实际剂量",
            "计划剂量",
            "漏服",
            "给药",
            "治疗暴露",
            "暴露依从",
            "adherence",
            "compliance",
            "actual dose",
            "planned dose",
            "missed dose",
            "treatment exposure",
        )
        decline = self._has(q, "下降", "降低", "变差", "衰减", "decline", "decrease", "deteriorat")
        rank = self._has(q, "哪个", "最弱", "最低", "最高", "排名", "weakest", "lowest", "highest", "rank")
        trend = self._has(q, "最近", "趋势", "增加", "下降", "变化", "trend", "increase", "decrease")
        safety_compare = safety and self._has(
            q,
            "比例",
            "发生率",
            "各是多少",
            "分别",
            "组间",
            "严重事件是否",
            "adverse event rate",
            "event proportion",
        )

        # A sensitivity question is a different analytical contract from a plain site ranking:
        # it needs a baseline effect, a governed exclusion/ranking rule and a second effect
        # calculation.  Compile it before the broad ``site + efficacy + rank`` branch so words
        # such as “剔除质量负担最高的中心” cannot be mistaken for a one-shot ranking request.
        if efficacy and sensitivity and (site or quality_burden or protocol_quality):
            return CompiledClinicalQuestion(
                question=question,
                trial_id=trial_id,
                intent="efficacy",
                operation="sensitivity",
                metric="treatment_effect",
                dimensions=("treatment_arm", "site_id"),
                required_domains=frozenset({"DM", "ADSL", "ADEFF"}),
                candidate_tools=(
                    "rank_sites",
                    "compare_treatment_effect",
                    "run_sensitivity_analysis",
                    "inspect_protocol_quality",
                    "inspect_data_quality",
                    "search_clinical_metrics",
                ),
                quality_requested=quality,
                sensitivity_requested=True,
                quality_burden_requested=quality_burden,
                protocol_filter_requested=protocol_filter or protocol_quality,
            )
        if site and efficacy and rank:
            return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="site_quality", operation="rank", metric="treatment_effect", dimensions=("site_id", "arm"), required_domains=frozenset({"DM", "ADSL", "ADEFF"}), candidate_tools=("profile_sites", "rank_sites", "inspect_protocol_quality", "inspect_treatment_exposure", "inspect_trial", "search_clinical_metrics"))
        if site and not exposure and self._has(q, "受试者", "样本", "规模", "构成", "入组", "participant", "sample", "arm composition"):
            return CompiledClinicalQuestion(
                question=question,
                trial_id=trial_id,
                intent="site_quality",
                operation="rank",
                metric="site_population",
                dimensions=("site_id", "treatment_arm"),
                required_domains=frozenset({"DM", "ADSL", "ADEFF"}),
                candidate_tools=("profile_sites", "inspect_trial", "search_clinical_metrics"),
            )
        if protocol_quality:
            return CompiledClinicalQuestion(
                question=question,
                trial_id=trial_id,
                intent="site_quality",
                operation="compare",
                metric="site_quality_burden",
                dimensions=("site_id", "region"),
                required_domains=frozenset({"DM"}),
                candidate_tools=("inspect_protocol_quality", "rank_sites", "inspect_trial", "search_clinical_metrics"),
            )
        if baseline:
            return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="efficacy", operation="inspect", metric="baseline_balance", dimensions=("arm",), required_domains=frozenset({"DM", "ADSL"}), candidate_tools=("check_randomization_balance", "inspect_trial", "search_clinical_metrics"), duration_requested=duration)
        if visit_window:
            return CompiledClinicalQuestion(
                question=question,
                trial_id=trial_id,
                intent="data_quality",
                operation="compare" if self._has(q, "比较", "分别", "分布") else "rank",
                metric="visit_window_deviation",
                dimensions=("visit", "treatment_arm"),
                required_domains=frozenset({"DM", "ADEFF"}),
                candidate_tools=("analyze_visit_windows", "inspect_trial", "search_clinical_metrics"),
            )
        if exposure:
            exposure_by_site = site or self._has(q, "各中心", "研究中心", "每个中心")
            return CompiledClinicalQuestion(
                question=question,
                trial_id=trial_id,
                intent="exposure",
                operation="rank" if rank else "compare",
                metric="adherence_rate",
                dimensions=("site_id",) if exposure_by_site else ("region",),
                required_domains=frozenset({"DM"}),
                candidate_tools=("inspect_treatment_exposure", "search_clinical_metrics", "inspect_trial", "analyze_missingness", "inspect_protocol_quality"),
                quality_requested=quality or self._has(q, "核查", "影响结论", "进一步"),
            )
        if safety:
            if trend:
                return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="safety", operation="trend", metric="serious_adverse_event_rate", dimensions=("time", "arm"), required_domains=frozenset({"DM", "AE"}), candidate_tools=("inspect_trial", "analyze_safety_trend", "inspect_data_quality", "analyze_missingness", "search_clinical_metrics"), quality_requested=quality)
            if safety_compare:
                return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="safety", operation="compare", metric="adverse_event_rate", dimensions=("treatment_arm",), required_domains=frozenset({"DM", "AE"}), candidate_tools=("inspect_safety_summary", "search_clinical_metrics", "inspect_trial", "analyze_safety_trend", "inspect_data_quality", "analyze_missingness"))
            return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="safety", operation="inspect", metric="serious_adverse_event_rate", dimensions=("arm",), required_domains=frozenset({"DM", "AE"}), candidate_tools=("inspect_trial", "analyze_safety_trend", "inspect_data_quality", "analyze_missingness", "search_clinical_metrics"))
        if efficacy:
            return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="efficacy", operation="trend" if trend else "compare", metric="treatment_effect", dimensions=("arm",), required_domains=frozenset({"DM", "ADSL", "ADEFF"}), candidate_tools=("inspect_trial", "compare_treatment_effect", "profile_sites", "rank_sites", "inspect_data_quality", "inspect_protocol_quality", "inspect_treatment_exposure", "analyze_missingness", "analyze_visit_windows", "run_sensitivity_analysis", "search_clinical_metrics"), quality_requested=quality, decline_premise=decline)
        if site:
            return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="site_quality", operation="rank" if rank else "inspect", dimensions=("site_id",), required_domains=frozenset({"DM"}), candidate_tools=("inspect_trial", "rank_sites", "profile_sites", "inspect_protocol_quality", "inspect_treatment_exposure", "search_clinical_metrics"))
        if quality:
            return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="data_quality", operation="explain", required_domains=frozenset({"DM"}), candidate_tools=("inspect_trial", "inspect_data_quality", "analyze_missingness", "analyze_visit_windows", "inspect_protocol_quality", "search_clinical_metrics"))
        return CompiledClinicalQuestion(question=question, trial_id=trial_id, intent="general", operation="discover", candidate_tools=("inspect_trial", "search_clinical_metrics"))

    @staticmethod
    def _has(text: str, *terms: str) -> bool:
        return any(term in text for term in terms)

