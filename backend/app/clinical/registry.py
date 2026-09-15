from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.clinical.tools import (
    ClinicalToolResult,
    ClinicalTools,
    CompareTreatmentEffectRequest,
    GroupedTrialRequest,
    InspectProtocolQualityRequest,
    InspectTreatmentExposureRequest,
    SensitivityAnalysisRequest,
    SubgroupForestRequest,
)
from app.clinical.canonical import ClinicalSubgroup

CLINICAL_TOOL_NAMES = (
    "search_clinical_metrics",
    "inspect_trial",
    "compare_treatment_effect",
    "check_randomization_balance",
    "analyze_missingness",
    "profile_sites",
    "inspect_treatment_exposure",
    "inspect_protocol_quality",
    "submit_clinical_conclusion",
    "inspect_safety_summary",
    "analyze_safety_trend",
    "analyze_visit_windows",
    "analyze_visit_missingness",
    "inspect_data_quality",
    "rank_sites",
    "build_subgroup_forest",
    "run_sensitivity_analysis",
)

SAFE_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

DEFAULT_METADATA = {
    "search_clinical_metrics": ("检索受治理临床指标", "discovery", 1),
    "inspect_trial": ("检查试验总体结构", "trial", 1),
    "compare_treatment_effect": ("比较治疗效应", "efficacy", 1),
    "check_randomization_balance": ("检查随机化平衡", "statistics", 1),
    "analyze_missingness": ("分析结局缺失", "data_quality", 1),
    "profile_sites": ("分析研究中心构成", "site_quality", 1),
    "inspect_treatment_exposure": ("检查治疗暴露", "exposure", 1),
    "inspect_protocol_quality": ("检查方案与处理质量", "site_quality", 1),
    "submit_clinical_conclusion": ("提交临床结论审批", "governance", 1),
    "inspect_safety_summary": ("比较治疗组与对照组安全性事件比例", "safety", 1),
    "analyze_safety_trend": ("分析安全性趋势", "safety", 1),
    "analyze_visit_windows": ("分析访视窗口偏离", "data_quality", 1),
    "analyze_visit_missingness": ("按访视周分析缺失访视", "data_quality", 1),
    "inspect_data_quality": ("检查数据完整性", "data_quality", 1),
    "rank_sites": ("研究中心质量排名", "site_quality", 1),
    "build_subgroup_forest": ("生成亚组森林图聚合数据", "statistics", 1),
    "run_sensitivity_analysis": ("执行敏感性分析", "statistics", 1),
}

# Analytical meaning is separate from the concrete tool name. The planner asks for a reusable
# capability and the registry resolves it to a governed adapter supported by the published batch.
CAPABILITY_METADATA = {
    "search_clinical_metrics": ("discover_metric", ("discover",), (), ()),
    "inspect_trial": ("describe_population", ("discover", "describe"), (), ("site_id", "treatment_arm")),
    "compare_treatment_effect": ("compare_group_measure", ("compare",), ("treatment_effect", "week_12_improvement"), ("treatment_arm",)),
    "check_randomization_balance": ("compare_group_measure", ("compare",), ("baseline_score", "disease_duration_months"), ("treatment_arm",)),
    "analyze_missingness": ("assess_missingness", ("quality_check", "compare", "rank"), ("missing_rate", "week_12_improvement"), ("treatment_arm", "missingness_status", "region", "site_id", "age_band", "sex", "severity_band")),
    "profile_sites": ("stratify_measure", ("stratify", "compare", "rank"), ("treatment_effect", "week_12_improvement", "site_population"), ("site_id", "treatment_arm")),
    "inspect_treatment_exposure": ("assess_exposure", ("describe", "compare", "rank", "quality_check"), ("adherence_rate",), ("site_id", "region", "treatment_arm")),
    # The mart returns one row per site and treatment arm.  ``treatment_arm``
    # is therefore a valid requested dimension even though the argument
    # boundary uses ``group_by=site`` to retain both columns in the result.
    "inspect_protocol_quality": ("assess_protocol_quality", ("describe", "compare", "quality_check"), ("protocol_deviation", "temperature_excursion"), ("site_id", "region", "treatment_arm")),
    "inspect_safety_summary": ("summarize_safety", ("describe", "compare"), ("adverse_event_rate", "serious_adverse_event_rate"), ("treatment_arm",)),
    "analyze_safety_trend": ("trend_group_measure", ("trend", "compare"), ("serious_adverse_event_rate",), ("time", "treatment_arm")),
    "analyze_visit_windows": ("assess_visit_window", ("quality_check", "compare", "rank"), ("visit_window_deviation",), ("visit", "treatment_arm")),
    # Visit missingness is a time series: the governed adapter returns one row per visit week.
    # Accepting ``trend``/``time`` here lets an LLM express a natural “does it rise over time?”
    # question without falling back to the unrelated Week-12-only missingness tool.
    "analyze_visit_missingness": ("assess_missingness", ("quality_check", "compare", "trend", "rank"), ("missing_rate", "missed_visit_rate"), ("visit", "time", "treatment_arm")),
    "inspect_data_quality": ("assess_data_quality", ("quality_check", "describe"), ("missing_rate",), ("treatment_arm",)),
    "rank_sites": ("rank_groups", ("rank",), ("site_quality_burden",), ("site_id", "region")),
    "build_subgroup_forest": ("stratify_measure", ("stratify", "compare"), ("treatment_effect", "week_12_improvement"), ("subgroup", "region", "site_id", "age_band", "sex", "severity_band", "treatment_arm")),
    "run_sensitivity_analysis": ("sensitivity_analysis", ("sensitivity",), ("treatment_effect", "week_12_improvement"), ("analysis_method", "treatment_arm")),
    "submit_clinical_conclusion": ("submit_conclusion", ("describe",), (), ()),
}

