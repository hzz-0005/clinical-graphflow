from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from app.llm.models import AgentMode, LLMUsage
from app.clinical.models import ClinicalVerificationReport


class InvestigationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"
    PENDING_APPROVAL = "pending_approval"
    FAILED = "failed"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    TESTING = "testing"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class HypothesisKind(StrEnum):
    SEGMENT_SHIFT = "segment_shift"
    PRODUCT_BEHAVIOR = "product_behavior"
    SUPPORT_ISSUE = "support_issue"
    EXPERIMENT_EFFECT = "experiment_effect"
    TEMPORAL_EFFECT = "temporal_effect"
    CLINICAL_EFFICACY = "clinical_efficacy"
    CLINICAL_SAFETY = "clinical_safety"
    CLINICAL_SITE_QUALITY = "clinical_site_quality"
    CLINICAL_DATA_QUALITY = "clinical_data_quality"


class EvidenceType(StrEnum):
    BASELINE = "baseline"
    SEGMENT = "segment"
    BEHAVIOR = "behavior"
    EXPERIMENT = "experiment"
    SUPPORT = "support"
    ALTERNATIVE = "alternative"
    CLINICAL_OUTCOME = "clinical_outcome"
    CLINICAL_BALANCE = "clinical_balance"
    CLINICAL_MISSINGNESS = "clinical_missingness"
    CLINICAL_SITE = "clinical_site"
    CLINICAL_EXPOSURE = "clinical_exposure"
    CLINICAL_PROTOCOL_QUALITY = "clinical_protocol_quality"


class EffectSize(BaseModel):
    value: float
    unit: str
    baseline: float | None = None
    comparison: float | None = None


class Hypothesis(BaseModel):
    hypothesis_id: str | None = None
    statement: str
    kind: HypothesisKind
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    priority: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    rationale: str | None = None


class InvestigationBudget(BaseModel):
    max_steps: int = Field(default=12, ge=1, le=50)
    max_queries: int = Field(default=8, ge=1, le=30)


class InvestigationCost(BaseModel):
    steps: int = 0
    queries: int = 0
    returned_rows: int = 0


class VerificationCheck(BaseModel):
    name: str
    passed: bool
    message: str


class ConfidenceComponent(BaseModel):
    name: str
    score: float = Field(ge=0, le=1)
    reason: str


class VerificationResult(BaseModel):
    passed: bool
    checks: list[VerificationCheck] = Field(default_factory=list)
    confidence: float = Field(default=0, ge=0, le=1)
    confidence_components: list[ConfidenceComponent] = Field(default_factory=list)

    def check(self, name: str) -> VerificationCheck:
        for item in self.checks:
            if item.name == name:
                return item
        raise KeyError(name)


class InvestigationStep(BaseModel):
    sequence: int
    tool: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    summary: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class Evidence(BaseModel):
    evidence_id: str | None = None
    claim: str
    source: str
    sql: str
    params: list[Any] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    sample_size: int | None = None
    metric: str | None = None
    time_range: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    evidence_type: EvidenceType = EvidenceType.BASELINE
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    effect_size: EffectSize | None = None
    metric_version: str = "1"
    quality_flags: list[str] = Field(default_factory=list)
    observation_signal: str | None = None


