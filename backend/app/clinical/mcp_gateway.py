from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.clinical.registry import ClinicalToolRegistry
from app.clinical.tools import ClinicalToolResult
from app.clinical.v17_contracts import ToolCallRequest
from app.tools.sql_safety import CLINICAL_MART_RELATIONS, SqlPolicy, UnsafeSqlError


class MCPToolError(ValueError):
    """A safe, model-facing error raised before or during a governed tool call."""


class MCPToolDescriptor(BaseModel):
    """Provider-neutral tool metadata exposed to an agent or an MCP client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    required_domains: tuple[str, ...] = ()
    supporting_domains: tuple[str, ...] = ()
    missing_supporting_domains: tuple[str, ...] = ()
    capability: str = ""
    operations: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    read_only: bool = True


class MCPToolResult(BaseModel):
    """Stable result envelope consumed by the graph observation node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    source: str
    sql: str = ""
    params: tuple[Any, ...] = ()
    rows: list[dict[str, Any]] = Field(default_factory=list)
    warnings: tuple[str, ...] = ()
    minimum_cell_size: int = 10
    missing_supporting_domains: tuple[str, ...] = ()


class ClinicalMCPGateway:
    """In-process MCP-compatible boundary over the existing clinical registry.

    This is deliberately not a second permission system.  The registry remains the source of
    truth for tool availability and handlers; this class adds a stable descriptor/result contract
    and validates the SQL returned by a handler before the result can enter the graph.
    """

    def __init__(
        self,
        registry: ClinicalToolRegistry,
        available_domains: set[str] | frozenset[str],
        *,
        sql_policy: SqlPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.available_domains = frozenset(item.upper() for item in available_domains)
        self.sql_policy = sql_policy or SqlPolicy(set(CLINICAL_MART_RELATIONS))

    def _eligible(self, name: str):
        try:
            plugin = self.registry.plugin(name)
        except KeyError as exc:
            raise MCPToolError(f"tool {name} is not available") from exc
        if name == "submit_clinical_conclusion":
            raise MCPToolError("conclusion submission is not a query tool")
        if not plugin.required_domains.issubset(self.available_domains):
            missing = ",".join(sorted(plugin.required_domains - self.available_domains))
            raise MCPToolError(f"tool {name} is not available for published domains: {missing}")
        return plugin

    @staticmethod
    def _descriptor(plugin, available_domains: frozenset[str]) -> MCPToolDescriptor:
        return MCPToolDescriptor(
            name=plugin.name,
            description=plugin.description,
            input_schema=(
                plugin.argument_model.model_json_schema()
                if plugin.argument_model
                else {"type": "object", "properties": {}}
            ),
            required_domains=tuple(sorted(plugin.required_domains)),
            supporting_domains=tuple(sorted(plugin.supporting_domains)),
            missing_supporting_domains=tuple(sorted(plugin.missing_domains(set(available_domains)))),
            capability=plugin.capability,
            operations=plugin.operations,
            measures=plugin.measures,
            dimensions=plugin.dimensions,
        )

    def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        """Return only tools whose hard-gated domains are actually published."""

        descriptors = []
        for plugin in self.registry.describe():
            if plugin.name == "submit_clinical_conclusion":
                continue
            if plugin.required_domains.issubset(self.available_domains):
                descriptors.append(self._descriptor(plugin, self.available_domains))
        return tuple(descriptors)

    def call(self, request: ToolCallRequest) -> MCPToolResult:
        plugin = self._eligible(request.tool_name)
        try:
            raw = self.registry.invoke(request.tool_name, request.arguments)
        except (KeyError, TypeError, ValueError) as exc:
            raise MCPToolError(f"tool {request.tool_name} rejected the request: {exc}") from exc
        if not isinstance(raw, ClinicalToolResult):
            raise MCPToolError(f"tool {request.tool_name} returned an invalid result")

        safe_sql = ""
        if raw.sql:
            try:
                safe_sql = self.sql_policy.validate_template(raw.sql)
            except UnsafeSqlError as exc:
                raise MCPToolError(f"tool {request.tool_name} returned unsafe SQL: {exc}") from exc

        missing = tuple(sorted(plugin.missing_domains(set(self.available_domains))))
        warnings = tuple(dict.fromkeys([*raw.warnings, *(f"缺少支持数据域：{item}" for item in missing)]))
        return MCPToolResult(
            tool_name=request.tool_name,
            source=raw.source,
            sql=safe_sql,
            params=raw.params,
            rows=raw.rows,
            warnings=warnings,
            minimum_cell_size=raw.minimum_cell_size,
            missing_supporting_domains=missing,
        )

