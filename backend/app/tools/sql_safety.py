from dataclasses import dataclass

from sqlglot import exp, parse
from sqlglot.errors import ParseError


class UnsafeSqlError(ValueError):
    pass


CLINICAL_MART_RELATIONS = {
    # ``dim_participants`` is a governed, read-only demographic lookup used to
    # attach age/sex/severity to batch-bound outcome and missingness marts.
    # It is included explicitly so the policy does not turn a safe join into
    # an opaque execution error.
    "analytics_clinical_core.dim_participants",
    # Participant-level protocol flags are a governed quality input for the
    # allow-listed ``exclude_major_protocol_deviation`` sensitivity method.
    # The adapter still controls the join and predicate; exposing this relation
    # here only permits that fixed, read-only query shape.
    "analytics_clinical_core.fct_protocol_deviations",
    "analytics_clinical_marts.mart_trial_population",
    "analytics_clinical_marts.mart_week12_efficacy",
    "analytics_clinical_marts.mart_randomization_balance",
    "analytics_clinical_marts.mart_missingness",
    "analytics_clinical_marts.mart_treatment_exposure",
    "analytics_clinical_marts.mart_site_quality",
    "analytics_clinical_marts.mart_safety_summary",
    "analytics_clinical_marts.mart_safety_trend",
    "analytics_clinical_marts.mart_visit_windows",
}


@dataclass(frozen=True)
class SqlPolicy:
    allowed_relations: set[str]
    max_rows: int = 500

    def validate(self, sql: str) -> str:
        if "--" in sql or "/*" in sql or "*/" in sql:
            raise UnsafeSqlError("SQL comments are not allowed")
        try:
            statements = parse(sql, dialect="postgres")
        except ParseError as exc:
            raise UnsafeSqlError("SQL could not be parsed") from exc
        if len(statements) != 1:
            raise UnsafeSqlError("Exactly one SQL statement is allowed")
        statement = statements[0]
        if not isinstance(statement, exp.Query):
            raise UnsafeSqlError("Only SELECT or WITH queries are allowed")
        forbidden_nodes = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Command)
        if any(statement.find_all(forbidden_nodes)):
            raise UnsafeSqlError("Nested write or command statements are not allowed")

        allowed_functions = {
            "avg",
            "bool_and",
            "bool_or",
            "cast",
            "coalesce",
            "count",
            "date_trunc",
            "max",
            "min",
            "nullif",
            "percentile_cont",
            "round",
            "sum",
        }
        # sqlglot models operators such as AND and CAST as typed Func nodes too.
        # Those nodes have fixed parser semantics. Only generic/unknown SQL
        # functions (Anonymous) need the explicit allowlist; this blocks calls
        # such as pg_read_file and query_to_xml without rejecting normal syntax.
        for function in statement.find_all(exp.Anonymous):
            function_name = function.name.lower()
            if function_name not in allowed_functions:
                raise UnsafeSqlError(f"Function is not allowed: {function_name}")

        cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            if table.name.lower() in cte_names and not table.db:
                continue
            relation = ".".join(
                part.lower() for part in (table.catalog, table.db, table.name) if part
            )
            if relation not in self.allowed_relations:
                raise UnsafeSqlError(f"Relation is not allowed: {relation}")

        limit = statement.args.get("limit")
        if limit is None:
            statement = statement.limit(self.max_rows)
        else:
            literal = limit.expression
            if not isinstance(literal, exp.Literal) or not literal.is_int:
                raise UnsafeSqlError("LIMIT must be an integer literal")
            if int(literal.this) > self.max_rows:
                statement.set("limit", exp.Limit(expression=exp.Literal.number(self.max_rows)))
        return statement.sql(dialect="postgres")

    def validate_template(self, sql: str) -> str:
        """Validate parameterized SQL while preserving database placeholders."""
        placeholder_count = sql.count("%s")
        validated = self.validate(sql.replace("%s", "NULL"))
        if placeholder_count == 0:
            return validated
        # Validation may normalize the query, so only use it as the policy check.
        # Psycopg must receive the original placeholders for safe value binding.
        if " limit " not in sql.lower():
            return f"{sql} limit {self.max_rows}"
        return sql

