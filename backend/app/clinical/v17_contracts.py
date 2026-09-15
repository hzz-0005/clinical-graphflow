from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.models import InvestigationState, InvestigationStatus
from app.clinical.analysis_plan import InvestigationPlan


GraphNodeName = Literal[
    "route_question",
    "load_context",
    "generate_plan",
    "validate_plan",
    "select_task",
    "propose_hypothesis",
    "execute_task",
    "interpret_observation",
    "advance_task",
    "verify_coverage",
    "synthesize_report",
    "finish",
]

# Durable graph snapshots are intentionally versioned independently from the V16 response model.
# A schema bump is required before changing any persisted graph field, while the enterprise row
# version remains the optimistic-lock/CAS version for one investigation.
GRAPH_STATE_SCHEMA_VERSION = "v17.graph_state.v1"

TaskExecutionPhase = Literal[
    "select_task",
    "propose_hypothesis",
    "execute_task",
    "interpret_observation",
    "advance_task",
]

ApprovalState = Literal["not_required", "pending", "approved", "rejected", "cancelled"]


class ToolCallRequest(BaseModel):
    """One auditable request from the graph to a governed clinical tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^A[1-9][0-9]*$")
    # V16 evidence IDs are rendered as H01, while provider-authored test contracts may use H1.
    # Accept both forms but reject an all-zero identifier.
    hypothesis_id: str = Field(pattern=r"^H0*[1-9][0-9]*$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolObservation(BaseModel):
    """Normalized tool output; raw rows remain evidence-owned, not model-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^A[1-9][0-9]*$")
    hypothesis_id: str = Field(pattern=r"^H0*[1-9][0-9]*$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    source: str = Field(min_length=1, max_length=500)
    sql: str = ""
    params: tuple[Any, ...] = ()
    rows: list[dict[str, Any]] = Field(default_factory=list)
    signal: str | None = None
    summary: str | None = Field(default=None, max_length=2000)
    warnings: tuple[str, ...] = ()


class TaskExecutionCursor(BaseModel):
    """Durable cursor for the current task-level graph transition.

    The cursor is deliberately separate from ``active_task_id``.  The latter is a legacy
    response field, while this model records exactly which typed node may run next.  Keeping
    the request arguments here also makes a resumed execution deterministic without asking the
    planner to regenerate anything.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^A[1-9][0-9]*$")
    phase: TaskExecutionPhase
    hypothesis_id: str | None = Field(default=None, pattern=r"^H0*[1-9][0-9]*$")
    tool_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,127}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class PendingToolExecution(BaseModel):
    """The governed tool result waiting for the interpretation node.

    This is not a clinical fact store.  It is a typed checkpoint payload written immediately
    after the one gateway call so an interpreter retry never calls the tool again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(pattern=r"^A[1-9][0-9]*$")
    hypothesis_id: str = Field(pattern=r"^H0*[1-9][0-9]*$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    source: str = Field(min_length=1, max_length=500)
    sql: str = ""
    params: tuple[Any, ...] = ()
    rows: list[dict[str, Any]] = Field(default_factory=list)
    warnings: tuple[str, ...] = ()
    minimum_cell_size: int = 10
    missing_supporting_domains: tuple[str, ...] = ()


# Short names make the checkpoint schema convenient for callers that only care about the graph
# contract.  The canonical class names above remain explicit in generated JSON schemas.
TaskCursor = TaskExecutionCursor
PendingExecution = PendingToolExecution


class CoverageDecision(BaseModel):
    """The verifier's decision for one independently answerable question part."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["continue", "approval", "complete", "gap"]
    requirement_id: str = Field(pattern=r"^R[1-9][0-9]*$")
    evidence_ids: tuple[str, ...] = ()
    gap: str | None = Field(default=None, min_length=4, max_length=1000)
    rationale: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def validate_coverage(self) -> "CoverageDecision":
        if self.status == "gap" and not self.gap:
            raise ValueError("a gap decision needs a specific gap")
        if self.status in {"complete", "approval"} and not self.evidence_ids and not self.gap:
            raise ValueError("a completed or approval decision needs evidence or a gap")
        return self


class GraphEvent(BaseModel):
    """Append-only event used for audit and future telemetry adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    node: GraphNodeName
    event_type: Literal["entered", "completed", "decision", "error"]
    payload: dict[str, Any] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InvestigationGraphState(BaseModel):
    """Typed graph envelope around the V16 state object.

    The embedded legacy state is deliberately retained during migration.  This lets the
    V17 graph use the existing persistence and API serializer while every new transition
    remains explicit and replayable.
    """

    model_config = ConfigDict(extra="forbid")

    # This marker is part of the durable payload, not merely application metadata.  It lets a
    # worker fail closed when it sees a checkpoint created by an incompatible graph schema.
    schema_version: Literal["v17.graph_state.v1"] = GRAPH_STATE_SCHEMA_VERSION
    # ``state_version`` is the enterprise snapshot version mirrored in the embedded compatibility
    # projection.  ``enterprise_version`` is retained separately so a checkpoint can prove which
    # PostgreSQL row version it was derived from before a resume/write.
    state_version: int = Field(default=1, ge=1)
    enterprise_version: int | None = Field(default=None, ge=1)
    checkpoint_version: int = Field(default=0, ge=0)
    approval_status: ApprovalState = "not_required"
    approval_version: int = Field(default=0, ge=0)
    publication_status: str = "draft"
    legacy: InvestigationState
    node: GraphNodeName = "route_question"
    trial_id: str | None = None
    published_batch_id: str | None = None
    space: str = "clinical_trial"
    plan: InvestigationPlan | None = None
    completed_task_ids: tuple[str, ...] = ()
    active_task_id: str | None = None
    active_hypothesis_id: str | None = None
    task_cursor: TaskExecutionCursor | None = None
    pending_execution: PendingToolExecution | None = None
    observations: list[ToolObservation] = Field(default_factory=list)
    coverage: list[CoverageDecision] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)
    events: list[GraphEvent] = Field(default_factory=list)

    @classmethod
    def from_legacy_state(
        cls,
        state: InvestigationState,
        *,
        trial_id: str | None = None,
        published_batch_id: str | None = None,
        space: str = "clinical_trial",
    ) -> "InvestigationGraphState":
        approval_status: ApprovalState = "pending" if state.status is InvestigationStatus.PENDING_APPROVAL else "not_required"
        return cls(
            legacy=state,
            state_version=state.state_version,
            approval_status=approval_status,
            approval_version=1 if approval_status == "pending" else 0,
            trial_id=trial_id,
            published_batch_id=published_batch_id,
            space=space,
            observations=[],
        )

    @classmethod
    def from_canonical_snapshot(cls, payload: str | bytes | bytearray) -> "InvestigationGraphState":
        """Decode a durable graph payload and reject unknown schema versions."""

        state = cls.model_validate_json(payload)
        if state.schema_version != GRAPH_STATE_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported graph state schema: {state.schema_version!r}"
            )
        state.assert_internal_version_consistency()
        return state

    def assert_internal_version_consistency(self) -> None:
        """Ensure the graph and its compatibility projection describe one snapshot."""

        if self.legacy.state_version != self.state_version:
            raise ValueError(
                "graph state version does not match its legacy compatibility projection"
            )
        if self.enterprise_version is not None and self.enterprise_version != self.state_version:
            raise ValueError("graph enterprise version does not match graph state version")

    def with_enterprise_version(self, version: int) -> "InvestigationGraphState":
        """Return the canonical state projected at one enterprise row version."""

        if version < 1:
            raise ValueError("enterprise version must be positive")
        projected = self.legacy.model_copy(update={"state_version": version}, deep=True)
        return self.model_copy(
            update={
                "state_version": version,
                "enterprise_version": version,
                "legacy": projected,
            },
            deep=True,
        )

    def with_approval(self, status: ApprovalState, version: int) -> "InvestigationGraphState":
        """Return a snapshot carrying the approval lifecycle for the same investigation."""

        if version < 0:
            raise ValueError("approval version must be non-negative")
        return self.model_copy(
            update={"approval_status": status, "approval_version": version},
            deep=True,
        )

    @property
    def question(self) -> str:
        return self.legacy.question

    @property
    def investigation_id(self) -> str:
        return self.legacy.investigation_id

    def record_event(
        self,
        node: GraphNodeName,
        event_type: Literal["entered", "completed", "decision", "error"],
        payload: dict[str, Any] | None = None,
    ) -> GraphEvent:
        event = GraphEvent(
            sequence=len(self.events) + 1,
            node=node,
            event_type=event_type,
            payload=payload or {},
        )
        self.events.append(event)
        self.node = node
        # Every event is a new replayable checkpoint boundary.  This cursor is deliberately
        # independent from the enterprise CAS version; the latter changes only on a durable
        # investigation write.
        self.checkpoint_version = max(self.checkpoint_version + 1, event.sequence)
        return event

    def to_legacy_state(self) -> InvestigationState:
        """Return an isolated V16-compatible state with V17 audit details attached."""

        restored = self.legacy.model_copy(deep=True)
        if self.observations:
            restored.observations = [item.model_dump(mode="json") for item in self.observations]
        metadata = dict(restored.audit_metadata)
        metadata.update(
            {
                "runtime_graph_node": self.node,
                "graph_events": [item.model_dump(mode="json") for item in self.events],
                "graph_completed_task_ids": list(self.completed_task_ids),
                "graph_data_gaps": list(self.data_gaps),
            }
        )
        if self.plan is not None:
            metadata["graph_plan"] = self.plan.model_dump(mode="json")
        if self.trial_id is not None:
            metadata["trial_id"] = self.trial_id
        if self.published_batch_id is not None:
            metadata["published_batch_id"] = self.published_batch_id
        restored.audit_metadata = metadata
        return restored

