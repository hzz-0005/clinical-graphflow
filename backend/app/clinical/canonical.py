from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SAFE_CLINICAL_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$"


class ClinicalEndpoint(StrEnum):
    PRIMARY_EFFICACY = "primary_efficacy"
    SAFETY = "safety"
    TREATMENT_EXPOSURE = "treatment_exposure"


class ClinicalPopulation(StrEnum):
    INTENTION_TO_TREAT = "intention_to_treat"
    PER_PROTOCOL = "per_protocol"
    SAFETY = "safety"


class ClinicalTimepoint(StrEnum):
    WEEK_12 = "week_12"


class ClinicalSubgroup(BaseModel):
    """A governed dimension/value pair, independent of a database column API."""

    model_config = ConfigDict(frozen=True)
    dimension: Literal["region", "site_id", "age_band", "sex", "severity_band"]
    value: str = Field(pattern=SAFE_CLINICAL_IDENTIFIER)


class CanonicalClinicalQuery(BaseModel):
    """Stable clinical query contract consumed by every data adapter."""

    model_config = ConfigDict(frozen=True)
    trial_id: str = Field(pattern=SAFE_CLINICAL_IDENTIFIER)
    endpoint: ClinicalEndpoint = ClinicalEndpoint.PRIMARY_EFFICACY
    population: ClinicalPopulation = ClinicalPopulation.INTENTION_TO_TREAT
    timepoint: ClinicalTimepoint = ClinicalTimepoint.WEEK_12
    subgroup: ClinicalSubgroup | None = None
    source_batch_id: str | None = Field(default=None, exclude=True, pattern=SAFE_CLINICAL_IDENTIFIER)

    @field_validator("trial_id")
    @classmethod
    def reject_unsafe_trial_id(cls, value: str) -> str:
        if re.fullmatch(SAFE_CLINICAL_IDENTIFIER, value) is None:
            raise ValueError("unsafe clinical trial identifier")
        return value


class CanonicalAggregateResult(BaseModel):
    """Aggregate-only result shared by tools and every database adapter."""

    source: str
    sql: str
    params: tuple[Any, ...] = ()
    rows: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

