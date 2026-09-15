"""Temporal Activity adapter for the governed clinical investigation runtime.

Temporal coordinates a workflow, but it must not become a second clinical fact store.  This
adapter is the only place where a workflow payload is turned into the existing dynamic runtime
request.  It persists the resulting investigation through the normal repository and returns only
small workflow metadata; evidence rows remain queryable through the protected API/PostgreSQL
path instead of being copied into Temporal history.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.clinical.temporal_boundary import TemporalWorkflowInput
from app.clinical.v17_contracts import InvestigationGraphState
from app.enterprise.models import Principal

try:  # Keep the ordinary API importable without the optional Temporal SDK.
    from temporalio import activity as temporal_activity
except ImportError:  # pragma: no cover - environment-dependent optional dependency
    temporal_activity = None


class TemporalActivityError(RuntimeError):
    """The durable activity cannot safely execute the supplied request."""


class TemporalActivityResult(BaseModel):
    """Metadata returned to a Temporal workflow, deliberately excluding evidence rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1, max_length=120)
    investigation_id: str = Field(min_length=1, max_length=120)
    status: str = Field(min_length=1, max_length=40)
    publication_status: str = Field(min_length=1, max_length=40)
    approval_id: str | None = Field(default=None, max_length=120)
    runtime_version: Literal["v17"] = "v17"


RuntimeFactory = Callable[[], Any]
PrincipalResolver = Callable[[str], Principal | None]
ExecuteDynamic = Callable[..., Any]
PersistDynamic = Callable[[Any, Principal, Any, str], str]