class InvestigationReport(BaseModel):
    """Stable, human-readable rendering contract for every investigation result."""

    model_config = ConfigDict(extra="forbid")
    format_version: str = "1"
    synthesis_mode: Literal["external_verified", "deterministic_fallback", "inconclusive"] = "deterministic_fallback"
    direct_answer: str = Field(min_length=1, max_length=8000)
    key_findings: list[str] = Field(default_factory=list, max_length=20)
    evidence_summary: list[str] = Field(default_factory=list, max_length=30)
    limitations: list[str] = Field(default_factory=list, max_length=30)
    follow_up: list[str] = Field(default_factory=list, max_length=20)
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class InvestigationState(BaseModel):
    investigation_id: str = Field(default_factory=lambda: str(uuid4()))
    # Monotonic version of the complete state snapshot.  The enterprise repository uses this
    # value together with its row version for optimistic concurrency control; it is deliberately
    # part of the serialized state so a checkpoint/retry cannot silently overwrite a newer run.
    state_version: int = Field(default=1, ge=1)
    question: str
    domain: str = "saas_analytics"
    status: InvestigationStatus = InvestigationStatus.PENDING
    metric: str | None = None
    steps: list[InvestigationStep] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    answer: str | None = None
    report: InvestigationReport | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    baseline: dict[str, Any] = Field(default_factory=dict)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    verification: ClinicalVerificationReport | VerificationResult | None = None
    confidence: float = Field(default=0, ge=0, le=1)
    budget: InvestigationBudget = Field(default_factory=InvestigationBudget)
    cost: InvestigationCost = Field(default_factory=InvestigationCost)
    agent_mode: AgentMode = AgentMode.OFFLINE_RULES
    provider: str | None = None
    model: str | None = None
    external_model_called: bool = False
    llm_usage: LLMUsage = Field(default_factory=LLMUsage)
    audit_metadata: dict[str, Any] = Field(default_factory=dict)
    observations: list[dict[str, Any]] = Field(default_factory=list)

    def add_evidence(self, evidence: Evidence) -> Evidence:
        numbered = evidence.model_copy(
            update={"evidence_id": f"E{len(self.evidence) + 1:02d}"}
        )
        self.evidence.append(numbered)
        return numbered

    def add_hypothesis(self, hypothesis: Hypothesis) -> Hypothesis:
        numbered = hypothesis.model_copy(
            update={"hypothesis_id": f"H{len(self.hypotheses) + 1:02d}"}
        )
        self.hypotheses.append(numbered)
        return numbered

    def _hypothesis(self, hypothesis_id: str | None) -> Hypothesis:
        for item in self.hypotheses:
            if item.hypothesis_id == hypothesis_id:
                return item
        raise KeyError(hypothesis_id)

    def start_hypothesis(self, hypothesis_id: str | None) -> None:
        hypothesis = self._hypothesis(hypothesis_id)
        if hypothesis.status is not HypothesisStatus.PROPOSED:
            raise ValueError("Only a proposed hypothesis can enter testing")
        hypothesis.status = HypothesisStatus.TESTING

    def resolve_hypothesis(
        self,
        hypothesis_id: str | None,
        status: HypothesisStatus,
        evidence_ids: list[str] | None = None,
        counter_evidence_ids: list[str] | None = None,
        rationale: str | None = None,
    ) -> None:
        hypothesis = self._hypothesis(hypothesis_id)
        if hypothesis.status is not HypothesisStatus.TESTING:
            raise ValueError("A hypothesis must be testing before it can be resolved")
        if status not in {
            HypothesisStatus.SUPPORTED,
            HypothesisStatus.REJECTED,
            HypothesisStatus.INCONCLUSIVE,
        }:
            raise ValueError("A testing hypothesis needs a terminal resolution")
        hypothesis.status = status
        hypothesis.evidence_ids = evidence_ids or []
        hypothesis.counter_evidence_ids = counter_evidence_ids or []
        hypothesis.rationale = rationale

    def record_query(self, returned_rows: int) -> None:
        if self.cost.queries >= self.budget.max_queries:
            raise ValueError("Investigation query budget exceeded")
        self.cost.queries += 1
        self.cost.returned_rows += returned_rows

    def record_step(self) -> None:
        if self.cost.steps >= self.budget.max_steps:
            raise ValueError("Investigation step budget exceeded")
        self.cost.steps += 1

    def mark_inconclusive(self, answer: str) -> None:
        if not self.evidence:
            raise ValueError("An inconclusive investigation still needs evidence")
        self.answer = answer
        self.status = InvestigationStatus.INCONCLUSIVE
        self.refresh_report(synthesis_mode="inconclusive")

    def finish(self, answer: str) -> None:
        if not self.evidence:
            raise ValueError("An investigation needs evidence before it can finish")
        if not any(f"[{item.evidence_id}]" in answer for item in self.evidence):
            raise ValueError("The final answer must cite at least one evidence ID")
        self.answer = answer
        self.status = InvestigationStatus.COMPLETED
        self.refresh_report()

    def submit_for_approval(self, answer: str) -> None:
        if not self.evidence:
            raise ValueError("A clinical investigation needs evidence before approval")
        if not any(f"[{item.evidence_id}]" in answer for item in self.evidence):
            raise ValueError("The clinical conclusion must cite structured evidence")
        self.answer = answer
        self.status = InvestigationStatus.PENDING_APPROVAL
        self.refresh_report()

    def refresh_report(
        self,
        *,
        synthesis_mode: Literal["external_verified", "deterministic_fallback", "inconclusive"] = "deterministic_fallback",
        key_findings: list[str] | tuple[str, ...] | None = None,
        limitations: list[str] | tuple[str, ...] | None = None,
        follow_up: list[str] | tuple[str, ...] | None = None,
        evidence_ids: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        """Materialize a predictable report without changing the underlying answer contract."""

        def unique(values: list[str] | tuple[str, ...]) -> list[str]:
            return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

        ordered = sorted(
            self.evidence,
            key=lambda item: int(item.evidence_id[1:]) if item.evidence_id and item.evidence_id[1:].isdigit() else 10**9,
        )
        selected_ids = unique(evidence_ids or tuple(item.evidence_id or "" for item in ordered))
        evidence_by_id = {item.evidence_id: item for item in ordered if item.evidence_id}
        selected = [evidence_by_id[item] for item in selected_ids if item in evidence_by_id]
        default_findings = [
            item.claim
            for item in selected or ordered
            if item.observation_signal not in {"metric_context", "no_data", "insufficient_data"}
        ]
        summaries = [f"{item.evidence_id}：{item.claim}" for item in selected or ordered if item.evidence_id]
        report_limitations = unique(tuple(self.warnings) + tuple(limitations or ()))
        report_follow_up = unique(tuple(follow_up or ()) + tuple(self.open_questions))
        self.report = InvestigationReport(
            synthesis_mode=synthesis_mode,
            direct_answer=self.answer or "当前调查尚未形成回答。",
            key_findings=unique(key_findings or tuple(default_findings)),
            evidence_summary=summaries,
            limitations=report_limitations,
            follow_up=report_follow_up,
            evidence_ids=selected_ids,
        )

    def fail(self, error: str) -> None:
        self.errors.append(error)
        self.status = InvestigationStatus.FAILED

