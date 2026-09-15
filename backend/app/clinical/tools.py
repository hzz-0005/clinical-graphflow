from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.semantic.metrics import ClinicalMetricRepository
from app.clinical.adapters.base import ClinicalAnalyticsAdapter
from app.clinical.canonical import CanonicalAggregateResult, CanonicalClinicalQuery, ClinicalSubgroup

MINIMUM_CELL_SIZE = 10
SAFE_VALUE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$"

#: Marts that carry ``source_batch_id`` and can therefore serve a bound published batch.
BATCH_BOUND_OPERATIONS = frozenset(
    {
        "inspect_trial",
        "compare_treatment_effect",
        "check_randomization_balance",
        "analyze_missingness",
        "profile_sites",
        "build_subgroup_forest",
        "run_sensitivity_analysis",
        "inspect_data_quality",
    }
)

#: Domains each non-batch-bound operation needs before it can be attempted at all.
BATCH_UNBOUND_REQUIREMENTS = {
    "inspect_safety_summary": frozenset({"AE"}),
    "analyze_safety_trend": frozenset({"AE"}),
    "analyze_visit_windows": frozenset({"ADEFF"}),
    "rank_sites": frozenset({"SITE_QUALITY"}),
}


class ClinicalDataScope(BaseModel):
    """Immutable authorization boundary applied before any clinical query."""

    model_config = ConfigDict(frozen=True)

    trial_ids: frozenset[str] = Field(min_length=1)
    regions: frozenset[str] = Field(default_factory=frozenset)
    site_ids: frozenset[str] = Field(default_factory=frozenset)
    published_batch_id: str | None = Field(default=None, pattern=SAFE_VALUE_PATTERN)
    published_domains: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("trial_ids", "regions", "site_ids")
    @classmethod
    def validate_scope_values(cls, values: frozenset[str]) -> frozenset[str]:
        if any(re.fullmatch(SAFE_VALUE_PATTERN, value) is None for value in values):
            raise ValueError("clinical scope contains an unsafe identifier")
        return values


class TrialRequest(BaseModel):
    trial_id: str = Field(pattern=SAFE_VALUE_PATTERN)


class CompareTreatmentEffectRequest(TrialRequest):
    subgroup: ClinicalSubgroup | None = None
    timepoint: Literal["week_12"] = "week_12"


class InspectTreatmentExposureRequest(TrialRequest):
    site_id: str | None = Field(default=None, pattern=SAFE_VALUE_PATTERN)
    group_by: Literal["site", "region"] = "site"


class SensitivityAnalysisRequest(TrialRequest):
    """Parameters for a governed, pre-defined sensitivity calculation.

    The method is an allow-listed control, not free SQL or a user supplied predicate.  Keeping it
    explicit makes the evidence chain say exactly which population was re-analysed.
    """

    subgroup: ClinicalSubgroup | None = None
    analysis_method: Literal[
        "leave_one_site_out",
        "exclude_highest_quality_burden",
        "exclude_major_protocol_deviation",
    ] = "leave_one_site_out"


class InspectProtocolQualityRequest(TrialRequest):
    # A plan may ask for the quality profile before it knows which site to focus on.
    # ``None`` means all authorized sites; ``group_by=region`` is the privacy-safe fallback
    # when site cells are too small.
    site_id: str | None = Field(default=None, pattern=SAFE_VALUE_PATTERN)
    group_by: Literal["site", "region", "treatment_arm"] = "site"
    # A quality mart can expose several measures at different grains.  Carrying the
    # requested measure to the adapter lets it refuse an invalid attribution instead of
    # duplicating a site-only event into every treatment arm.
    measure: Literal["protocol_deviation", "temperature_excursion", "site_quality_burden"] | None = None


class GroupedTrialRequest(TrialRequest):
    subgroup: ClinicalSubgroup | None = None
    group_by: Literal["site", "region"] = "site"


class SubgroupForestRequest(TrialRequest):
    subgroup: ClinicalSubgroup | None = None
    group_by: Literal["region", "site_id", "age_band", "sex", "severity_band"] = "region"


