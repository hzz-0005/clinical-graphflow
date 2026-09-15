from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.clinical.canonical import CanonicalClinicalQuery


class ClinicalDialect(StrEnum):
    POSTGRESQL = "postgresql"
    SNOWFLAKE = "snowflake"
    BIGQUERY = "bigquery"
    DATABRICKS = "databricks"


class ClinicalOperation(StrEnum):
    INSPECT_TRIAL = "inspect_trial"
    TREATMENT_EFFECT = "compare_treatment_effect"
    RANDOMIZATION_BALANCE = "check_randomization_balance"
    MISSINGNESS = "analyze_missingness"
    PROFILE_SITES = "profile_sites"
    TREATMENT_EXPOSURE = "inspect_treatment_exposure"
    PROTOCOL_QUALITY = "inspect_protocol_quality"
    SAFETY_TREND = "analyze_safety_trend"
    VISIT_WINDOWS = "analyze_visit_windows"
    DATA_QUALITY = "inspect_data_quality"
    RANK_SITES = "rank_sites"
    SUBGROUP_FOREST = "build_subgroup_forest"
    SENSITIVITY = "run_sensitivity_analysis"


class CompiledClinicalQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    dialect: ClinicalDialect
    operation: ClinicalOperation
    sql: str
    bindings: tuple[Any, ...]


_OPERATION_MODEL: dict[ClinicalOperation, tuple[str, tuple[str, ...]]] = {
    ClinicalOperation.INSPECT_TRIAL: ("mart_trial_population", ("trial_id",)),
    ClinicalOperation.TREATMENT_EFFECT: ("mart_week12_efficacy", ("arm",)),
    ClinicalOperation.RANDOMIZATION_BALANCE: ("mart_randomization_balance", ("arm",)),
    ClinicalOperation.MISSINGNESS: ("mart_missingness", ("arm", "missing_reason")),
    ClinicalOperation.PROFILE_SITES: ("mart_week12_efficacy", ("site_id", "region", "arm")),
    ClinicalOperation.TREATMENT_EXPOSURE: ("mart_treatment_exposure", ("site_id", "arm")),
    ClinicalOperation.PROTOCOL_QUALITY: ("mart_site_quality", ("site_id", "region", "arm")),
    ClinicalOperation.SAFETY_TREND: ("mart_safety_trend", ("event_month", "arm")),
    ClinicalOperation.VISIT_WINDOWS: ("mart_visit_windows", ("site_id", "region", "arm", "visit_week")),
    ClinicalOperation.DATA_QUALITY: ("mart_missingness", ("arm",)),
    ClinicalOperation.RANK_SITES: ("mart_site_quality", ("site_id", "region")),
    ClinicalOperation.SUBGROUP_FOREST: ("mart_week12_efficacy", ("region", "arm")),
    ClinicalOperation.SENSITIVITY: ("mart_week12_efficacy", ("arm",)),
}


class ClinicalDialectCompiler:
    """Compile allowlisted canonical operations; never accepts caller-supplied SQL."""

    def __init__(self, dialect: ClinicalDialect) -> None:
        self._dialect = dialect

    def compile(
        self, operation: ClinicalOperation, query: CanonicalClinicalQuery
    ) -> CompiledClinicalQuery:
        if not isinstance(operation, ClinicalOperation):
            raise ValueError("clinical operation is not allowlisted")
        model, groups = _OPERATION_MODEL[operation]
        quote = '"' if self._dialect in {
            ClinicalDialect.POSTGRESQL,
            ClinicalDialect.SNOWFLAKE,
        } else "`"

        def identifier(value: str) -> str:
            return ".".join(f"{quote}{part}{quote}" for part in value.split("."))

        bindings: list[Any] = [query.trial_id]
        clauses = [f"{identifier('trial_id')} = {self._placeholder(1)}"]
        if query.subgroup is not None:
            bindings.append(query.subgroup.value)
            clauses.append(
                f"{identifier(query.subgroup.dimension)} = {self._placeholder(2)}"
            )
        group_sql = ", ".join(identifier(item) for item in groups)
        relation = identifier(f"analytics_clinical_marts.{model}")
        sql = (
            f"select {group_sql}, count(*) as {identifier('sample_size')} "
            f"from {relation} where {' and '.join(clauses)} group by {group_sql}"
        )
        return CompiledClinicalQuery(
            dialect=self._dialect,
            operation=operation,
            sql=sql,
            bindings=tuple(bindings),
        )

    def _placeholder(self, index: int) -> str:
        if self._dialect == ClinicalDialect.POSTGRESQL:
            return "%s"
        if self._dialect == ClinicalDialect.SNOWFLAKE:
            return "?"
        if self._dialect == ClinicalDialect.BIGQUERY:
            return f"@p{index}"
        return f":p{index}"

