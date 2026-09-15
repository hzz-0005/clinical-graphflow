from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field, computed_field


class LLMProvider(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    GLM = "glm"
    KIMI = "kimi"
    CUSTOM = "custom_openai_compatible"


class AgentMode(StrEnum):
    OFFLINE_RULES = "offline_rules"
    LIVE = "live"
    TEST_FAKE = "test_fake"


class LLMAction(StrEnum):
    SEARCH_METRICS = "search_metrics"
    SEARCH_DATA_CATALOG = "search_data_catalog"
    INSPECT_MODEL = "inspect_model"
    QUERY_METRIC = "query_metric"
    PROFILE_DIMENSION = "profile_dimension"
    COMPARE_SEGMENTS = "compare_segments"
    INSPECT_EXPERIMENT = "inspect_experiment"
    SUBMIT_CONCLUSION = "submit_conclusion"


class LLMDecision(BaseModel):
    action: LLMAction
    rationale: str = Field(min_length=1, max_length=500)
    hypothesis_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class LLMDecisionRequest(BaseModel):
    question: str
    state: dict[str, Any]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    remaining_steps: int
    remaining_queries: int


class LLMCallUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    latency_ms: int = 0
    retry_count: int = 0

    @computed_field
    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class LLMUsage(BaseModel):
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_latency_ms: int = 0
    retry_count: int = 0

    @computed_field
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add(self, call: LLMCallUsage) -> None:
        self.request_count += 1
        self.input_tokens += call.input_tokens or 0
        self.output_tokens += call.output_tokens or 0
        self.cached_tokens += call.cached_tokens or 0
        self.total_latency_ms += call.latency_ms
        self.retry_count += call.retry_count


class LLMCallResult(BaseModel):
    decision: LLMDecision
    usage: LLMCallUsage = Field(default_factory=LLMCallUsage)
    provider_request_id: str | None = None


class LLMClient(Protocol):
    provider: str
    model: str
    def decide(self, request: LLMDecisionRequest) -> LLMCallResult: ...