class ClinicalToolResult(BaseModel):
    tool: str
    source: str
    sql: str | None = None
    params: tuple[Any, ...] = ()
    rows: list[dict[str, Any]] = Field(default_factory=list)
    minimum_cell_size: int = MINIMUM_CELL_SIZE
    warnings: list[str] = Field(default_factory=list)


class ClinicalConclusionSubmission(BaseModel):
    status: Literal["pending_approval"] = "pending_approval"
    claims: list[str]
    evidence_ids: list[str]
    message: str = "Clinical conclusions require human approval before publication."


class ClinicalTools:
    """Nine governed clinical operations; no free-form SQL enters this class."""

    def __init__(
        self,
        metrics: ClinicalMetricRepository,
        adapter: ClinicalAnalyticsAdapter,
        scope: ClinicalDataScope,
    ) -> None:
        self._metrics = metrics
        self._adapter = adapter
        self._scope = scope

    def search_clinical_metrics(self, query: str) -> list[dict[str, Any]]:
        if not query.strip() or len(query) > 200:
            raise ValueError("metric search query must contain 1 to 200 characters")
        return [match.model_dump(mode="json") for match in self._metrics.search(query)]

    def inspect_trial(self, trial_id: str) -> ClinicalToolResult:
        request = TrialRequest(trial_id=trial_id)
        self._authorize_trial(request.trial_id)
        return self._run("inspect_trial", self._adapter.inspect_trial(request.trial_id, self._scope.published_batch_id))

    def compare_treatment_effect(
        self, request: CompareTreatmentEffectRequest
    ) -> ClinicalToolResult:
        self._authorize(request.trial_id, request.subgroup)
        query = self._canonical(request.trial_id, request.subgroup)
        return self._run("compare_treatment_effect", self._adapter.compare_treatment_effect(query))

    def check_randomization_balance(
        self, trial_id: str, subgroup: ClinicalSubgroup | None = None
    ) -> ClinicalToolResult:
        request = TrialRequest(trial_id=trial_id)
        self._authorize(request.trial_id, subgroup)
        query = self._canonical(request.trial_id, subgroup)
        return self._run("check_randomization_balance", self._adapter.check_randomization_balance(query))

    def analyze_missingness(
        self,
        trial_id: str,
        subgroup: ClinicalSubgroup | None = None,
        timepoint: Literal["week_12"] = "week_12",
        group_by: Literal["treatment_arm", "region", "site_id", "age_band", "sex", "severity_band"] = "treatment_arm",
    ) -> ClinicalToolResult:
        request = CompareTreatmentEffectRequest(
            trial_id=trial_id, subgroup=subgroup, timepoint=timepoint
        )
        self._authorize(request.trial_id, request.subgroup)
        query = self._canonical(request.trial_id, request.subgroup)
        return self._run("analyze_missingness", self._adapter.analyze_missingness(query, group_by))

    def profile_sites(
        self, trial_id: str, subgroup: ClinicalSubgroup | None = None
    ) -> ClinicalToolResult:
        request = TrialRequest(trial_id=trial_id)
        self._authorize(request.trial_id, subgroup)
        query = self._canonical(request.trial_id, subgroup)
        return self._run("profile_sites", self._adapter.profile_sites(query))

    def inspect_treatment_exposure(
        self, request: InspectTreatmentExposureRequest
    ) -> ClinicalToolResult:
        if request.site_id:
            self._authorize_site(request.trial_id, request.site_id)
        else:
            self._authorize_trial(request.trial_id)
        return self._run("inspect_treatment_exposure", self._adapter.inspect_treatment_exposure(request.trial_id, request.site_id, self._scope.published_batch_id, self._scope.published_domains, request.group_by))

    def inspect_protocol_quality(
        self, request: InspectProtocolQualityRequest
    ) -> ClinicalToolResult:
        if request.site_id:
            self._authorize_site(request.trial_id, request.site_id)
        else:
            self._authorize_trial(request.trial_id)
        return self._run(
            "inspect_protocol_quality",
            self._adapter.inspect_protocol_quality(
                request.trial_id,
                request.site_id,
                self._scope.published_batch_id,
                self._scope.published_domains,
                request.group_by,
                request.measure,
            ),
        )

    def analyze_safety_trend(
        self, trial_id: str, subgroup: ClinicalSubgroup | None = None
    ) -> ClinicalToolResult:
        return self._run_canonical("analyze_safety_trend", trial_id, subgroup)

    def inspect_safety_summary(self, trial_id: str) -> ClinicalToolResult:
        """Compare trial-level adverse-event proportions by treatment arm.

        This is deliberately trial-level: the summary mart has no region/site grain, so the
        tool does not accept a subgroup that would produce an invalid or misleading query.
        """

        request = TrialRequest(trial_id=trial_id)
        self._authorize_trial(request.trial_id)
        return self._run_canonical("inspect_safety_summary", request.trial_id, None)

    def analyze_visit_windows(
        self, trial_id: str, subgroup: ClinicalSubgroup | None = None
    ) -> ClinicalToolResult:
        return self._run_canonical("analyze_visit_windows", trial_id, subgroup)

    def analyze_visit_missingness(
        self, trial_id: str, subgroup: ClinicalSubgroup | None = None
    ) -> ClinicalToolResult:
        result=self._run_canonical("analyze_visit_windows",trial_id,subgroup)
        return result.model_copy(update={"tool":"analyze_visit_missingness"})

    def inspect_data_quality(
        self, trial_id: str, subgroup: ClinicalSubgroup | None = None
    ) -> ClinicalToolResult:
        return self._run_canonical("inspect_data_quality", trial_id, subgroup)

    def rank_sites(
        self, trial_id: str, subgroup: ClinicalSubgroup | None = None, group_by: Literal["site", "region"] = "site"
    ) -> ClinicalToolResult:
        request = GroupedTrialRequest(trial_id=trial_id, subgroup=subgroup, group_by=group_by)
        self._authorize(request.trial_id, request.subgroup)
        return self._run(
            "rank_sites",
            self._adapter.rank_sites(
                self._canonical(request.trial_id, request.subgroup),
                request.group_by,
                self._scope.published_batch_id,
                self._scope.published_domains,
            ),
        )

    def build_subgroup_forest(
        self,
        trial_id: str,
        subgroup: ClinicalSubgroup | None = None,
        group_by: Literal["region", "site_id", "age_band", "sex", "severity_band"] = "region",
    ) -> ClinicalToolResult:
        request = SubgroupForestRequest(trial_id=trial_id, subgroup=subgroup, group_by=group_by)
        self._authorize(request.trial_id, request.subgroup)
        return self._run(
            "build_subgroup_forest",
            self._adapter.build_subgroup_forest(
                self._canonical(request.trial_id, request.subgroup), request.group_by
            ),
        )

    def run_sensitivity_analysis(
        self,
        request: SensitivityAnalysisRequest | str,
        subgroup: ClinicalSubgroup | None = None,
        analysis_method: Literal[
            "leave_one_site_out",
            "exclude_highest_quality_burden",
            "exclude_major_protocol_deviation",
        ] = "leave_one_site_out",
    ) -> ClinicalToolResult:
        """Run a governed sensitivity analysis.

        The registry passes a validated :class:`SensitivityAnalysisRequest`.  The string form is
        retained as a small backwards-compatible adapter for callers of the original public
        ``run_sensitivity_analysis(trial_id, subgroup)`` helper; both forms enter the same
        validation and authorization path before any SQL is executed.
        """

        if isinstance(request, str):
            request = SensitivityAnalysisRequest(
                trial_id=request,
                subgroup=subgroup,
                analysis_method=analysis_method,
            )
        self._authorize(request.trial_id, request.subgroup)
        query = self._canonical(request.trial_id, request.subgroup)
        if self._scope.published_batch_id:
            # The currently published efficacy mart is batch-bound, but participant-level
            # protocol flags are not yet published as a batch-bound domain.  Refuse to mix them.
            return self._run(
                "run_sensitivity_analysis",
                self._adapter.empty_for_unbound_mart(
                    "run_sensitivity_analysis", self._scope.published_batch_id
                ),
            )
        return self._run(
            "run_sensitivity_analysis",
            self._adapter.run_sensitivity_analysis(query, request.analysis_method),
        )

    def submit_clinical_conclusion(
        self, claims: list[str], evidence_ids: list[str]
    ) -> ClinicalConclusionSubmission:
        if not claims or not evidence_ids:
            raise ValueError("clinical conclusions require claims and evidence IDs")
        if any(re.fullmatch(r"E\d{2,}", item) is None for item in evidence_ids):
            raise ValueError("invalid clinical evidence ID")
        return ClinicalConclusionSubmission(claims=claims, evidence_ids=evidence_ids)

    def _authorize_trial(self, trial_id: str) -> None:
        if trial_id not in self._scope.trial_ids:
            raise ValueError(f"Trial {trial_id} is outside authorized clinical scope")

    def _run_canonical(
        self, operation: str, trial_id: str, subgroup: ClinicalSubgroup | None
    ) -> ClinicalToolResult:
        request = TrialRequest(trial_id=trial_id)
        self._authorize(request.trial_id, subgroup)
        query = self._canonical(request.trial_id, subgroup)
        handler = getattr(self._adapter, operation)
        if self._scope.published_batch_id and operation not in BATCH_BOUND_OPERATIONS:
            needed = BATCH_UNBOUND_REQUIREMENTS.get(operation, frozenset())
            if needed - self._scope.published_domains:
                return self._run(operation, self._adapter.empty_for_unavailable_batch(operation, self._scope.published_batch_id))
            return self._run(operation, self._adapter.empty_for_unbound_mart(operation, self._scope.published_batch_id))
        return self._run(operation, handler(query))

    def _authorize_site(self, trial_id: str, site_id: str) -> None:
        self._authorize_trial(trial_id)
        if self._scope.site_ids and site_id not in self._scope.site_ids:
            raise ValueError(f"Site {site_id} is outside authorized clinical scope")

    def _authorize(
        self, trial_id: str, subgroup: ClinicalSubgroup | None
    ) -> None:
        self._authorize_trial(trial_id)
        if subgroup is None:
            return
        if (
            subgroup.dimension == "region"
            and self._scope.regions
            and subgroup.value not in self._scope.regions
        ):
            raise ValueError(f"Region {subgroup.value} is outside authorized clinical scope")
        if subgroup.dimension == "site_id":
            self._authorize_site(trial_id, subgroup.value)

    def _canonical(self, trial_id: str, subgroup: ClinicalSubgroup | None) -> CanonicalClinicalQuery:
        return CanonicalClinicalQuery(trial_id=trial_id, subgroup=subgroup, source_batch_id=self._scope.published_batch_id)

    def _run(
        self, tool: str, result: CanonicalAggregateResult
    ) -> ClinicalToolResult:
        rows = [self._suppress_small_cell(row) for row in result.rows]
        warnings = list(result.warnings)
        warnings.extend(
            ["Cells below 10 participants were suppressed."]
            if any(row.get("suppressed") is True for row in rows)
            else []
        )
        return ClinicalToolResult(
            tool=tool,
            source=result.source,
            sql=result.sql,
            params=result.params,
            rows=rows,
            warnings=warnings,
        )

    @staticmethod
    def _suppress_small_cell(row: dict[str, Any]) -> dict[str, Any]:
        sample_keys = (
            "sample_size",
            "participant_count",
            "randomized_participants",
            "exposed_participants",
            "denominator",
        )
        sample_size = next(
            (int(row[key]) for key in sample_keys if row.get(key) is not None), None
        )
        output = dict(row)
        suppressed = sample_size is not None and sample_size < MINIMUM_CELL_SIZE
        if suppressed:
            identifiers = {
                "trial_id",
                "site_id",
                "region",
                # Subgroup queries expose the selected dimension through a
                # stable ``subgroup`` alias.  Keep that label visible even
                # when the measurement values are suppressed for a small
                # cell; otherwise the caller cannot tell which group was
                # withheld.
                "subgroup",
                "arm",
                "age_band",
                "sex",
                "severity_band",
                "missing_reason",
                *sample_keys,
            }
            for key in output:
                if key not in identifiers:
                    output[key] = None
        output["suppressed"] = suppressed
        return output

