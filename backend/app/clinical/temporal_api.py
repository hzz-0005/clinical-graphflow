"""Opt-in HTTP control plane for Temporal-backed clinical investigations.

The ordinary V8 endpoint remains synchronous and backward-compatible.  V20 exposes a separate
route so deployments can explicitly opt into a real Temporal server, while preserving all of the
same identity, published-batch and domain gates before a workflow is started.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.clinical.runtime_api import (
    DynamicInvestigationBody,
    resolve_published_domains,
)
from app.clinical.temporal_boundary import (
    TemporalCancellationSignal,
    TemporalClinicalWorkflowBoundary,
    TemporalWorkflowInput,
)
from app.clinical.v17_contracts import GraphEvent
from app.clinical.temporal_outbox import (
    InMemoryTemporalSignalOutbox,
    TemporalOutboxDispatcher,
    TemporalOutboxEvent,
)
from app.enterprise.api import principal_from_header
from app.enterprise.approval import (
    ApprovalConflict,
    ApprovalTokenError,
    ApprovalMachine,
    issue_resume_token,
    verify_resume_token,
)
from app.enterprise.auth import AuthorizationError, AuthorizationPolicy
from app.enterprise.models import ApprovalStatus, Capability, DataScope, Role
from app.settings import get_settings


class TemporalApprovalResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)
    approved: bool
    comment: str = Field(default="", max_length=2000)
    resume_token: str = Field(min_length=1, max_length=4096)


class TemporalCancellationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="", max_length=2000)


class TemporalOutboxDispatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=20, ge=1, le=100)


class TemporalRuntimeEventView(BaseModel):
    """Safe operational event view; clinical payloads stay in the investigation record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    node: str = Field(min_length=1, max_length=80)
    event_type: str = Field(min_length=1, max_length=40)
    recorded_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class TemporalRuntimeEventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=120)
    count: int = Field(ge=0)
    events: list[TemporalRuntimeEventView] = Field(default_factory=list)


_RUNTIME_EVENT_ALLOWED_KEYS = frozenset(
    {
        "space",
        "resumed",
        "tool_count",
        "published_domains",
        "task_ids",
        "bindings",
        "task_id",
        "hypothesis_id",
        "tool",
        "evidence_id",
        "signal",
        "decision",
        "status",
        "completed",
        "error_type",
        "node",
        "rows_returned",
        "claim_count",
        "safe_count",
        "version",
    }
)
_RUNTIME_EVENT_SENSITIVE_KEYS = frozenset(
    {
        "question",
        "query",
        "sql",
        "params",
        "rows",
        "observation",
        "observations",
        "evidence",
        "arguments",
        "error",
        "summary",
    }
)


def _redact_runtime_payload(value: Any, key: str | None = None) -> Any:
    """Defensively redact persisted event payloads before returning an operational response."""

    if key is not None and key.lower() in _RUNTIME_EVENT_SENSITIVE_KEYS:
        return None
    # This is a second boundary after the Graph's event sanitizer.  Keep it allowlist-based so a
    # legacy row or a future event key such as ``patient_id`` cannot leak through this endpoint.
    if key is not None and key.lower() not in _RUNTIME_EVENT_ALLOWED_KEYS:
        return None
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for child_key, child_value in value.items():
            cleaned = _redact_runtime_payload(child_value, str(child_key))
            if cleaned is not None:
                output[str(child_key)[:80]] = cleaned
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [cleaned for item in value if (cleaned := _redact_runtime_payload(item)) is not None]
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:200]


def _request_id(value: str | None) -> str:
    return value or str(uuid4())


def _workflow_id(value: str | None) -> str:
    """Return a UUID-backed workflow identity for the UUID-backed investigation tables.

    ``X-Request-ID`` is a correlation header and callers commonly use readable values such as
    ``support-ticket-42``.  The Temporal activity persists the workflow identity as
    ``enterprise.investigations.investigation_id`` (PostgreSQL ``uuid``), so an arbitrary header
    must never cross that boundary.  Preserve a caller-provided UUID for idempotent retries and
    allocate a fresh UUID for every other correlation value.
    """

    if value:
        try:
            return str(UUID(value))
        except (ValueError, AttributeError):
            pass
    return str(uuid4())


