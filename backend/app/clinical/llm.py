from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from app.clinical.registry import CLINICAL_TOOL_NAMES
from app.llm.models import LLMUsage, LLMCallUsage


class ClinicalPlanAction(BaseModel):
    model_config = ConfigDict(frozen=True)
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""

    @field_validator("tool")
    @classmethod
    def governed_tool_only(cls, value: str) -> str:
        if value not in CLINICAL_TOOL_NAMES or value == "submit_clinical_conclusion":
            raise ValueError("only governed clinical query tools are allowed")
        return value


class ClinicalPlan(BaseModel):
    model_config = ConfigDict(frozen=True)
    # Keep the legacy typed plan aligned with the governed registry.  The current registry has
    # sixteen read-only query tools (plus the separate approval action); this bound is a safety
    # ceiling, not a request to execute every tool in one investigation.
    actions: tuple[ClinicalPlanAction, ...] = Field(min_length=1, max_length=16)

    @classmethod
    def default(cls, trial_id: str, region: str | None = None) -> "ClinicalPlan":
        """Bounded legacy plan used by the pre-V8 ``/investigations/llm`` endpoint.

        A plan produced *before* any query runs cannot know which site to drill into, so it must not
        name one. Site-level execution checks (``inspect_treatment_exposure`` /
        ``inspect_protocol_quality``) are therefore selected at runtime from observed results by the
        V8 dynamic runtime (``/api/v8/clinical/investigations``), never hardcoded here.
        """
        subgroup = {"dimension": "region", "value": region} if region else None
        return cls(actions=(
            ClinicalPlanAction(tool="inspect_trial", arguments={"trial_id": trial_id}),
            ClinicalPlanAction(tool="compare_treatment_effect", arguments={"trial_id": trial_id, "subgroup": subgroup}),
            ClinicalPlanAction(tool="check_randomization_balance", arguments={"trial_id": trial_id, "subgroup": subgroup}),
            ClinicalPlanAction(tool="analyze_missingness", arguments={"trial_id": trial_id, "subgroup": subgroup}),
            ClinicalPlanAction(tool="profile_sites", arguments={"trial_id": trial_id, "subgroup": subgroup}),
        ))


class ClinicalPlanResult(BaseModel):
    plan: ClinicalPlan
    usage: LLMUsage = Field(default_factory=LLMUsage)
    provider: str
    model: str
    request_id: str | None = None


class ClinicalLLMClient(Protocol):
    provider: str
    model: str
    def plan(self, question: str, trial_id: str, region: str | None) -> ClinicalPlanResult: ...


class FakeClinicalLLMClient:
    provider = "fake"
    model = "deterministic-clinical-plan"

    def __init__(self, plan: ClinicalPlan | None = None) -> None:
        self._plan = plan

    def plan(self, question: str, trial_id: str, region: str | None) -> ClinicalPlanResult:
        usage = LLMUsage()
        usage.add(LLMCallUsage(input_tokens=20, output_tokens=10))
        return ClinicalPlanResult(plan=self._plan or ClinicalPlan.default(trial_id, region), usage=usage, provider=self.provider, model=self.model)


Transport = Callable[[str, dict[str, str], dict[str, Any], float], dict[str, Any]]


class OpenAICompatibleClinicalClient:
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init__(self, provider: str, model: str, api_key: str, base_url: str, timeout: float = 60, transport: Transport | None = None) -> None:
        if not api_key:
            raise ValueError(f"{provider} API key is not configured")
        self.provider = provider
        self.model = model
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._transport = transport or self._post

    def plan(self, question: str, trial_id: str, region: str | None) -> ClinicalPlanResult:
        prompt = self._prompt(question, trial_id, region)
        payload = {"model": self.model, "messages": [{"role": "system", "content": "You are a governed clinical analytics planner. Output valid JSON."}, {"role": "user", "content": prompt}], "response_format": {"type": "json_object"}, "temperature": 0, "max_tokens": 1400}
        if self.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}
        started = time.perf_counter()
        data = self._transport(f"{self._base_url}/chat/completions", {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}, payload, self._timeout)
        elapsed = int((time.perf_counter() - started) * 1000)
        content = data["choices"][0]["message"]["content"]
        return self._result(data, content, elapsed, "prompt_tokens", "completion_tokens")

    @staticmethod
    def _prompt(question: str, trial_id: str, region: str | None) -> str:
        return (
            "Return JSON only with an actions array. Select a bounded investigation plan from these governed tools: "
            + ", ".join(name for name in CLINICAL_TOOL_NAMES if name != "submit_clinical_conclusion")
            + ". Never emit SQL. Return no more than 15 actions. Use trial_id and optional region. Include efficacy, alternatives, site drill-down, exposure and protocol quality. "
            + f"Question={question}; trial_id={trial_id}; region={region or 'none'}"
        )
    def _result(self, data: dict[str, Any], content: str, elapsed: int, input_key: str, output_key: str) -> ClinicalPlanResult:
        plan = ClinicalPlan.model_validate(json.loads(content))
        raw_usage = data.get("usage", {})
        usage = LLMUsage()
        usage.add(LLMCallUsage(input_tokens=raw_usage.get(input_key), output_tokens=raw_usage.get(output_key), cached_tokens=(raw_usage.get("prompt_cache_hit_tokens") or 0), latency_ms=elapsed))
        return ClinicalPlanResult(plan=plan, usage=usage, provider=self.provider, model=self.model, request_id=data.get("id"))

    @staticmethod
    def _post(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = httpx.post(url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()


class AnthropicClinicalClient(OpenAICompatibleClinicalClient):
    def plan(self, question: str, trial_id: str, region: str | None) -> ClinicalPlanResult:
        payload = {"model": self.model, "system": "You are a governed clinical analytics planner. Return valid JSON only.", "messages": [{"role": "user", "content": self._prompt(question, trial_id, region)}], "temperature": 0, "max_tokens": 1400}
        started = time.perf_counter()
        data = self._transport(f"{self._base_url}/v1/messages", {"x-api-key": self._api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}, payload, self._timeout)
        elapsed = int((time.perf_counter() - started) * 1000)
        content = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
        return self._result(data, content, elapsed, "input_tokens", "output_tokens")


def build_clinical_llm_client(settings, provider: str, model: str | None = None) -> ClinicalLLMClient:
    configs = {
        "openai": (settings.openai_api_key, settings.openai_base_url, model or settings.openai_model),
        "deepseek": (settings.deepseek_api_key, settings.deepseek_base_url, model or settings.deepseek_model),
        "glm": (settings.zhipu_api_key, settings.glm_base_url, model or settings.glm_model),
        "kimi": (settings.moonshot_api_key, settings.kimi_base_url, model or settings.kimi_model),
        "custom": (settings.custom_llm_api_key, settings.custom_llm_base_url, model or settings.custom_llm_model),
    }
    if provider == "fake":
        return FakeClinicalLLMClient()
    if provider == "anthropic":
        return AnthropicClinicalClient(provider, model or settings.anthropic_model, settings.anthropic_api_key, settings.anthropic_base_url)
    if provider not in configs:
        raise ValueError("unsupported clinical LLM provider")
    api_key, base_url, resolved_model = configs[provider]
    if not resolved_model:
        raise ValueError(f"{provider} model is not configured")
    return OpenAICompatibleClinicalClient(provider, resolved_model, api_key, base_url, settings.insightflow_llm_timeout_seconds)

