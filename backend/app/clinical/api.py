from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.clinical.tools import ClinicalDataScope
from app.clinical.llm import build_clinical_llm_client
from app.clinical.llm_investigator import LLMClinicalInvestigator
from app.settings import get_settings
from app.clinical.operations import OPERATIONS
from app.agent.models import InvestigationStatus

from app.enterprise.api import principal_from_header
from app.enterprise.auth import AuthorizationError, AuthorizationPolicy
from app.enterprise.models import ApprovalRequest, Capability, DataScope, Role
from app.enterprise.policy import PublicationPolicy
from app.enterprise.repository import IdempotencyConflict, VersionConflict


class ClinicalInvestigationRequest(BaseModel):
    trial_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    question: str = Field(min_length=5, max_length=500)
    subgroup: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")
    batch_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


class LLMClinicalInvestigationRequest(ClinicalInvestigationRequest):
    provider: str = Field(default="fake", pattern=r"^[a-z][a-z0-9_-]{1,31}$")
    model: str | None = Field(default=None, max_length=100)


def _request_id(value: str | None) -> str:
    return value or str(uuid4())


def _clinical_scope(actor, trial_id: str | None = None, published_batch_id: str | None = None) -> ClinicalDataScope:
    trial_ids = frozenset(actor.allowed_trial_ids)
    if actor.role is Role.ADMIN and trial_id:
        trial_ids = frozenset({trial_id})
    return ClinicalDataScope(
        trial_ids=trial_ids,
        regions=frozenset(actor.allowed_regions),
        site_ids=frozenset(actor.allowed_site_ids),
        published_batch_id=published_batch_id,
    )


def _resolve_published_batch(runtime, actor, trial_id: str, batch_id: str | None):
    if batch_id is None:
        return None
    coordinator = getattr(runtime, "cdisc_imports", None)
    if coordinator is None:
        raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
    scope = DataScope.from_principal(actor)
    item = next((candidate for candidate in coordinator.list_batches() if candidate.batch_id == batch_id), None)
    if item is None or item.status != "published" or trial_id not in item.trial_ids or not scope.allows_clinical(trial_id):
        raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
    return item


def _data_version_snapshot(batch):
    if batch is None:
        return None
    return {
        "batch_id": batch.batch_id,
        "content_hash": batch.content_hash,
        "published_at": batch.published_at.isoformat().replace("+00:00", "Z") if batch.published_at else None,
        "record_count": batch.record_count,
        "trial_ids": list(batch.trial_ids),
    }


def _suppress_row(row: dict[str, Any]) -> dict[str, Any]:
    output = dict(row)
    size = output.get("sample_size", output.get("participant_count"))
    if size is not None and int(size) < 10:
        keep = {"trial_id", "site_id", "region", "arm", "sample_size", "participant_count"}
        for key in output:
            if key not in keep:
                output[key] = None
        output["suppressed"] = True
    return output


def _serialize(item, actor) -> dict[str, Any]:
    payload = item.model_dump(mode="json")
    for evidence in payload["state"]["evidence"]:
        if actor.role is Role.VIEWER:
            evidence["sql"] = ""
        evidence["rows"] = [_suppress_row(row) for row in evidence.get("rows", [])]
    payload["research_use_only"] = "Research analysis only（仅供研究分析）"
    return payload


def execute_clinical_investigation(runtime, actor, body: ClinicalInvestigationRequest, request_id: str):
    """Shared governed execution path for synchronous requests and asynchronous jobs."""
    auth = AuthorizationPolicy()
    publication = PublicationPolicy()
    auth.require(actor, Capability.INVESTIGATION_CREATE)
    scope = DataScope.from_principal(actor)
    if not scope.allows_clinical(body.trial_id, body.subgroup):
        raise KeyError("resource_not_found")
    batch = _resolve_published_batch(runtime, actor, body.trial_id, body.batch_id)
    clinical_scope = _clinical_scope(actor, body.trial_id, batch.batch_id if batch else None)
    state = runtime.clinical_investigator.investigate(body.question, clinical_scope, body.subgroup)
    state.audit_metadata = {
        "trial_id": body.trial_id,
        "population": "intention_to_treat",
        "subgroup": body.subgroup,
        "batch_id": batch.batch_id if batch else None,
        "data_version_status": batch.status if batch else "governed_default",
        "data_version_snapshot": _data_version_snapshot(batch),
        "guardrail_flags": list(getattr(state.verification, "flags", [])) if state.verification else [],
    }
    status = publication.decide_clinical().value if state.status is InvestigationStatus.PENDING_APPROVAL else "draft"
    repo = runtime.enterprise_repository
    approval = ApprovalRequest(investigation_id=state.investigation_id, requested_by=actor.user_id, reason="mandatory_clinical_review") if state.status is InvestigationStatus.PENDING_APPROVAL else None
    atomic = getattr(repo, "save_investigation_and_approval", None)
    if approval is not None and callable(atomic):
        item, _ = atomic(state, actor, scope, status, request_id, approval)
    else:
        item = repo.save_investigation(state, actor, scope, status, request_id)
        if approval is not None:
            repo.create_approval(approval, actor, request_id)
    return item


