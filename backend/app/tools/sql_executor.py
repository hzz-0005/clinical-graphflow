from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from app.tools.metric_query import QueryResult
from app.tools.sql_safety import SqlPolicy


class ReadonlySqlTool:
    def __init__(
        self,
        database_url: str,
        allowed_relations: set[str],
        statement_timeout_ms: int = 5_000,
    ) -> None:
        self._database_url = database_url
        self._policy = SqlPolicy(allowed_relations=allowed_relations)
        self._statement_timeout_ms = statement_timeout_ms

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> QueryResult:
        safe_sql = (
            self._policy.validate_template(sql) if params else self._policy.validate(sql)
        )
        with psycopg.connect(self._database_url) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(
                    f"set local statement_timeout = {self._statement_timeout_ms}"
                )
                cursor.execute(safe_sql, params)
                columns = [column.name for column in cursor.description or []]
                raw_rows = cursor.fetchall()
        rows = [
            {column: self._json_value(value) for column, value in zip(columns, row)}
            for row in raw_rows
        ]
        return QueryResult(
            sql=safe_sql,
            source="readonly_sql",
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

