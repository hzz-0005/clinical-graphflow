from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.lower())


class ColumnSignal(BaseModel):
    model_config = ConfigDict(frozen=True)

    inferred_type: str
    distinct_values: tuple[str, ...] = ()


class DomainFieldDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    data_type: Literal[
        "string", "integer", "decimal", "boolean", "date", "datetime", "enum", "identifier"
    ]
    required: bool = False
    aliases: tuple[str, ...] = ()
    controlled_values: tuple[str, ...] = ()
    unit_field: str | None = None


class DomainRelationship(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_domain: str
    local: tuple[str, ...]
    remote: tuple[str, ...]


class DomainDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    standard: str
    version: str
    grain: str
    keys: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    fields: dict[str, DomainFieldDefinition]
    relationships: tuple[DomainRelationship, ...] = ()

    @model_validator(mode="after")
    def keys_must_be_fields(self):
        missing = set(self.keys) - set(self.fields)
        if missing:
            raise ValueError(f"domain keys are not declared fields: {', '.join(sorted(missing))}")
        return self


class DomainMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain: str
    version: str | None = None
    score: float = Field(ge=0)
    matched_fields: tuple[str, ...] = ()
    reason: str


class DomainRegistry:
    def __init__(self, domains: tuple[DomainDefinition, ...]) -> None:
        seen: set[tuple[str, str]] = set()
        for domain in domains:
            key = (domain.name.upper(), domain.version)
            if key in seen:
                raise ValueError(f"duplicate clinical domain version: {key[0]} {key[1]}")
            seen.add(key)
        self._domains = domains

    @classmethod
    def from_yaml(cls, path: Path) -> "DomainRegistry":
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(tuple(DomainDefinition.model_validate(item) for item in payload["domains"]))

    @property
    def names(self) -> set[str]:
        return {item.name for item in self._domains}

    @property
    def domains(self) -> tuple[DomainDefinition, ...]:
        return self._domains

    def get(self, name: str, version: str | None = None) -> DomainDefinition:
        candidates = [
            item
            for item in self._domains
            if item.name.upper() == name.upper() and (version is None or item.version == version)
        ]
        if not candidates:
            raise KeyError(f"unknown clinical domain: {name}")
        return sorted(candidates, key=lambda item: item.version)[-1]

    def search(
        self,
        query: str,
        columns: tuple[str, ...],
        profiles: dict[str, ColumnSignal] | None = None,
        limit: int = 5,
    ) -> list[DomainMatch]:
        query_normal = _normal(query)
        profiles = profiles or {}
        matches: list[DomainMatch] = []
        filename_normal = _normal(query.rsplit(".", 1)[0])
        for domain in self._domains:
            score = 0.0
            reasons: list[str] = []
            if _normal(domain.name) and _normal(domain.name) in query_normal:
                score += 8
                reasons.append("问题包含数据域名称")
            for alias in domain.aliases:
                if _normal(alias) and _normal(alias) in query_normal:
                    score += 10
                    reasons.append(f"问题匹配域别名 {alias}")
                # A source filename is a strong signal when it exactly names a registered
                # domain (for example ``claims_transactions.csv``).  Substring matching alone
                # would let the shorter ``claims`` domain outrank the more specific plugin.
                if filename_normal and filename_normal == _normal(alias):
                    score += 12
                    reasons.append(f"文件名精确匹配域别名 {alias}")
            if filename_normal and filename_normal == _normal(domain.name):
                score += 12
                reasons.append("文件名精确匹配数据域名称")

            matched_fields: list[str] = []
            for target, definition in domain.fields.items():
                accepted = {_normal(target), *(_normal(alias) for alias in definition.aliases)}
                source = next((column for column in columns if _normal(column) in accepted), None)
                if source is None:
                    continue
                matched_fields.append(target)
                score += 3 if definition.required else 1.5
                signal = profiles.get(source)
                if signal and (
                    signal.inferred_type == definition.data_type
                    or definition.data_type == "enum" and signal.inferred_type in {"enum", "string"}
                ):
                    score += 0.5
            if matched_fields:
                reasons.append(f"匹配 {len(matched_fields)} 个注册字段")
            if score > 0:
                matches.append(
                    DomainMatch(
                        domain=domain.name,
                        version=domain.version,
                        score=round(score, 2),
                        matched_fields=tuple(matched_fields),
                        reason="；".join(dict.fromkeys(reasons)),
                    )
                )

        if not matches:
            return [
                DomainMatch(
                    domain="custom_candidate",
                    score=0,
                    reason="没有标准域达到可信匹配条件，保留为待确认自定义数据域",
                )
            ]
        return sorted(matches, key=lambda item: (-item.score, item.domain))[:limit]

