"""Typed Temporal boundary for durable clinical investigations.

The graph remains the source of workflow semantics.  This module only translates a validated
request and an approval signal to a Temporal client.  It accepts a client by dependency injection
so tests do not need a Temporal server and deployments can choose the official ``temporalio`` SDK
without coupling the clinical tool registry to it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class TemporalWorkflowHandle(Protocol):
    async def signal(self, name: str, payload: dict[str, Any]) -> None: ...


class TemporalClient(Protocol):
    async def start_workflow(
        self,
        workflow: str,
        payload: dict[str, Any],
        *,
        id: str,
        task_queue: str,
    ) -> TemporalWorkflowHandle: ...

    def get_workflow_handle(self, workflow_id: str) -> TemporalWorkflowHandle: ...


class TemporalWorkflowInput(BaseModel):
    """Data allowed to cross the workflow boundary; no API keys or raw patient rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str = Field(min_length=1, max_length=120)
    question: str = Field(min_length=5, max_length=1000)
    trial_id: str = Field(min_length=1, max_length=80)
    published_batch_id: str | None = Field(default=None, max_length=120)
    requested_by: str = Field(min_length=1, max_length=120)
    provider: Literal["fake", "openai", "anthropic", "deepseek", "glm", "kimi", "custom"] = "fake"
    model: str | None = Field(default=None, max_length=200)
    available_domains: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    runtime_version: Literal["v17"] = "v17"


class TemporalApprovalSignal(BaseModel):
    """Human decision sent to a paused workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1, max_length=120)
    # Version is the optimistic-lock value that was checked before the decision.  The persisted
    # approval is incremented after the decision, while the workflow signal must carry the
    # pre-decision version so duplicate or stale signals can be rejected deterministically.
    version: int = Field(default=1, ge=1)
    approved: bool
    decided_by: str = Field(min_length=1, max_length=120)
    decided_at: datetime
    comment: str = Field(default="", max_length=2000)
    resume_token: str | None = Field(default=None, max_length=4096)


class TemporalCancellationSignal(BaseModel):
    """Human cancellation request delivered to a running workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested_by: str = Field(min_length=1, max_length=120)
    requested_at: datetime
    reason: str = Field(default="", max_length=2000)


class TemporalWorkflowStatus(BaseModel):
    """Metadata returned when an operator polls a durable investigation.

    This contract intentionally contains workflow and publication state only.  Clinical evidence,
    SQL, questions, and patient rows remain in the governed PostgreSQL investigation record.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=80)
    run_id: str | None = Field(default=None, max_length=120)
    clinical_status: str | None = Field(default=None, max_length=80)
    publication_status: str | None = Field(default=None, max_length=80)
    approval_id: str | None = Field(default=None, max_length=120)
    source: Literal["query", "describe"] = "query"
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TemporalClinicalWorkflowBoundary:
    """Start and signal operations with explicit workflow identity and queue."""

    workflow_name = "InsightFlowClinicalWorkflow"

    def __init__(
        self,
        client: TemporalClient,
        *,
        task_queue: str = "clinical-v20",
        workflow_name: str | None = None,
    ) -> None:
        if not task_queue.strip():
            raise ValueError("Temporal task_queue cannot be empty")
        self._client = client
        self._task_queue = task_queue
        if workflow_name is not None:
            if not workflow_name.strip():
                raise ValueError("Temporal workflow_name cannot be empty")
            self.workflow_name = workflow_name

    async def start(self, payload: TemporalWorkflowInput) -> TemporalWorkflowHandle:
        return await self._client.start_workflow(
            self.workflow_name,
            payload.model_dump(mode="json"),
            id=payload.investigation_id,
            task_queue=self._task_queue,
        )

    async def signal_approval(
        self,
        workflow_id: str,
        signal: TemporalApprovalSignal,
    ) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal("approval_decision", signal.model_dump(mode="json"))

    async def signal_cancellation(
        self,
        workflow_id: str,
        signal: TemporalCancellationSignal,
    ) -> None:
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal("cancel_investigation", signal.model_dump(mode="json"))

    async def query_status(self, workflow_id: str) -> TemporalWorkflowStatus:
        """Read a small status snapshot without copying clinical evidence into Temporal.

        Open workflows expose the typed ``status`` query.  A closed workflow may reject queries,
        so fall back to Temporal's execution description, which still contains only orchestration
        metadata.  The fallback keeps polling useful after completion without requiring a second
        clinical data read.
        """

        if not workflow_id.strip():
            raise ValueError("Temporal workflow_id cannot be empty")
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            raw = await handle.query("status")
        except Exception:
            describe = getattr(handle, "describe", None)
            if not callable(describe):
                raise
            description = await describe()
            raw_status = getattr(description, "status", "unknown")
            status = getattr(raw_status, "name", None) or getattr(raw_status, "value", None) or str(raw_status)
            return TemporalWorkflowStatus(
                workflow_id=workflow_id,
                status=status.lower(),
                run_id=getattr(description, "run_id", None),
                source="describe",
            )
        payload = raw.model_dump(mode="json") if isinstance(raw, TemporalWorkflowStatus) else dict(raw)
        payload.setdefault("workflow_id", workflow_id)
        return TemporalWorkflowStatus.model_validate(payload)

    @classmethod
    async def connect(
        cls,
        target: str,
        *,
        namespace: str = "default",
        task_queue: str = "clinical-v20",
    ) -> "TemporalClinicalWorkflowBoundary":
        """Connect using the optional official SDK only when a deployment asks for it."""

        try:
            from temporalio.client import Client
        except ImportError as exc:  # pragma: no cover - deployment-only path
            raise RuntimeError(
                "Temporal runtime is enabled but temporalio is not installed; install insightflow-backend[durable]"
            ) from exc
        client = await Client.connect(target, namespace=namespace)
        return cls(client, task_queue=task_queue)

