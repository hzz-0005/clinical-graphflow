"""Optional Temporal Workflow/Worker definition for V21.

The workflow itself only coordinates an activity and waits for a typed approval signal.  Clinical
queries stay in the activity, where the existing runtime factory, MCP gateway, PostgreSQL scope,
and evidence rules remain in force.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Iterable

from app.clinical.temporal_boundary import (
    TemporalApprovalSignal,
    TemporalCancellationSignal,
    TemporalWorkflowInput,
)

try:  # Keep local V16/V17 tests runnable without installing the optional Temporal SDK.
    from temporalio import workflow

    TEMPORAL_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - environment-dependent
    workflow = None
    TEMPORAL_SDK_AVAILABLE = False


if TEMPORAL_SDK_AVAILABLE:

    from temporalio.common import RetryPolicy

    @workflow.defn(name="InsightFlowClinicalWorkflow")
    class InsightFlowClinicalWorkflow:
        workflow_name = "InsightFlowClinicalWorkflow"
        approval_signal_name = "approval_decision"
        cancellation_signal_name = "cancel_investigation"

        def __init__(self) -> None:
            self._approval: dict[str, Any] | None = None
            self._cancelled: dict[str, Any] | None = None
            self._activity_result: dict[str, Any] = {}
            self._workflow_id = ""
            self._workflow_status = "created"

        @workflow.run
        async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            # Temporal serializes this plain dictionary; validation happens before start at the
            # boundary and again inside the activity before any data access.
            validated = TemporalWorkflowInput.model_validate(payload)
            self._workflow_id = validated.investigation_id
            self._workflow_status = "running"
            result = await workflow.execute_activity(
                "execute_clinical_investigation_activity",
                payload,
                start_to_close_timeout=timedelta(minutes=30),
                schedule_to_close_timeout=timedelta(hours=2),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2),
                    backoff_coefficient=2.0,
                    maximum_interval=timedelta(seconds=30),
                    maximum_attempts=3,
                    # Invalid identity/payload errors cannot be repaired by retrying the same
                    # activity.  Provider/network failures remain retryable.
                    non_retryable_error_types=("TemporalActivityError",),
                ),
            )
            self._activity_result = dict(result) if isinstance(result, dict) else {}
            if isinstance(result, dict) and result.get("status") == "pending_approval":
                self._workflow_status = "pending_approval"
                try:
                    await workflow.wait_condition(
                        lambda: self._approval is not None or self._cancelled is not None,
                        timeout=timedelta(days=7),
                        timeout_summary="clinical approval timeout",
                    )
                except asyncio.TimeoutError:
                    pass
                if self._cancelled is not None:
                    self._workflow_status = "cancelled"
                    return {**result, "status": "cancelled", "cancellation": self._cancelled}
                # Temporal's wait_condition returns None whether the predicate became true or
                # the timeout elapsed; inspect the state rather than treating the return value as
                # a boolean signal.
                if self._approval is None:
                    self._workflow_status = "approval_timeout"
                    return {**result, "status": "approval_timeout"}
                self._workflow_status = "approved" if self._approval.get("approved") else "rejected"
                return {**result, "approval": self._approval}
            self._workflow_status = "completed"
            return result

        @workflow.query(name="status")
        def status(self) -> dict[str, Any]:
            """Return orchestration metadata without exposing question, SQL, or evidence rows."""

            return {
                "workflow_id": self._workflow_id,
                "status": self._workflow_status,
                "clinical_status": self._activity_result.get("status"),
                "publication_status": self._activity_result.get("publication_status"),
                "approval_id": self._activity_result.get("approval_id"),
            }

        @workflow.signal(name="approval_decision")
        async def approval_decision(self, payload: dict[str, Any]) -> None:
            signal = TemporalApprovalSignal.model_validate(payload)
            if not signal.resume_token:
                raise ValueError("approval resume token is required")
            if self._approval is not None:
                previous = TemporalApprovalSignal.model_validate(self._approval)
                if (
                    previous.approval_id == signal.approval_id
                    and previous.version == signal.version
                ):
                    return
                raise ValueError("approval signal version conflict")
            if self._cancelled is not None:
                return
            self._approval = signal.model_dump(mode="json")
            self._workflow_status = "approved" if signal.approved else "rejected"

        @workflow.signal(name="cancel_investigation")
        async def cancel_investigation(self, payload: dict[str, Any]) -> None:
            signal = TemporalCancellationSignal.model_validate(payload)
            if self._approval is not None:
                return
            if self._cancelled is None:
                self._cancelled = signal.model_dump(mode="json")
                self._workflow_status = "cancelled"

else:

    class InsightFlowClinicalWorkflow:
        """Metadata-only fallback used when the optional Temporal SDK is absent."""

        workflow_name = "InsightFlowClinicalWorkflow"
        approval_signal_name = "approval_decision"
        cancellation_signal_name = "cancel_investigation"

        async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
            del payload
            raise RuntimeError("temporalio is required to execute InsightFlowClinicalWorkflow")

        async def approval_decision(self, payload: dict[str, Any]) -> None:
            del payload
            raise RuntimeError("temporalio is required to signal InsightFlowClinicalWorkflow")

        async def cancel_investigation(self, payload: dict[str, Any]) -> None:
            del payload
            raise RuntimeError("temporalio is required to signal InsightFlowClinicalWorkflow")

        def status(self) -> dict[str, Any]:
            raise RuntimeError("temporalio is required to query InsightFlowClinicalWorkflow")


def build_temporal_worker(
    client: Any,
    *,
    task_queue: str,
    activities: Iterable[Any],
) -> Any:
    """Build a Worker only when the optional SDK is installed."""

    if not TEMPORAL_SDK_AVAILABLE:
        raise RuntimeError(
            "Temporal worker is enabled but temporalio is not installed; install insightflow-backend[temporal]"
        )
    if not task_queue.strip():
        raise ValueError("Temporal task_queue cannot be empty")
    from temporalio.worker import Worker

    return Worker(
        client,
        task_queue=task_queue,
        workflows=[InsightFlowClinicalWorkflow],
        activities=list(activities),
    )

