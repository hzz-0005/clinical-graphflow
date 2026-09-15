"""Runtime data catalog for V15.

The investigation planner must see what is actually present in the selected data
version.  This module intentionally exposes metadata only: field names, observed
types, roles and row counts.  It never returns payload values and never accepts a
table name from a user or from an LLM.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Protocol

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field

from app.clinical.domain_registry import DomainRegistry


class CatalogQueryExecutor(Protocol):
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]: ...


class CatalogField(BaseModel):
    """One observed field with a conservative analytical role."""

    model_config = ConfigDict(frozen=True)

    name: str
    inferred_type: str
    role: str
    nullable: bool = True
    null_rate: float | None = Field(default=None, ge=0, le=1)
    observed_rows: int | None = Field(default=None, ge=0)
    aliases: tuple[str, ...] = ()


class CatalogDataset(BaseModel):
    """A metadata-only description of one published dataset or mart."""

    model_config = ConfigDict(frozen=True)

    dataset: str
    domain: str
    domain_version: str | None = None
    domain_status: str = "registered"
    is_registered: bool = True
    source: str
    grain: str | None = None
    record_count: int | None = Field(default=None, ge=0)
    fields: tuple[CatalogField, ...] = ()
    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()


class CatalogSnapshot(BaseModel):
    """The planner-facing catalog contract.

    ``measures`` and ``dimensions`` are candidates observed in real data.  They
    are not a promise that every registered tool can query every candidate; the
    capability validator remains the final authority.
    """

    model_config = ConfigDict(frozen=True)

    catalog_version: str = "V15"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    trial_id: str | None = None
    published_batch_id: str | None = None
    published_domains: tuple[str, ...] = ()
    datasets: tuple[CatalogDataset, ...] = ()
    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()


# These are code-owned relation contracts, not user input.  Keeping the map
# explicit prevents the metadata endpoint from becoming a table-discovery or
# arbitrary-SQL endpoint.  Public domains are discovered from JSONB below.
_MARTS: tuple[dict[str, Any], ...] = (
    {"schema": "analytics_clinical_core", "table": "dim_trials", "domain": "STUDY", "batch_bound": False},
    {"schema": "analytics_clinical_core", "table": "dim_participants", "domain": "DM", "batch_bound": False},
    {"schema": "analytics_clinical_marts", "table": "mart_trial_population", "domain": "ADSL", "batch_bound": True},
    {"schema": "analytics_clinical_marts", "table": "mart_week12_efficacy", "domain": "ADEFF", "batch_bound": True},
    {"schema": "analytics_clinical_marts", "table": "mart_randomization_balance", "domain": "ADSL", "batch_bound": True},
    {"schema": "analytics_clinical_marts", "table": "mart_missingness", "domain": "ADEFF", "batch_bound": True},
    {"schema": "analytics_clinical_marts", "table": "mart_treatment_exposure", "domain": "EX", "batch_bound": False},
    {"schema": "analytics_clinical_marts", "table": "mart_site_quality", "domain": "SITE_QUALITY", "batch_bound": False},
    {"schema": "analytics_clinical_marts", "table": "mart_safety_summary", "domain": "AE", "batch_bound": False},
    {"schema": "analytics_clinical_marts", "table": "mart_safety_trend", "domain": "AE", "batch_bound": False},
    {"schema": "analytics_clinical_marts", "table": "mart_visit_windows", "domain": "ADEFF", "batch_bound": False},
)

_MART_BY_RELATION = {
    f"{item['schema']}.{item['table']}": item for item in _MARTS
}

# Conceptual metric/dimension names used by the governed tool registry are
# intentionally different from raw warehouse column names.  These small,
# auditable aliases let the planner expose a semantic capability only when its
# backing field is present in the discovered data catalog.
SEMANTIC_MEASURE_FIELDS: dict[str, frozenset[str]] = {
    "treatment_effect": frozenset({"week12_improvement_score", "week12_improvement", "improvement_score", "chg"}),
    "week_12_improvement": frozenset({"week12_improvement_score", "week12_improvement", "improvement_score", "chg"}),
    "baseline_score": frozenset({"baseline_score", "baseline", "base"}),
    # ``mart_randomization_balance`` carries disease duration alongside the
    # baseline score.  It is a first-class governed measure rather than an
    # unrecognised raw column, so a question can ask for both in one balance
    # review without falling back to an inconclusive plan error.
    "disease_duration_months": frozenset({"disease_duration_months", "duration_months", "disease_duration"}),
    "missing_rate": frozenset({"week12_missing", "missing_rate", "missing_reason", "assessment_status"}),
    "missed_visit_rate": frozenset({"missed_visits", "visit_status", "missed_visit_rate"}),
    "adherence_rate": frozenset({"adherence_rate", "actual_dose", "planned_dose"}),
    "protocol_deviation": frozenset({"major_deviation_participants", "protocol_deviation", "deviation_count"}),
    "temperature_excursion": frozenset({"temperature_excursions", "maximum_excursion_temperature_c", "excursion_flag"}),
    "visit_window_deviation": frozenset({"outside_window_visits", "mean_absolute_deviation_days", "window_deviation_days"}),
    "serious_adverse_event_rate": frozenset({"serious_event_count", "participants_with_serious_adverse_event", "participant_event_rate"}),
    "adverse_event_rate": frozenset({"participants_with_adverse_event", "exposed_participants", "participant_event_rate", "adverse_event_count"}),
    "site_quality_burden": frozenset({"major_deviation_participants", "temperature_excursions", "outside_window_visits"}),
    "site_population": frozenset({"sample_size", "participant_count", "subject_count"}),
}

SEMANTIC_DIMENSION_FIELDS: dict[str, frozenset[str]] = {
    "treatment_arm": frozenset({"arm", "treatment_arm", "group", "variant"}),
    "site_id": frozenset({"site_id", "siteid", "site", "center_id"}),
    "region": frozenset({"region", "region1", "area"}),
    "time": frozenset({"event_month", "month", "date", "start_date", "received_date", "collected_at"}),
    "visit": frozenset({"visit_week", "visit", "avisit", "timepoint"}),
    "subgroup": frozenset({"age_band", "sex", "severity_band", "region", "subgroup"}),
    # Concrete subgroup dimensions are exposed as semantic names as well.  A
    # planner can create one governed task per requested dimension (for
    # example, one task for sex and another for severity_band) instead of
    # silently collapsing both into region.
    "age_band": frozenset({"age_band"}),
    "sex": frozenset({"sex"}),
    "severity_band": frozenset({"severity_band"}),
    "treatment": frozenset({"arm", "treatment_arm", "treatment", "extrt"}),
    "missingness_status": frozenset({"missing_reason", "assessment_status", "missingness_status"}),
    "analysis_method": frozenset({"analysis_method", "method"}),
}


def semantic_candidates(observed_fields: tuple[str, ...], candidates: tuple[str, ...], aliases: dict[str, frozenset[str]]) -> tuple[str, ...]:
    """Return semantic names whose raw backing field was actually observed."""

    observed = {_normal(item) for item in observed_fields}
    selected: list[str] = []
    for candidate in candidates:
        accepted = aliases.get(candidate, frozenset({candidate}))
        if any(_normal(field) in observed for field in accepted):
            selected.append(candidate)
    return tuple(selected)


_GENERIC_FIELDS_SQL = """
WITH published AS (
    SELECT r.domain_name, r.domain_version, r.payload_json
      FROM analytics_clinical_staging.stg_public_domain_records r
     WHERE (%s::text IS NULL OR r.batch_id = %s::text)
       AND (%s::text IS NULL OR NOT (r.payload_json ? 'TRIAL_ID') OR r.payload_json->>'TRIAL_ID' = %s::text)
), domain_counts AS (
    SELECT domain_name, domain_version, count(*)::int AS record_count
      FROM published
     GROUP BY domain_name, domain_version
), field_rows AS (
    SELECT p.domain_name, p.domain_version, f.field_name,
           jsonb_typeof(p.payload_json -> f.field_name) AS observed_type,
           p.payload_json ->> f.field_name AS text_value
      FROM published p
      CROSS JOIN LATERAL jsonb_object_keys(p.payload_json) AS f(field_name)
)
SELECT f.domain_name, f.domain_version, c.record_count, f.field_name,
       string_agg(DISTINCT f.observed_type, ',' ORDER BY f.observed_type) AS observed_type,
       count(*) FILTER (WHERE f.text_value IS NULL OR btrim(f.text_value) = '')::int AS null_count,
       count(*)::int AS observed_rows
  FROM field_rows f
  JOIN domain_counts c USING (domain_name, domain_version)
 GROUP BY f.domain_name, f.domain_version, c.record_count, f.field_name
 ORDER BY f.domain_name, f.field_name