def _activity_definition(function: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate the registered callable only when the optional SDK is installed."""

    if temporal_activity is None:
        return function
    return temporal_activity.defn(name="execute_clinical_investigation_activity")(function)


def _default_runtime_factory() -> Any:
    from app.main import build_runtime

    return build_runtime()


def _default_principal_resolver(user_id: str) -> Principal | None:
    from app.enterprise.api import DEV_USERS

    return DEV_USERS.get(user_id)


def _default_execute_dynamic(
    runtime: Any,
    principal: Principal,
    body: Any,
    version: str,
    *,
    investigation_id: str | None = None,
    return_graph_state: bool = False,
) -> Any:
    from app.clinical.runtime_api import execute_dynamic

    return execute_dynamic(
        runtime,
        principal,
        body,
        version,
        investigation_id=investigation_id,
        return_graph_state=return_graph_state,
    )


def _default_persist_dynamic(runtime: Any, principal: Principal, state: Any, request_id: str) -> str:
    from app.clinical.runtime_api import persist_dynamic

    return persist_dynamic(runtime, principal, state, request_id)


class ClinicalTemporalActivities:
    """Build the activity function registered by the Temporal Worker.

    Dependencies are injectable so the contract can be tested without a Temporal server, a
    PostgreSQL connection, or a paid model call.  Production uses the same runtime factory as the
    HTTP and lease-based worker paths.
    """

    def __init__(
        self,
        *,
        runtime_factory: RuntimeFactory | None = None,
        principal_resolver: PrincipalResolver | None = None,
        execute_dynamic: ExecuteDynamic | None = None,
        persist_dynamic: PersistDynamic | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory or _default_runtime_factory
        self._principal_resolver = principal_resolver or _default_principal_resolver
        self._execute_dynamic = execute_dynamic or _default_execute_dynamic
        self._persist_dynamic = persist_dynamic or _default_persist_dynamic

    @staticmethod
    def _approval_id(repository: Any, principal: Principal, investigation_id: str) -> str | None:
        """Find only the approval identifier needed by the workflow metadata contract."""

        list_approvals = getattr(repository, "list_approvals", None)
        if not callable(list_approvals):
            return None
        approvals = list_approvals(principal)
        current = next(
            (item for item in approvals if item.investigation_id == investigation_id),
            None,
        )
        return current.approval_id if current is not None else None

    def _existing_result(
        self,
        payload: TemporalWorkflowInput,
        principal: Principal,
        repository: Any,
        existing: Any,
    ) -> dict[str, Any]:
        """Convert an already-persisted investigation into an idempotent activity result.

        Temporal may retry an activity after PostgreSQL committed but before the first attempt
        returned.  Reusing the persisted record prevents a second LLM call and a duplicate UUID
        insert, while keeping evidence in PostgreSQL rather than Temporal history.
        """

        graph = getattr(existing, "graph_state", None)
        state = graph.to_legacy_state() if isinstance(graph, InvestigationGraphState) else getattr(existing, "state", None)
        if state is None or state.question != payload.question:
            raise TemporalActivityError("existing investigation does not match the workflow question")
        route = dict(getattr(state, "audit_metadata", {}).get("runtime_route", {}))
        if isinstance(graph, InvestigationGraphState):
            if graph.trial_id != payload.trial_id or graph.published_batch_id != payload.published_batch_id:
                raise TemporalActivityError("existing investigation does not match the workflow scope")
        elif route and (
            route.get("trial_id") != payload.trial_id
            or route.get("published_batch_id") != payload.published_batch_id
        ):
            raise TemporalActivityError("existing investigation does not match the workflow scope")
        raw_status = getattr(state, "status", "unknown")
        status = getattr(raw_status, "value", str(raw_status))
        return TemporalActivityResult(
            workflow_id=payload.investigation_id,
            investigation_id=str(getattr(existing, "investigation_id", payload.investigation_id)),
            status=status,
            publication_status=str(getattr(existing, "publication_status", "draft")),
            approval_id=self._approval_id(repository, principal, payload.investigation_id),
        ).model_dump(mode="json")

    def _execute_sync(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = TemporalWorkflowInput.model_validate(raw_payload)
        except ValidationError as exc:
            # The workflow retry policy treats TemporalActivityError as non-retryable.  A
            # malformed boundary payload cannot be repaired by executing the same activity again.
            raise TemporalActivityError(f"Temporal activity payload is invalid: {exc}") from exc
        principal = self._principal_resolver(payload.requested_by)
        if principal is None:
            raise TemporalActivityError("Temporal activity requester is not recognized")

        # Import here to keep the optional Temporal worker boundary from changing normal API
        # imports when temporalio is not installed.
        from app.clinical.runtime_api import DynamicInvestigationBody

        runtime = self._runtime_factory()
        repository = getattr(runtime, "enterprise_repository", None)
        get_investigation = getattr(repository, "get_investigation", None)
        if callable(get_investigation):
            try:
                existing = get_investigation(payload.investigation_id, principal)
            except KeyError:
                existing = None
            if existing is not None:
                return self._existing_result(payload, principal, repository, existing)

        body = DynamicInvestigationBody(
            question=payload.question,
            trial_id=payload.trial_id,
            provider=payload.provider,
            model=payload.model,
            published_batch_id=payload.published_batch_id,
            available_domains=list(payload.available_domains) or None,
        )
        try:
            state = self._execute_dynamic(
                runtime,
                principal,
                body,
                "v17",
                investigation_id=payload.investigation_id,
                return_graph_state=True,
            )
        except TypeError as exc:
            # Older injected executors (and V20 fixtures) do not know the additive keyword.  Do
            # not let compatibility shims hide a real execution TypeError.
            if "return_graph_state" not in str(exc):
                raise
            state = self._execute_dynamic(
                runtime,
                principal,
                body,
                "v17",
                investigation_id=payload.investigation_id,
            )

        # The runtime receives the workflow id so Graph events/checkpoints and repository records
        # share one durable correlation id.  Keep this assignment as a defensive fallback for
        # injected legacy executors that still allocate their own id.
        if isinstance(state, InvestigationGraphState):
            state.legacy.investigation_id = payload.investigation_id
            legacy_state = state.legacy
        else:
            state.investigation_id = payload.investigation_id
            legacy_state = state
        legacy_state.audit_metadata = {
            **getattr(legacy_state, "audit_metadata", {}),
            "temporal_workflow_id": payload.investigation_id,
            "temporal_activity": "execute_clinical_investigation_activity",
        }
        investigation_id = self._persist_dynamic(runtime, principal, state, payload.investigation_id)
        status = getattr(legacy_state.status, "value", str(legacy_state.status))
        publication_status = "pending_approval" if status == "pending_approval" else "draft"

        approval_id: str | None = None
        list_approvals = getattr(repository, "list_approvals", None)
        if status == "pending_approval" and callable(list_approvals):
            approval_id = self._approval_id(repository, principal, investigation_id)

        return TemporalActivityResult(
            workflow_id=payload.investigation_id,
            investigation_id=investigation_id,
            status=status,
            publication_status=publication_status,
            approval_id=approval_id,
        ).model_dump(mode="json")

    @_activity_definition
    async def execute_clinical_investigation_activity(
        self,
        raw_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the synchronous governed runtime in an activity thread."""

        return await asyncio.to_thread(self._execute_sync, raw_payload)


def build_temporal_activities(
    *,
    runtime_factory: RuntimeFactory | None = None,
    principal_resolver: PrincipalResolver | None = None,
) -> list[Callable[..., Any]]:
    """Return stable callable objects for ``temporalio.worker.Worker`` registration."""

    activities = ClinicalTemporalActivities(
        runtime_factory=runtime_factory,
        principal_resolver=principal_resolver,
    )
    return [activities.execute_clinical_investigation_activity]