def _default_boundary_factory(settings: Any) -> TemporalClinicalWorkflowBoundary:
    """Connect synchronously from a FastAPI sync endpoint without leaking an async client."""

    return asyncio.run(
        TemporalClinicalWorkflowBoundary.connect(
            settings.insightflow_temporal_target,
            namespace=settings.insightflow_temporal_namespace,
            task_queue=settings.insightflow_temporal_task_queue,
        )
    )


def _require_temporal(settings: Any) -> None:
    if not bool(getattr(settings, "insightflow_temporal_enabled", False)):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "temporal_disabled",
                "message": "Temporal 工作流未启用；设置 INSIGHTFLOW_TEMPORAL_ENABLED=true 后再试",
            },
        )


def _require_approval_decision(actor: Any) -> None:
    try:
        AuthorizationPolicy().require(actor, Capability.APPROVAL_DECIDE)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc


def create_temporal_router(
    get_runtime: Callable[[], Any],
    *,
    boundary_factory: Callable[[Any], TemporalClinicalWorkflowBoundary] | None = None,
    settings_factory: Callable[[], Any] = get_settings,
) -> APIRouter:
    router = APIRouter(prefix="/api/v20/clinical")
    machine = ApprovalMachine()
    connect_boundary = boundary_factory or _default_boundary_factory
    fallback_outbox = InMemoryTemporalSignalOutbox()

    def resolve_outbox(runtime: Any) -> Any:
        # Production Runtime owns the PostgreSQL implementation. Tests and memory-only
        # deployments get the same contract without requiring a database connection.
        return getattr(runtime, "temporal_signal_outbox", None) or fallback_outbox

    def build_dispatcher(runtime: Any, settings: Any) -> TemporalOutboxDispatcher:
        return TemporalOutboxDispatcher(
            resolve_outbox(runtime),
            boundary_factory=connect_boundary,
            approval_repository=runtime.enterprise_repository,
            settings=settings,
            max_attempts=int(getattr(settings, "insightflow_temporal_outbox_max_attempts", 5)),
        )

    @router.post("/workflows", status_code=202)
    def start_workflow(
        body: DynamicInvestigationBody,
        x_insightflow_user: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        settings = settings_factory()
        _require_temporal(settings)
        actor = principal_from_header(x_insightflow_user)
        if actor.role is Role.VIEWER:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})

        runtime = get_runtime()
        scope = DataScope.from_principal(actor)
        if not scope.allows_clinical(body.trial_id):
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        try:
            domains = resolve_published_domains(runtime, actor, scope, body)
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "temporal_scope_invalid", "message": str(exc)},
            ) from exc

        request_id = _request_id(x_request_id)
        workflow_id = _workflow_id(request_id)
        payload = TemporalWorkflowInput(
            investigation_id=workflow_id,
            question=body.question,
            trial_id=body.trial_id,
            published_batch_id=body.published_batch_id,
            requested_by=actor.user_id,
            provider=body.provider,
            model=body.model,
            available_domains=tuple(sorted(domains)),
        )
        try:
            boundary = connect_boundary(settings)
            asyncio.run(boundary.start(payload))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "temporal_unavailable", "message": str(exc)},
            ) from exc
        return {
            "workflow_id": workflow_id,
            "status": "started",
            "runtime_version": "v17",
            "task_queue": settings.insightflow_temporal_task_queue,
            "provider": body.provider,
            "available_domains": sorted(domains),
            "request_id": request_id,
        }

    @router.get("/workflows/{workflow_id}/status")
    def workflow_status(
        workflow_id: str,
        x_insightflow_user: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Poll durable orchestration state without returning clinical evidence.

        The Temporal query is deliberately separate from ``GET /api/v3/investigations``.  It is
        safe for a UI poller to call repeatedly, while the normal investigation endpoint remains
        the source for the governed answer and evidence chain.
        """

        settings = settings_factory()
        _require_temporal(settings)
        actor = principal_from_header(x_insightflow_user)
        try:
            AuthorizationPolicy().require(actor, Capability.INVESTIGATION_READ)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc

        runtime = get_runtime()
        # A non-admin may poll only an investigation visible in the enterprise repository.  An
        # administrator can poll during the short window before the activity creates its row.
        if actor.role is not Role.ADMIN:
            repository = getattr(runtime, "enterprise_repository", None)
            get_investigation = getattr(repository, "get_investigation", None)
            if not callable(get_investigation):
                raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
            try:
                get_investigation(workflow_id, actor)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc

        try:
            status = asyncio.run(connect_boundary(settings).query_status(workflow_id))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "temporal_unavailable", "message": str(exc)},
            ) from exc
        return status.model_dump(mode="json")

    @router.get("/workflows/{workflow_id}/events", response_model=TemporalRuntimeEventsResponse)
    def workflow_events(
        workflow_id: str,
        x_insightflow_user: str | None = Header(default=None),
    ) -> TemporalRuntimeEventsResponse:
        """Return only the bounded, redacted runtime event stream for one workflow.

        The durable investigation row is the source for this operational view.  It intentionally
        omits the question, SQL, arguments, evidence, result rows, and exception text; callers
        needing those governed facts must use the separately authorized investigation endpoint.
        """

        settings = settings_factory()
        _require_temporal(settings)
        actor = principal_from_header(x_insightflow_user)
        try:
            AuthorizationPolicy().require(actor, Capability.INVESTIGATION_READ)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc

        repository = getattr(get_runtime(), "enterprise_repository", None)
        get_investigation = getattr(repository, "get_investigation", None)
        if not callable(get_investigation):
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        try:
            item = get_investigation(workflow_id, actor)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc

        metadata = getattr(getattr(item, "state", None), "audit_metadata", {}) or {}
        raw_events = metadata.get("graph_events", []) if isinstance(metadata, dict) else []
        events: list[TemporalRuntimeEventView] = []
        for raw_event in raw_events if isinstance(raw_events, list) else []:
            try:
                event = GraphEvent.model_validate(raw_event)
            except (TypeError, ValueError):
                # A legacy or partially migrated record must not make the operational endpoint
                # leak an arbitrary mapping or fail the entire query.
                continue
            payload = _redact_runtime_payload(event.payload)
            events.append(
                TemporalRuntimeEventView(
                    sequence=event.sequence,
                    node=event.node,
                    event_type=event.event_type,
                    recorded_at=event.recorded_at,
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
        events.sort(key=lambda event: event.sequence)
        return TemporalRuntimeEventsResponse(workflow_id=workflow_id, count=len(events), events=events)

    @router.post("/approvals/{approval_id}/resume-token")
    def mint_resume_token(
        approval_id: str,
        x_insightflow_user: str | None = Header(default=None),
    ) -> dict[str, Any]:
        settings = settings_factory()
        _require_temporal(settings)
        actor = principal_from_header(x_insightflow_user)
        _require_approval_decision(actor)
        secret = str(getattr(settings, "insightflow_approval_resume_secret", ""))
        if not secret:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "approval_resume_secret_missing",
                    "message": "未配置审批恢复令牌密钥",
                },
            )
        try:
            item = get_runtime().enterprise_repository.get_approval(approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        if item.status is not ApprovalStatus.PENDING:
            raise HTTPException(
                status_code=409,
                detail={"code": "approval_not_pending", "message": "审批已经结束，不能再签发恢复令牌"},
            )
        token = issue_resume_token(
            item,
            secret=secret,
            ttl_seconds=int(getattr(settings, "insightflow_approval_resume_ttl_seconds", 900)),
        )
        return {
            "approval_id": item.approval_id,
            "investigation_id": item.investigation_id,
            "version": item.version,
            "resume_token": token,
            "expires_in_seconds": int(getattr(settings, "insightflow_approval_resume_ttl_seconds", 900)),
        }

    @router.post("/workflows/{workflow_id}/approval")
    def resume_workflow(
        workflow_id: str,
        body: TemporalApprovalResumeBody,
        x_insightflow_user: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        settings = settings_factory()
        _require_temporal(settings)
        actor = principal_from_header(x_insightflow_user)
        _require_approval_decision(actor)
        secret = str(getattr(settings, "insightflow_approval_resume_secret", ""))
        if not secret:
            raise HTTPException(
                status_code=503,
                detail={"code": "approval_resume_secret_missing", "message": "未配置审批恢复令牌密钥"},
            )
        runtime = get_runtime()
        try:
            repository = runtime.enterprise_repository
            item = repository.get_approval(body.approval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        if item.investigation_id != workflow_id:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        try:
            verify_resume_token(
                body.resume_token,
                secret=secret,
                expected_approval_id=item.approval_id,
                expected_investigation_id=workflow_id,
                expected_version=item.version,
            )
            decided = (
                machine.approve(item, actor, body.version, body.comment)
                if body.approved
                else machine.reject(item, actor, body.version, body.comment)
            )
        except ApprovalTokenError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "approval_token_invalid", "message": str(exc)},
            ) from exc
        except ApprovalConflict as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "approval_conflict", "message": str(exc)},
            ) from exc

        request_id = _request_id(x_request_id)
        event = TemporalOutboxEvent(
            workflow_id=workflow_id,
            approval_id=item.approval_id,
            expected_version=item.version,
            approved=body.approved,
            decided_by=actor.user_id,
            decided_at=decided.decided_at or datetime.now(timezone.utc),
            comment=body.comment,
        )
        try:
            outbox = resolve_outbox(runtime)
            atomic_commit = getattr(repository, "save_approval_and_enqueue", None)
            if callable(atomic_commit):
                atomic_commit(decided, actor, request_id, event, outbox)
            else:
                repository.save_approval(decided, actor, request_id)
                outbox.enqueue(event)
            delivered = asyncio.run(
                build_dispatcher(runtime, settings).dispatch(event, resume_token=body.resume_token)
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "approval_conflict", "message": str(exc)},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "approval_outbox_unavailable", "message": str(exc)},
            ) from exc

        response = {
            "status": {
                "sent": "signal_sent",
                "pending": "signal_pending",
                "dead": "signal_dead",
            }[delivered.status],
            "workflow_id": workflow_id,
            "approval": decided.model_dump(mode="json"),
            "outbox_event_id": delivered.event_id,
            "outbox_attempts": delivered.attempts,
        }
        if delivered.last_error:
            response["outbox_last_error"] = delivered.last_error
        if delivered.status != "sent":
            # The decision is already durably recorded; an operator/worker can retry the
            # metadata-only event later without asking the approver for another bearer token.
            from fastapi.responses import JSONResponse

            return JSONResponse(status_code=503 if delivered.status == "dead" else 202, content=response)
        return response

    @router.post("/workflows/{workflow_id}/cancel")
    def cancel_workflow(
        workflow_id: str,
        body: TemporalCancellationBody,
        x_insightflow_user: str | None = Header(default=None),
    ) -> dict[str, Any]:
        settings = settings_factory()
        _require_temporal(settings)
        actor = principal_from_header(x_insightflow_user)
        _require_approval_decision(actor)
        signal = TemporalCancellationSignal(
            requested_by=actor.user_id,
            requested_at=datetime.now(timezone.utc),
            reason=body.reason,
        )
        try:
            boundary = connect_boundary(settings)
            asyncio.run(boundary.signal_cancellation(workflow_id, signal))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "temporal_unavailable", "message": str(exc)},
            ) from exc
        return {
            "status": "cancel_requested",
            "workflow_id": workflow_id,
            "requested_by": actor.user_id,
        }

    @router.post("/outbox/dispatch")
    def dispatch_outbox(
        body: TemporalOutboxDispatchBody,
        x_insightflow_user: str | None = Header(default=None),
    ) -> dict[str, Any]:
        settings = settings_factory()
        _require_temporal(settings)
        actor = principal_from_header(x_insightflow_user)
        _require_approval_decision(actor)
        runtime = get_runtime()
        try:
            events = asyncio.run(build_dispatcher(runtime, settings).dispatch_pending(limit=body.limit))
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "approval_outbox_unavailable", "message": str(exc)},
            ) from exc
        return {
            "status": "dispatched",
            "count": len(events),
            "events": [event.model_dump(mode="json") for event in events],
        }

    return router

