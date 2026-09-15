from __future__ import annotations

import math
from collections.abc import Sequence

from app.clinical.models import (
    ClinicalAnalysisSummary,
    ClinicalVerificationReport,
    GuardrailCheck,
)

MINIMUM_ARM_SAMPLE_SIZE = 30
MAXIMUM_CI_WIDTH = 10.0
MAXIMUM_ABSOLUTE_SMD = 0.10
MAXIMUM_MISSING_RATE = 0.20
MAXIMUM_MISSING_RATE_GAP = 0.10
MAXIMUM_SITE_SHARE = 0.30
MAXIMUM_UNADJUSTED_SUBGROUPS = 5
Z_95 = 1.96


def continuous_smd(
    treatment_mean: float,
    control_mean: float,
    treatment_variance: float,
    control_variance: float,
) -> float:
    """Return a continuous standardized mean difference.

    Equal zero-variance groups are identical (0); unequal zero-variance groups
    are perfectly separated and deliberately return signed infinity.
    """

    pooled_standard_deviation = math.sqrt(
        (treatment_variance + control_variance) / 2
    )
    difference = treatment_mean - control_mean
    if pooled_standard_deviation == 0:
        if difference == 0:
            return 0.0
        return math.copysign(math.inf, difference)
    return difference / pooled_standard_deviation


def proportion_smd(treatment_rate: float, control_rate: float) -> float:
    """Return the standardized difference between two proportions."""

    pooled_rate = (treatment_rate + control_rate) / 2
    pooled_standard_deviation = math.sqrt(pooled_rate * (1 - pooled_rate))
    difference = treatment_rate - control_rate
    if pooled_standard_deviation == 0:
        if difference == 0:
            return 0.0
        return math.copysign(math.inf, difference)
    return difference / pooled_standard_deviation


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Adjust p-values with Holm's step-down family-wise error procedure."""

    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in p_values):
        raise ValueError("p-values must be finite and between 0 and 1")
    count = len(p_values)
    ranked = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [0.0] * count
    previous = 0.0
    for rank, (original_index, p_value) in enumerate(ranked):
        candidate = min(1.0, (count - rank) * p_value)
        previous = max(previous, candidate)
        adjusted[original_index] = previous
    return adjusted


def _evidence(summary: ClinicalAnalysisSummary, key: str) -> str | None:
    return summary.evidence_ids.get(key)


def _check(
    name: str,
    passed: bool,
    value: object,
    threshold: str,
    message: str,
    evidence_id: str | None = None,
) -> GuardrailCheck:
    return GuardrailCheck(
        name=name,
        passed=passed,
        value=value,
        threshold=threshold,
        message=message,
        evidence_id=evidence_id,
    )