"""

_MART_COLUMNS_SQL = """
SELECT c.table_schema AS schema_name,
       c.table_name AS dataset,
       CASE
         WHEN c.table_name = 'dim_trials' THEN 'STUDY'
         WHEN c.table_name = 'dim_participants' THEN 'DM'
         WHEN c.table_name IN ('mart_trial_population', 'mart_randomization_balance') THEN 'ADSL'
         WHEN c.table_name IN ('mart_week12_efficacy', 'mart_missingness', 'mart_visit_windows') THEN 'ADEFF'
         WHEN c.table_name = 'mart_treatment_exposure' THEN 'EX'
         WHEN c.table_name = 'mart_site_quality' THEN 'SITE_QUALITY'
         WHEN c.table_name IN ('mart_safety_summary', 'mart_safety_trend') THEN 'AE'
       END AS domain_name,
       c.column_name,
       c.data_type,
       c.is_nullable
  FROM information_schema.columns c
 WHERE (c.table_schema, c.table_name) IN (
       ('analytics_clinical_core', 'dim_trials'),
       ('analytics_clinical_core', 'dim_participants'),
       ('analytics_clinical_marts', 'mart_trial_population'),
       ('analytics_clinical_marts', 'mart_week12_efficacy'),
       ('analytics_clinical_marts', 'mart_randomization_balance'),
       ('analytics_clinical_marts', 'mart_missingness'),
       ('analytics_clinical_marts', 'mart_treatment_exposure'),
       ('analytics_clinical_marts', 'mart_site_quality'),
       ('analytics_clinical_marts', 'mart_safety_summary'),
       ('analytics_clinical_marts', 'mart_safety_trend'),
       ('analytics_clinical_marts', 'mart_visit_windows')
 )
 ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _inferred_type(raw: str | None, field_name: str) -> str:
    value = (raw or "").casefold()
    types = {item.strip() for item in value.split(",") if item.strip()}
    if types <= {"number", "integer", "decimal", "numeric", "bigint", "double precision", "real"} and types:
        return "number"
    if types <= {"boolean", "bool"} and types:
        return "boolean"
    if types <= {"date"} and types:
        return "date"
    if types <= {"timestamp", "timestamp without time zone", "timestamp with time zone"} and types:
        return "datetime"
    if types <= {"array"} and types:
        return "array"
    if types <= {"object"} and types:
        return "object"
    # Uploaded JSON often carries dates as strings.  This is only a type hint;
    # it does not coerce the source value or claim that every string is a date.
    if re.search(r"(?:^|_)(date|datetime|timestamp|time|dt|at)$", field_name.casefold()):
        return "datetime"
    return "string" if types else "unknown"