# A governed adapter can expose more than one analytical meaning.  The forest query is both a
# stratified result and a group comparison when the requested dimension is a region; keeping the
# alias here lets an LLM plan either vocabulary without adding a duplicate SQL tool.
CAPABILITY_ALIASES = {
    "build_subgroup_forest": ("compare_group_measure",),
    # Providers often name the requested metric/capability instead of the
    # registry's analytical verb.  These aliases preserve semantic matching
    # without exposing a new SQL surface or weakening tool governance.
    "check_randomization_balance": ("randomization_balance", "baseline_balance"),
    "analyze_visit_windows": ("visit_window_deviation", "assess_visit_window"),
}

# Hard-gate domains: a tool is removed from the planner when these are not published.
#
# ``inspect_treatment_exposure`` intentionally gates on ``DM`` only. The exposure mart is a
# supporting (degradation) source: when the selected batch does not publish ``EX`` the governed
# adapter returns an explicit "domain not provided by selected published batch" limitation instead
# of fabricating rows. Hiding the tool entirely would make that missing capability invisible to the
# planner, so the runtime reports it as a data gap rather than silently skipping the check.
REQUIRED_DOMAINS = {
    "compare_treatment_effect": frozenset({"DM", "ADSL", "ADEFF"}),
    "check_randomization_balance": frozenset({"DM", "ADSL"}),
    "analyze_missingness": frozenset({"ADSL", "ADEFF"}),
    "build_subgroup_forest": frozenset({"DM", "ADSL", "ADEFF"}),
    "run_sensitivity_analysis": frozenset({"DM", "ADSL", "ADEFF"}),
    "analyze_safety_trend": frozenset({"DM", "AE"}),
    "inspect_safety_summary": frozenset({"DM", "AE"}),
    "inspect_treatment_exposure": frozenset({"DM"}),
    "analyze_visit_windows": frozenset({"DM", "ADEFF"}),
    "analyze_visit_missingness": frozenset({"DM", "ADEFF"}),
}

# Supporting (degradation) domains: usable when present, but their absence must surface as an
# explicit data gap instead of removing the tool or inventing data. Only domains that the versioned
# clinical domain registry (``semantic/clinical_domains.yml``) actually declares may appear here.
SUPPORTING_DOMAINS = {
    "inspect_treatment_exposure": frozenset({"EX"}),
    "inspect_protocol_quality": frozenset({"LB"}),
}