def evaluate_guardrails(
    summary: ClinicalAnalysisSummary,
) -> ClinicalVerificationReport:
    """Apply the nine ordered clinical statistical and language safeguards."""

    effect_estimate = (
        summary.treatment.mean_improvement - summary.control.mean_improvement
    )
    interval_estimable = (
        summary.control.sample_size > 0 and summary.treatment.sample_size > 0
    )
    if interval_estimable:
        standard_error = math.sqrt(
            summary.treatment.variance / summary.treatment.sample_size
            + summary.control.variance / summary.control.sample_size
        )
        ci_lower = effect_estimate - Z_95 * standard_error
        ci_upper = effect_estimate + Z_95 * standard_error
    else:
        # Keep the API JSON-safe while the precision check records non-estimability.
        standard_error = 0.0
        ci_lower = effect_estimate
        ci_upper = effect_estimate

    interval_width = ci_upper - ci_lower
    minimum_sample_passed = min(
        summary.control.sample_size, summary.treatment.sample_size
    ) >= MINIMUM_ARM_SAMPLE_SIZE
    precision_passed = interval_estimable and interval_width <= MAXIMUM_CI_WIDTH

    maximum_smd = max(
        (abs(value) for value in summary.balance_smds.values()), default=0.0
    )
    balance_passed = maximum_smd <= MAXIMUM_ABSOLUTE_SMD
    missing_gap = abs(
        summary.control.missing_rate - summary.treatment.missing_rate
    )
    missingness_passed = (
        max(summary.control.missing_rate, summary.treatment.missing_rate)
        <= MAXIMUM_MISSING_RATE
        and missing_gap <= MAXIMUM_MISSING_RATE_GAP
    )
    site_passed = (
        summary.maximum_site_share <= MAXIMUM_SITE_SHARE
        or summary.site_stratified_checked
    )
    quality_passed = summary.exposure_checked and summary.protocol_quality_checked
    multiplicity_passed = summary.tested_subgroups <= MAXIMUM_UNADJUSTED_SUBGROUPS
    causal_passed = (
        not summary.causal_language_requested
        or (
            summary.temporal_order_checked
            and summary.alternatives_checked
            and quality_passed
        )
    )

    checks = [
        _check(
            "minimum_sample_size",
            minimum_sample_passed,
            {
                "control": summary.control.sample_size,
                "treatment": summary.treatment.sample_size,
            },
            ">=30 per arm",
            "Each randomized arm must contain at least 30 participants.",
            _evidence(summary, "effect"),
        ),
        _check(
            "confidence_interval_precision",
            precision_passed,
            {"width": interval_width, "level": 0.95},
            "95% CI width <=10 points",
            "The treatment effect must have an estimable, sufficiently precise interval.",
            _evidence(summary, "effect"),
        ),
        _check(
            "randomization_balance",
            balance_passed,
            {"maximum_absolute_smd": maximum_smd},
            "abs(SMD) <=0.10",
            "Baseline and site-composition balance must remain within the SMD threshold.",
            _evidence(summary, "balance"),
        ),
        _check(
            "missingness",
            missingness_passed,
            {
                "control_rate": summary.control.missing_rate,
                "treatment_rate": summary.treatment.missing_rate,
                "absolute_gap": missing_gap,
            },
            "each rate <=20%; gap <=10pp",
            "Week-12 missingness must remain bounded in both arms.",
            _evidence(summary, "missingness"),
        ),
        _check(
            "site_concentration",
            site_passed,
            {
                "maximum_site_share": summary.maximum_site_share,
                "site_stratified_checked": summary.site_stratified_checked,
            },
            "maximum site share <=30% or site-stratified analysis required",
            "Concentrated subgroups require an explicit site-stratified analysis.",
            _evidence(summary, "site"),
        ),
        _check(
            "exposure_and_deviations",
            quality_passed,
            {
                "exposure_checked": summary.exposure_checked,
                "protocol_quality_checked": summary.protocol_quality_checked,
            },
            "both checks required",
            "Exposure and protocol quality must be examined before interpretation.",
            _evidence(summary, "quality"),
        ),
        _check(
            "multiplicity",
            multiplicity_passed,
            {"tested_subgroups": summary.tested_subgroups},
            "<=5 unadjusted subgroup comparisons",
            "Broad subgroup searching requires multiplicity adjustment.",
            _evidence(summary, "multiplicity"),
        ),
        _check(
            "itt_first",
            summary.itt_primary,
            {"itt_primary": summary.itt_primary},
            "ITT must be primary",
            "The primary interpretation must use the intention-to-treat population.",
            _evidence(summary, "effect"),
        ),
        _check(
            "causal_language",
            causal_passed,
            {
                "requested": summary.causal_language_requested,
                "temporal_order_checked": summary.temporal_order_checked,
                "alternatives_checked": summary.alternatives_checked,
            },
            "temporal order and alternatives required",
            "Causal wording requires temporal ordering and alternative explanations.",
            _evidence(summary, "causal"),
        ),
    ]

    flag_by_check = {
        "minimum_sample_size": "small_sample",
        "confidence_interval_precision": "imprecise_estimate",
        "randomization_balance": "randomization_imbalance",
        "missingness": "high_missingness",
        "site_concentration": "site_concentration",
        "exposure_and_deviations": "quality_not_checked",
        "multiplicity": "multiple_comparisons",
        "itt_first": "non_itt_primary",
        "causal_language": "causal_language_unverified",
    }
    flags = [flag_by_check[check.name] for check in checks if not check.passed]

    return ClinicalVerificationReport(
        passed=all(check.passed for check in checks),
        checks=checks,
        flags=flags,
        effect_estimate=effect_estimate,
        standard_error=standard_error,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
    )

