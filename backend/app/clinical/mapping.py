from __future__ import annotations

import re
import json
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from pydantic import BaseModel, ConfigDict

from app.clinical.ingestion import STANDARD_FIELDS
from app.clinical.tabular import TabularFile


ALIASES = {
    "STUDYID": ("studyid", "study", "trialid", "trial", "试验编号", "研究编号"),
    "USUBJID": ("usubjid", "subject", "subjectid", "participantid", "受试者编号", "参与者编号"),
    "SITEID": ("siteid", "site", "centerid", "中心编号", "研究中心"),
    "ARM": ("arm", "group", "treatmentgroup", "分组", "治疗组"),
    "REGION1": ("region1", "region", "地区", "区域"),
    "ITTFL": ("ittfl", "itt", "意向治疗集"),
    "SAFFL": ("saffl", "safety", "安全集"),
    "PPROTFL": ("pprotfl", "perprotocol", "符合方案集"),
    "PARAMCD": ("paramcd", "parameter", "endpoint", "指标", "终点"),
    "AVISIT": ("avisit", "visit", "访视"),
    "BASE": ("base", "baseline", "基线值"),
    "AVAL": ("aval", "value", "result", "结果值"),
    "CHG": ("chg", "change", "变化值"),
}


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.lower())


class FieldSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True)
    target_field: str
    source_field: str | None = None
    confidence: float
    reason: str
    required: bool = True


class FileMappingSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True)
    filename: str
    format: str
    domain: str
    row_count: int
    confidence: float
    fields: tuple[FieldSuggestion, ...]


class MappingSuggestion(BaseModel):
    model_config = ConfigDict(frozen=True)
    files: tuple[FileMappingSuggestion, ...]
    ready_for_preview: bool
    warnings: tuple[str, ...] = ()
    mapping_provider: str = "rules"


class MappingRefiner(Protocol):
    provider: str
    model: str

    def refine(self, files: tuple[TabularFile, ...], suggestion: MappingSuggestion) -> MappingSuggestion: ...


MappingTransport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class ClinicalMappingSuggester:
    """Deterministic safe fallback; an LLM adapter may refine only this contract."""

    def suggest(self, files: tuple[TabularFile, ...]) -> MappingSuggestion:
        suggestions = tuple(self._suggest_file(item) for item in files)
        domains = {item.domain for item in suggestions}
        complete = all(all(field.source_field for field in item.fields) for item in suggestions)
        ready = domains == set(STANDARD_FIELDS) and complete
        warnings = () if ready else ("需要确认数据域或关键字段映射后才能预检",)
        return MappingSuggestion(files=suggestions, ready_for_preview=ready, warnings=warnings)

    @staticmethod
    def _validate_refinement(files: tuple[TabularFile, ...], refined: MappingSuggestion) -> MappingSuggestion:
        """Accept only mappings that stay inside the uploaded schema and CDISC contract."""
        uploaded = {item.filename: set(item.columns) for item in files}
        safe_files = []
        for item in refined.files:
            if item.filename not in uploaded or item.domain not in STANDARD_FIELDS:
                continue
            allowed_targets = set(STANDARD_FIELDS[item.domain])
            safe_fields = tuple(field for field in item.fields if field.target_field in allowed_targets and (field.source_field is None or field.source_field in uploaded[item.filename]))
            safe_files.append(item.model_copy(update={"fields": safe_fields}))
        by_name = {item.filename: item for item in safe_files}
        base_suggestion = ClinicalMappingSuggester().suggest(files)
        merged_items = []
        for base in base_suggestion.files:
            candidate = by_name.get(base.filename)
            if candidate is None or candidate.domain != base.domain:
                merged_items.append(base)
                continue
            candidate_fields = {field.target_field: field for field in candidate.fields}
            merged_items.append(candidate.model_copy(update={"fields": tuple(candidate_fields.get(field.target_field, field) for field in base.fields)}))
        merged = tuple(merged_items)
        domains = {item.domain for item in merged}
        complete = all(all(field.source_field for field in item.fields) for item in merged)
        warnings = tuple(refined.warnings) + (() if domains == set(STANDARD_FIELDS) and complete else ("AI 建议仍需人工确认，未通过完整字段校验",))
        return MappingSuggestion(files=merged, ready_for_preview=domains == set(STANDARD_FIELDS) and complete, warnings=warnings)

    def _suggest_file(self, item: TabularFile) -> FileMappingSuggestion:
        scores: dict[str, int] = {}
        for domain, targets in STANDARD_FIELDS.items():
            scores[domain] = sum(self._match(target, item.columns) is not None for target in targets)
        domain = max(scores, key=scores.get)
        fields = tuple(self._field(target, item.columns) for target in STANDARD_FIELDS[domain])
        confidence = round(sum(field.confidence for field in fields) / len(fields), 2)
        return FileMappingSuggestion(filename=item.filename, format=item.format, domain=domain, row_count=item.row_count, confidence=confidence, fields=fields)

    def _field(self, target: str, columns: tuple[str, ...]) -> FieldSuggestion:
        source = self._match(target, columns)
        exact = source is not None and _normal(source) == _normal(target)
        confidence = 1.0 if exact else 0.94 if source else 0.0
        reason = "字段名与临床标准字段一致" if exact else "字段语义与已审核别名匹配" if source else "未找到可信候选，需要人工选择"
        return FieldSuggestion(target_field=target, source_field=source, confidence=confidence, reason=reason)

    @staticmethod
    def _match(target: str, columns: tuple[str, ...]) -> str | None:
        aliases = {_normal(value) for value in ALIASES[target]}
        return next((column for column in columns if _normal(column) in aliases), None)