class _Strict(BaseModel): model_config=ConfigDict(extra="forbid")
class MetricArguments(_Strict): query:str=Field(min_length=1,max_length=200)
class TrialArguments(_Strict): trial_id:str
class TrialSubgroupArguments(TrialArguments): subgroup:ClinicalSubgroup|None=None
class MissingnessArguments(TrialSubgroupArguments):
    timepoint: str = "week_12"
    group_by: Literal["treatment_arm", "region", "site_id", "age_band", "sex", "severity_band"] = "treatment_arm"

ARGUMENT_MODELS={
    "search_clinical_metrics":MetricArguments,
    "inspect_trial":TrialArguments,
    "compare_treatment_effect":CompareTreatmentEffectRequest,
    "check_randomization_balance":TrialSubgroupArguments,
    "analyze_missingness":MissingnessArguments,
    "profile_sites":TrialSubgroupArguments,
    "inspect_treatment_exposure":InspectTreatmentExposureRequest,
    "inspect_protocol_quality":InspectProtocolQualityRequest,
    "inspect_safety_summary":TrialArguments,
    "analyze_safety_trend":TrialSubgroupArguments,
    "analyze_visit_windows":TrialSubgroupArguments,
    "analyze_visit_missingness":TrialSubgroupArguments,
    "inspect_data_quality":TrialSubgroupArguments,
    "rank_sites":GroupedTrialRequest,
    "build_subgroup_forest":SubgroupForestRequest,
    "run_sensitivity_analysis":SensitivityAnalysisRequest,
}


@dataclass(frozen=True)
class ClinicalToolPlugin:
    name: str
    description: str
    category: str
    query_cost: int
    handler: Callable[..., Any]
    argument_model: type[BaseModel] | None = None
    required_domains: frozenset[str] = frozenset()
    supporting_domains: frozenset[str] = frozenset()
    capability: str = ""
    operations: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    capability_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if SAFE_PLUGIN_NAME.fullmatch(self.name) is None:
            raise ValueError("invalid clinical tool plugin name")
        if SAFE_PLUGIN_NAME.fullmatch(self.category) is None:
            raise ValueError("invalid clinical tool plugin category")
        if not self.description.strip():
            raise ValueError("clinical tool plugin description is required")
        if self.query_cost < 1:
            raise ValueError("clinical tool plugin query cost must be positive")
        if not callable(self.handler):
            raise ValueError("clinical tool plugin handler must be callable")
        if self.supporting_domains & self.required_domains:
            raise ValueError("supporting domains cannot also be hard-gated domains")

    def missing_domains(self, published: set[str]) -> frozenset[str]:
        """Supporting domains this batch does not publish; recorded as investigation limitations."""

        return self.supporting_domains - published


