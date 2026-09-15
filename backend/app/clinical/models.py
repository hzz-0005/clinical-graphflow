from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ArmSummary(BaseModel):
    """Aggregate outcome and data-quality summary for one randomized arm."""

    model_config = ConfigDict(allow_inf_nan=False)

    arm: Literal["control", "treatment"]
    sample_size: int = Field(ge=0)
    mean_improvement: float
    variance: float = Field(ge=0)
    missing_rate: float = Field(ge=0, le=1)


class ClinicalAnalysisSummary(BaseModel):
    """Aggregate-only input accepted by the statistical guardrail engine."""

    model_config = ConfigDict(allow_inf_nan=False)

    control: ArmSummary
    treatment: ArmSummary
    balance_smds: dict[str, float] = Field(default_factory=dict)
    maximum_site_share: float = Field(ge=0, le=1)
    site_stratified_checked: bool = False
    tested_subgroups: int = Field(default=1, ge=1)
    exposure_checked: bool
    protocol_quality_checked: bool
    itt_primary: bool
    causal_language_requested: bool = False
    temporal_order_checked: bool = False
    alternatives_checked: bool = False
    evidence_ids: dict[str, str] = Field(default_factory=dict)

    @field_validator("balance_smds")
    @classmethod
    def reject_non_finite_smds(cls, values: dict[str, float]) -> dict[str, float]:
        # Pydantic does not apply allow_inf_nan to arbitrary mapping values.
        import math

        if any(not math.isfinite(value) for value in values.values()):
            raise ValueError("balance SMD values must be finite")
        return values


class GuardrailCheck(BaseModel):
    """One deterministic, evidence-linked verification decision."""

    model_config = ConfigDict(allow_inf_nan=False)

    name: str
    passed: bool
    value: Any
    threshold: str
    message: str
    evidence_id: str | None = None

    @field_validator("value")
    @classmethod
    def reject_non_finite_nested_values(cls, value: Any) -> Any:
        def validate(item: Any) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("guardrail values must contain only finite numbers")
            if isinstance(item, Mapping):
                for nested in item.values():
                    validate(nested)
            elif isinstance(item, Sequence) and not isinstance(
                item, (str, bytes, bytearray)
            ):
                for nested in item:
                    validate(nested)

        validate(value)
        return value


class ClinicalVerificationReport(BaseModel):
    """Ordered statistical verification result safe for API serialization."""

    model_config = ConfigDict(allow_inf_nan=False)

    passed: bool
    checks: list[GuardrailCheck]
    flags: list[str] = Field(default_factory=list)
    effect_estimate: float
    standard_error: float
    ci_lower: float
    ci_upper: float

    def get_check(self, name: str) -> GuardrailCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(name)