class OpenAICompatibleMappingRefiner:
    """Optional LLM refinement over schema metadata only; row values never enter the prompt."""

    def __init__(self, provider: str, model: str, api_key: str, base_url: str, timeout: float = 60, transport: MappingTransport | None = None) -> None:
        if not api_key:
            raise ValueError(f"{provider} API key is not configured")
        if not model:
            raise ValueError(f"{provider} model is not configured")
        self.provider, self.model = provider, model
        self._api_key, self._base_url, self._timeout = api_key, base_url.rstrip("/"), timeout
        self._transport = transport or self._post

    def refine(self, files: tuple[TabularFile, ...], suggestion: MappingSuggestion) -> MappingSuggestion:
        source_by_name = {item.filename: item for item in files}
        metadata = [{"filename": item.filename, "format": item.format, "columns": list(source_by_name[item.filename].columns), "row_count": item.row_count, "rule_suggestion": item.model_dump(mode="json")} for item in suggestion.files]
        prompt = (
            "You map clinical tabular schemas to a strict CDISC contract. Return JSON only with files. "
            "Never invent columns, domains, or identifiers. Use only source columns shown in the metadata. "
            "For every target field, provide source_field, confidence, and a short Chinese reason. "
            "Do not include row values, subject IDs, dates, or any uploaded records.\n"
            + json.dumps({"allowed_domains": list(STANDARD_FIELDS), "allowed_fields": STANDARD_FIELDS, "files": metadata}, ensure_ascii=False)
        )
        payload = {"model": self.model, "messages": [{"role": "system", "content": "You are a governed clinical schema mapper. JSON only."}, {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "temperature": 0, "max_tokens": 2400}
        if self.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        started = time.perf_counter()
        data = self._transport(f"{self._base_url}/chat/completions", {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}, payload, self._timeout)
        _ = int((time.perf_counter() - started) * 1000)
        content = data["choices"][0]["message"]["content"]
        return ClinicalMappingSuggester._validate_refinement(files, MappingSuggestion.model_validate(json.loads(content)))

    @staticmethod
    def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()


class AnthropicMappingRefiner(OpenAICompatibleMappingRefiner):
    """Claude transport using the same validated mapping contract."""

    def refine(self, files: tuple[TabularFile, ...], suggestion: MappingSuggestion) -> MappingSuggestion:
        source_by_name = {item.filename: item for item in files}
        metadata = [{"filename": item.filename, "format": item.format, "columns": list(source_by_name[item.filename].columns), "row_count": item.row_count, "rule_suggestion": item.model_dump(mode="json")} for item in suggestion.files]
        prompt = "Map only the supplied clinical schema metadata to CDISC fields. Return JSON only, never row values or identifiers. Include files with domain and fields (target_field, source_field, confidence, reason, required). Allowed contract: " + json.dumps({"domains": STANDARD_FIELDS, "files": metadata}, ensure_ascii=False)
        payload = {"model": self.model, "system": "You are a governed clinical schema mapper. Return valid JSON only.", "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 2400}
        data = self._transport(f"{self._base_url}/v1/messages", {"x-api-key": self._api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, payload, self._timeout)
        content = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        return ClinicalMappingSuggester._validate_refinement(files, MappingSuggestion.model_validate(json.loads(content)))


def build_mapping_refiner(settings, provider: str) -> MappingRefiner:
    """Build the optional provider adapter; rules mode is deliberately explicit."""
    configs = {
        "openai": (settings.openai_api_key, settings.openai_base_url, settings.openai_model),
        "deepseek": (settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model),
        "glm": (settings.zhipu_api_key, settings.glm_base_url, settings.glm_model),
        "kimi": (settings.moonshot_api_key, settings.kimi_base_url, settings.kimi_model),
        "custom": (settings.custom_llm_api_key, settings.custom_llm_base_url, settings.custom_llm_model),
    }
    if provider == "anthropic":
        return AnthropicMappingRefiner(provider, settings.anthropic_model, settings.anthropic_api_key, settings.anthropic_base_url, settings.insightflow_llm_timeout_seconds)
    if provider not in configs:
        raise ValueError("unsupported clinical mapping provider")
    api_key, base_url, model = configs[provider]
    return OpenAICompatibleMappingRefiner(provider, model, api_key, base_url, settings.insightflow_llm_timeout_seconds)

