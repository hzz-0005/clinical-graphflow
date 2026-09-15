from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Protocol

import psycopg
from pydantic import BaseModel, Field, model_validator

from app.semantic.metrics import MetricRepository
from app.tools.sql_safety import SqlPolicy


class MetricQuery(BaseModel):
    metric: str
    start: date
    end: date
    group_by: list[str] = Field(default_factory=list)
    filters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_range(self) -> MetricQuery:
        if self.start >= self.end:
            raise ValueError("start must be before end")
        return self


class CompiledMetricQuery(BaseModel):
    metric: str
    source: str
    sql: str
    params: tuple[Any, ...]
    filters: dict[str, str] = Field(default_factory=dict)


class QueryResult(BaseModel):
    sql: str
    source: str
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int


class MetricTool(Protocol):
    def query(self, compiled: CompiledMetricQuery) -> QueryResult: ...


class MetricQueryCompiler:

    def __init__(self, metrics: MetricRepository) -> None:
        self._metrics = metrics

    def compile(self, query: MetricQuery) -> CompiledMetricQuery:
        metric = self._metrics.get(query.metric)
        requested = [*query.group_by, *query.filters]
        invalid = [name for name in requested if not metric.allows_dimension(name)]
        if invalid:
            raise ValueError(f"Dimension is not governed for {metric.name}: {invalid[0]}")

        source = (
            f"analytics_core.{metric.model}"
            if metric.model.startswith("fct_")
            else f"analytics_marts.{metric.model}"
        )
        time_bucket = f"date_trunc('{metric.time_grain}', {metric.time_dimension})::date"
        dimensions = list(dict.fromkeys(query.group_by))
        select_dimensions = [f"{time_bucket} as {metric.time_dimension}", *dimensions]
        where = [f"{metric.time_dimension} >= %s", f"{metric.time_dimension} < %s"]
        params: list[Any] = [query.start, query.end]
        for name, value in query.filters.items():
            where.append(f"{name} = %s")
            params.append(value)
        group_positions = ", ".join(str(index) for index in range(1, len(select_dimensions) + 1))
        expression, components, sample_expression = self._expressions(metric.name)
        component_selects = [f"{value} as {name}" for name, value in components.items()]
        measures = [*component_selects, f"{expression} as {metric.name}"]
        entity_key = "ticket_id" if metric.entity == "support_ticket" else "organization_id"
        sql = (
            f"select {', '.join(select_dimensions)}, {', '.join(measures)}, "
            f"{sample_expression or f'count(distinct {entity_key})'} as sample_size "
            f"from {source} where {' and '.join(where)} "
            f"group by {group_positions} order by {group_positions}"
        )
        return CompiledMetricQuery(
            metric=metric.name,
            source=source,
            sql=sql,
            params=tuple(params),
            filters=query.filters,
        )

    @staticmethod
    def _expressions(metric_name: str) -> tuple[str, dict[str, str], str | None]:
        organization_count = "count(distinct organization_id)"
        renewed = f"{organization_count} filter (where renewed_contracts = 1)"
        eligible = f"{organization_count} filter (where eligible_contracts = 1)"
        activated = f"{organization_count} filter (where is_activated)"
        if metric_name == "arr":
            return "sum(annual_recurring_revenue)", {}, None
        if metric_name == "renewal_rate":
            return (
                f"{renewed}::double precision / nullif({eligible}, 0)",
                {"numerator": renewed, "denominator": eligible},
                eligible,
            )
        if metric_name == "activation_rate":
            return (
                f"{activated}::double precision / nullif({organization_count}, 0)",
                {"numerator": activated, "denominator": organization_count},
                organization_count,
            )
        if metric_name == "churn_rate":
            churned = f"{eligible} - {renewed}"
            return (
                f"({churned})::double precision / nullif({eligible}, 0)",
                {"numerator": churned, "denominator": eligible},
                eligible,
            )
        if metric_name == "support_resolution_time_hours":
            resolved = "count(*) filter (where resolved_at is not null)"
            return (
                "percentile_cont(0.5) within group (order by resolution_time_hours)",
                {},
                resolved,
            )
        raise ValueError(f"Metric has no governed SQL compiler: {metric_name}")


class PostgresMetricTool:
    def __init__(
        self,
        database_url: str,
        allowed_relations: set[str],
        statement_timeout_ms: int = 5_000,
    ) -> None:
        self._database_url = database_url
        self._policy = SqlPolicy(allowed_relations=allowed_relations)
        self._statement_timeout_ms = statement_timeout_ms

    def query(self, compiled: CompiledMetricQuery) -> QueryResult:
        validation_sql = compiled.sql.replace("%s", "NULL")
        self._policy.validate(validation_sql)
        execution_sql = f"{compiled.sql} limit {self._policy.max_rows}"
        with psycopg.connect(self._database_url) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(
                    f"set local statement_timeout = {self._statement_timeout_ms}"
                )
                cursor.execute(execution_sql, compiled.params)
                columns = [column.name for column in cursor.description or []]
                raw_rows = cursor.fetchall()
        rows = [
            {column: self._json_value(value) for column, value in zip(columns, row)}
            for row in raw_rows
        ]
        return QueryResult(
            sql=execution_sql,
            source=compiled.source,
            columns=columns,
            rows=rows,
            row_count=len(rows),
        )

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, date):
            return value.isoformat()
        return value