def create_clinical_router(get_runtime: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v4/clinical")
    auth = AuthorizationPolicy()
    publication = PublicationPolicy()

    @router.get("/metrics")
    def metrics(x_insightflow_user: str | None = Header(default=None)):
        principal_from_header(x_insightflow_user)
        runtime = get_runtime()
        return [runtime.clinical_metrics.get(name).model_dump(mode="json") for name in sorted(runtime.clinical_metrics.names)]

    @router.get("/operations")
    def operations(x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        try:
            auth.require(actor, Capability.AUDIT_READ)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc
        return OPERATIONS.snapshot()

    @router.get("/audit-events")
    def audit_events(x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        try:
            auth.require(actor, Capability.AUDIT_READ)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc
        source = get_runtime().enterprise_repository.audit_events
        events = source() if callable(source) else source
        return [event.model_dump(mode="json") for event in events]

    @router.get("/trials/{trial_id}")
    def trial(trial_id: str, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        data_scope = DataScope.from_principal(actor)
        if not data_scope.allows_clinical(trial_id):
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        try:
            result = get_runtime().clinical_tools_factory(_clinical_scope(actor, trial_id)).inspect_trial(trial_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        return {**result.model_dump(mode="json"), "research_use_only": "Research analysis only（仅供研究分析）"}

    @router.post("/investigations")
    def create_investigation(
        body: ClinicalInvestigationRequest,
        x_insightflow_user: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ):
        try:
            actor = principal_from_header(x_insightflow_user)
            item = execute_clinical_investigation(get_runtime(), actor, body, _request_id(x_request_id))
        except (AuthorizationError,) as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": str(exc)}) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict", "message": str(exc)}) from exc
        return _serialize(item, actor)

    @router.get("/investigations")
    def list_investigations(x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        return [_serialize(item, actor) for item in get_runtime().enterprise_repository.list_investigations(actor) if item.state.domain == "clinical_trial"]

    @router.post("/investigations/llm")
    def create_llm_investigation(
        body: LLMClinicalInvestigationRequest,
        x_insightflow_user: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ):
        actor = principal_from_header(x_insightflow_user)
        try:
            auth.require(actor, Capability.INVESTIGATION_CREATE)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc
        scope = DataScope.from_principal(actor)
        if not scope.allows_clinical(body.trial_id, body.subgroup):
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        runtime = get_runtime()
        batch = _resolve_published_batch(runtime, actor, body.trial_id, body.batch_id)
        try:
            client = build_clinical_llm_client(get_settings(), body.provider, body.model)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail={"code": "llm_not_configured", "message": str(exc)}) from exc
        clinical_scope = _clinical_scope(actor, body.trial_id, batch.batch_id if batch else None)
        state = LLMClinicalInvestigator(runtime.clinical_investigator, client).investigate(body.question, clinical_scope, body.subgroup)
        OPERATIONS.record(body.provider, state.status.value, state.llm_usage.input_tokens, state.llm_usage.output_tokens)
        state.audit_metadata.update({"trial_id": body.trial_id, "population": "intention_to_treat", "subgroup": body.subgroup, "batch_id": batch.batch_id if batch else None, "data_version_status": batch.status if batch else "governed_default", "data_version_snapshot": _data_version_snapshot(batch)})
        repo = runtime.enterprise_repository
        publication_status = publication.decide_clinical().value if state.status is InvestigationStatus.PENDING_APPROVAL else "draft"
        request_id = _request_id(x_request_id)
        approval = ApprovalRequest(investigation_id=state.investigation_id, requested_by=actor.user_id, reason="mandatory_clinical_review") if state.status.value == "pending_approval" else None
        atomic = getattr(repo, "save_investigation_and_approval", None)
        if approval is not None and callable(atomic):
            item, _ = atomic(state, actor, scope, publication_status, request_id, approval)
        else:
            item = repo.save_investigation(state, actor, scope, publication_status, request_id)
            if approval is not None:
                repo.create_approval(approval, actor, request_id)
        return _serialize(item, actor)

    @router.get("/investigations/{investigation_id}")
    def get_investigation(
        investigation_id: str,
        x_insightflow_user: str | None = Header(default=None),
    ):
        actor = principal_from_header(x_insightflow_user)
        try:
            auth.require(actor, Capability.INVESTIGATION_READ)
            item = get_runtime().enterprise_repository.get_investigation(investigation_id, actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        if item.state.domain != "clinical_trial":
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        return _serialize(item, actor)

    @router.post("/investigations/{investigation_id}/submit")
    def submit(
        investigation_id: str,
        x_insightflow_user: str | None = Header(default=None),
        x_request_id: str | None = Header(default=None),
    ):
        actor = principal_from_header(x_insightflow_user)
        try:
            auth.require(actor, Capability.INVESTIGATION_CREATE)
            item = get_runtime().enterprise_repository.get_investigation(investigation_id, actor)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"}) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        repo = get_runtime().enterprise_repository
        existing = [approval for approval in repo.list_approvals(actor) if approval.investigation_id == investigation_id]
        approval = existing[0] if existing else repo.create_approval(
            ApprovalRequest(
                investigation_id=item.investigation_id,
                requested_by=actor.user_id,
                reason="mandatory_clinical_review",
            ), actor, _request_id(x_request_id)
        )
        return approval.model_dump(mode="json")

    return router