def _role(field_name: str, inferred_type: str) -> str:
    normalized = field_name.casefold()
    if inferred_type == "number" and re.search(r"(?:^|_)(age|week|month|year)$", normalized):
        return "dimension"
    if inferred_type == "number" and not re.search(r"(?:^|_)(id|key|seq|sequence|identifier|number)$", normalized):
        return "measure"
    if inferred_type == "unknown":
        return "unknown"
    if re.search(r"(?:^|_)(id|key|seq|sequence|identifier|number)$", normalized) or normalized in {"id", "studyid", "usubjid"}:
        return "identifier"
    return "dimension"


def _sql_type(raw: str | None) -> str:
    value = (raw or "").casefold()
    if any(token in value for token in ("numeric", "decimal", "integer", "bigint", "double", "real")):
        return "number"
    if value in {"date"}:
        return "date"
    if "timestamp" in value or value == "time without time zone":
        return "datetime"
    if value in {"boolean", "bool"}:
        return "boolean"
    if value in {"json", "jsonb"}:
        return "object"
    return "string"


class ClinicalDataCatalog:
    """Build a governed metadata snapshot from a read-only query executor."""

    def __init__(self, executor: CatalogQueryExecutor, domain_registry: DomainRegistry | None = None) -> None:
        self.executor = executor
        self.domain_registry = domain_registry

    def _aliases(self, domain_name: str, field_name: str) -> tuple[str, ...]:
        if self.domain_registry is None:
            return ()
        try:
            definition = self.domain_registry.get(domain_name)
        except KeyError:
            return ()
        target_normal = _normal(field_name)
        for target, field in definition.fields.items():
            accepted = {_normal(target), *(_normal(alias) for alias in field.aliases)}
            if target_normal in accepted:
                return tuple(dict.fromkeys((target, *field.aliases)))
        return ()

    def _domain_metadata(self, domain_name: str, version: str | None) -> tuple[str, bool, str, str | None]:
        if self.domain_registry is None:
            return (version or "custom", False, "custom_candidate", None)
        try:
            definition = self.domain_registry.get(domain_name, version)
        except KeyError:
            return (version or "custom", False, "custom_candidate", None)
        return (definition.version, True, "registered", definition.grain)

    def _generic_datasets(self, trial_id: str | None, published_batch_id: str | None) -> list[CatalogDataset]:
        # Public-domain records (FAERS, registry, Synthea, and so on) are
        # intentionally kept out of an unbound participant-level trial
        # catalog.  They do not carry the trial mart's ``trial_id`` and would
        # otherwise force every clinical question to scan the entire public
        # ingestion table.  A selected published batch is the explicit bridge
        # for generic domains, so batch-scoped catalogs still inspect it.
        if trial_id is not None and published_batch_id is None:
            return []
        rows = self.executor.query(
            _GENERIC_FIELDS_SQL,
            (published_batch_id, published_batch_id, trial_id, trial_id),
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (str(row.get("domain_name", "")).upper(), str(row.get("domain_version") or "custom"))
            if key[0]:
                grouped.setdefault(key, []).append(row)
        datasets: list[CatalogDataset] = []
        for (domain_name, observed_version), items in sorted(grouped.items()):
            version, registered, status, grain = self._domain_metadata(domain_name, observed_version)
            fields: list[CatalogField] = []
            for row in items:
                observed_rows = int(row.get("observed_rows") or 0)
                null_count = int(row.get("null_count") or 0)
                inferred = _inferred_type(str(row.get("observed_type") or ""), str(row["field_name"]))
                fields.append(
                    CatalogField(
                        name=str(row["field_name"]),
                        inferred_type=inferred,
                        role=_role(str(row["field_name"]), inferred),
                        nullable=null_count > 0,
                        null_rate=(null_count / observed_rows) if observed_rows else None,
                        observed_rows=observed_rows,
                        aliases=self._aliases(domain_name, str(row["field_name"])),
                    )
                )
            datasets.append(
                CatalogDataset(
                    dataset=f"domain_records:{domain_name}",
                    domain=domain_name,
                    domain_version=version,
                    domain_status=status,
                    is_registered=registered,
                    source="clinical_ingestion.domain_records",
                    grain=grain,
                    record_count=int(items[0].get("record_count") or 0),
                    fields=tuple(fields),
                    measures=tuple(field.name for field in fields if field.role == "measure"),
                    dimensions=tuple(field.name for field in fields if field.role in {"dimension", "identifier"}),
                    identifiers=tuple(field.name for field in fields if field.role == "identifier"),
                )
            )
        return datasets

    def _mart_datasets(self, trial_id: str | None, published_batch_id: str | None) -> list[CatalogDataset]:
        column_rows = self.executor.query(_MART_COLUMNS_SQL, ())
        by_relation: dict[str, list[dict[str, Any]]] = {}
        for row in column_rows:
            relation = f"{row.get('schema_name')}.{row.get('dataset')}"
            if relation in _MART_BY_RELATION:
                by_relation.setdefault(relation, []).append(row)
        datasets: list[CatalogDataset] = []
        for relation, spec in _MART_BY_RELATION.items():
            rows = by_relation.get(relation, [])
            if not rows:
                continue
            column_names = {str(row["column_name"]) for row in rows}
            # A selected published batch may use only batch-bound marts.  An
            # unbound mart is deliberately omitted rather than leaking default
            # data into a version-scoped catalog.
            if published_batch_id and spec["batch_bound"] is False and spec["table"] not in {"dim_trials", "dim_participants"}:
                continue
            clauses: list[str] = []
            params: list[Any] = []
            if trial_id and "trial_id" in column_names:
                clauses.append("trial_id = %s")
                params.append(trial_id)
            if published_batch_id:
                if "source_batch_id" not in column_names:
                    if spec["batch_bound"]:
                        continue
                else:
                    clauses.append("source_batch_id = %s")
                    params.append(published_batch_id)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            count_sql = f"SELECT count(*)::int AS record_count FROM {relation}{where}"
            count_rows = self.executor.query(count_sql, tuple(params))
            record_count = int(count_rows[0].get("record_count") or 0) if count_rows else 0
            if record_count == 0:
                continue
            domain_name = str(spec["domain"])
            version, registered, status, grain = self._domain_metadata(domain_name, None)
            fields = tuple(
                CatalogField(
                    name=str(row["column_name"]),
                    inferred_type=_sql_type(str(row.get("data_type") or "")),
                    role=_role(str(row["column_name"]), _sql_type(str(row.get("data_type") or ""))),
                    nullable=str(row.get("is_nullable", "YES")).upper() == "YES",
                    aliases=self._aliases(domain_name, str(row["column_name"])),
                )
                for row in rows
            )
            datasets.append(
                CatalogDataset(
                    dataset=str(spec["table"]),
                    domain=domain_name,
                    domain_version=version,
                    domain_status=status,
                    is_registered=registered,
                    source=relation,
                    grain=grain,
                    record_count=record_count,
                    fields=fields,
                    measures=tuple(field.name for field in fields if field.role == "measure"),
                    dimensions=tuple(field.name for field in fields if field.role in {"dimension", "identifier"}),
                    identifiers=tuple(field.name for field in fields if field.role == "identifier"),
                )
            )
        return datasets

    def snapshot(self, trial_id: str | None = None, published_batch_id: str | None = None) -> CatalogSnapshot:
        datasets = self._generic_datasets(trial_id, published_batch_id)
        datasets.extend(self._mart_datasets(trial_id, published_batch_id))
        datasets.sort(key=lambda item: (item.domain, item.dataset))
        measures = tuple(sorted({field.name for dataset in datasets for field in dataset.fields if field.role == "measure"}))
        dimensions = tuple(sorted({field.name for dataset in datasets for field in dataset.fields if field.role in {"dimension", "identifier"}}))
        published_domains = tuple(sorted({dataset.domain for dataset in datasets if dataset.record_count is None or dataset.record_count > 0}))
        data_gaps = tuple(
            f"{dataset.domain} 未通过标准域注册，仅作为自定义候选域展示"
            for dataset in datasets
            if not dataset.is_registered
        )
        return CatalogSnapshot(
            trial_id=trial_id,
            published_batch_id=published_batch_id,
            published_domains=published_domains,
            datasets=tuple(datasets),
            measures=measures,
            dimensions=dimensions,
            data_gaps=data_gaps,
        )


class PostgresClinicalDataCatalog(ClinicalDataCatalog):
    def __init__(self, database_url: str, domain_registry: DomainRegistry | None = None) -> None:
        super().__init__(self._executor(database_url), domain_registry)

    @staticmethod
    def _executor(database_url: str) -> CatalogQueryExecutor:
        class _Executor:
            def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
                with psycopg.connect(database_url, row_factory=dict_row) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute(sql, params)
                        return [dict(row) for row in cursor.fetchall()]

        return _Executor()