class ClinicalToolRegistry:
    domain = "clinical_trial"

    def __init__(self, tools: ClinicalTools | None = None) -> None:
        self._registry: dict[str, ClinicalToolPlugin] = {}
        self._frozen = False
        if tools is not None:
            for name in CLINICAL_TOOL_NAMES:
                description, category, query_cost = DEFAULT_METADATA[name]
                capability, operations, measures, dimensions = CAPABILITY_METADATA[name]
                self.register(
                    ClinicalToolPlugin(
                        name=name,
                        description=description,
                        category=category,
                        query_cost=query_cost,
                        handler=getattr(tools, name),
                        argument_model=ARGUMENT_MODELS.get(name),
                        required_domains=REQUIRED_DOMAINS.get(name, frozenset({"DM"}) if name != "search_clinical_metrics" and name != "submit_clinical_conclusion" else frozenset()),
                        supporting_domains=SUPPORTING_DOMAINS.get(name, frozenset()),
                        capability=capability,
                        operations=operations,
                        measures=measures,
                        dimensions=dimensions,
                        capability_aliases=CAPABILITY_ALIASES.get(name, ()),
                    )
                )
            self.freeze()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._registry)

    def register(self, plugin: ClinicalToolPlugin) -> None:
        if self._frozen:
            raise RuntimeError("clinical tool registry is frozen")
        if plugin.name in self._registry:
            raise ValueError(f"Clinical tool {plugin.name} is already registered")
        self._registry[plugin.name] = plugin

    def freeze(self) -> None:
        self._frozen = True

    def describe(self, category: str | None = None) -> tuple[ClinicalToolPlugin, ...]:
        return tuple(
            plugin
            for plugin in self._registry.values()
            if category is None or plugin.category == category
        )

    def get(self, name: str) -> Callable[..., Any]:
        try:
            return self._registry[name].handler
        except KeyError as exc:
            raise KeyError(f"Unknown clinical tool: {name}") from exc

    def plugin(self, name: str) -> ClinicalToolPlugin:
        try:
            return self._registry[name]
        except KeyError as exc:
            raise KeyError(f"Unknown clinical tool: {name}") from exc

    @property
    def domain_universe(self) -> frozenset[str]:
        """Every domain referenced by the registered tools, whether hard-gated or supporting."""

        return frozenset().union(*(p.required_domains | p.supporting_domains for p in self._registry.values()))

    def unavailable_domains(self, domains: set[str]) -> tuple[str, ...]:
        return tuple(sorted(self.domain_universe - domains))

    def capability_specs(self, domains: set[str]) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "tool": plugin.name,
                "capability": plugin.capability,
                "capability_aliases": plugin.capability_aliases,
                "description": plugin.description,
                "operations": plugin.operations,
                "measures": plugin.measures,
                "dimensions": plugin.dimensions,
                "required_domains": tuple(sorted(plugin.required_domains)),
                "arguments": plugin.argument_model.model_json_schema() if plugin.argument_model else {"type": "object", "properties": {}},
            }
            for plugin in self._registry.values()
            if plugin.name != "submit_clinical_conclusion" and plugin.required_domains.issubset(domains)
        )

    def match_capability(
        self,
        capability: str,
        operation: str,
        measure: str | None,
        dimensions: tuple[str, ...],
        domains: set[str],
    ) -> tuple[ClinicalToolPlugin, ...]:
        requested_dimensions = set(dimensions)
        return tuple(
            plugin
            for plugin in self._registry.values()
            if plugin.name != "submit_clinical_conclusion"
            and (plugin.capability == capability or capability in plugin.capability_aliases)
            and operation in plugin.operations
            and (measure is None or measure in plugin.measures)
            and requested_dimensions.issubset(set(plugin.dimensions))
            and plugin.required_domains.issubset(domains)
        )

    def available(self, intent: str, domains: set[str]) -> tuple[ClinicalToolPlugin, ...]:
        general={"discovery","trial"}
        aliases={
            "efficacy":{"efficacy","statistics","data_quality","site_quality","exposure","trial","discovery"},
            "safety":{"safety","trial","discovery","data_quality","exposure"},
            "exposure":{"exposure","trial","discovery","data_quality"},
            "site_quality":{"site_quality","trial","discovery","data_quality","exposure"},
            "data_quality":{"data_quality","site_quality","trial","discovery","exposure"},
            "general":set(DEFAULT_METADATA[name][1] for name in DEFAULT_METADATA),
        }
        categories=aliases.get(intent,general)
        return tuple(p for p in self._registry.values() if p.name!="submit_clinical_conclusion" and p.category in categories and p.required_domains.issubset(domains))

    def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        try: plugin=self._registry[name]
        except KeyError as exc: raise KeyError(f"Unknown clinical tool: {name}") from exc
        if name=="submit_clinical_conclusion": raise ValueError("conclusion submission is not a query tool")
        try: validated=plugin.argument_model.model_validate(arguments) if plugin.argument_model else None
        except ValidationError as exc: raise ValueError(f"invalid arguments for {name}: {exc}") from exc
        if name in {
            "compare_treatment_effect",
            "inspect_treatment_exposure",
            "inspect_protocol_quality",
            # SensitivityAnalysisRequest is deliberately passed as one validated
            # object.  The handler owns the policy boundary (including the
            # allow-listed analysis method) and must not receive a partially
            # reconstructed **kwargs payload.
            "run_sensitivity_analysis",
        }:
            result = plugin.handler(validated)
        else:
            result = plugin.handler(**validated.model_dump(exclude_none=True)) if validated else plugin.handler()
        if isinstance(result, ClinicalToolResult):
            return result
        if name == "search_clinical_metrics" and isinstance(result, list) and all(isinstance(row, dict) for row in result):
            return ClinicalToolResult(
                tool=name,
                source="semantic/clinical_metrics.yml",
                rows=result,
            )
        raise TypeError(f"clinical tool {name} returned an invalid result contract")

