from __future__ import annotations

import os
from typing import Callable, Literal
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from app.clinical.jobs import ClinicalInvestigationJob
from app.clinical.registry import ClinicalToolRegistry
from app.clinical.tools import ClinicalDataScope
from app.clinical.runtime_factory import (
    _scope_catalog,
    configured_runtime_version,
    execute_clinical_investigation,
    persisted_runtime_version,
    validate_v16_fallback,
)
from app.clinical.v17_contracts import InvestigationGraphState
from app.enterprise.api import principal_from_header
from app.enterprise.models import ApprovalRequest, DataScope, Role
from app.enterprise.repository import IdempotencyConflict, VersionConflict
from app.settings import get_settings

class DynamicInvestigationBody(BaseModel):
    model_config=ConfigDict(extra="forbid")
    question:str=Field(min_length=5,max_length=1000); trial_id:str=Field(min_length=1,max_length=80)
    provider:Literal["fake","openai","anthropic","deepseek","glm","kimi","custom"]="fake"
    model:str|None=Field(default=None,max_length=200); published_batch_id:str|None=None
    available_domains:list[str]|None=None

def resolve_published_domains(runtime, principal, scope, body:DynamicInvestigationBody)->set[str]:
    """Resolve the analysis domain capability for one investigation.

    Precedence (narrow only, never widen): an explicit ``available_domains`` request must be a
    subset of the capability that is actually governed, which is the selected published batch
    contract when a batch is bound, otherwise the runtime's default published capability. A caller
    can therefore focus an investigation but can never grant itself a domain that was not published.
    """

    configured_domains=getattr(runtime,"published_domains",None)
    if configured_domains is not None:
        capability=set(configured_domains)
    elif hasattr(runtime.clinical_trial_catalog,"available_domains"):
        capability=set(runtime.clinical_trial_catalog.available_domains(body.trial_id))
    else:
        capability={"DM","ADSL","ADEFF"}
    if body.published_batch_id:
        batch=runtime.quarantine_repository.get_batch(body.published_batch_id,principal.user_id,scope.global_access)
        if batch.status!="published": raise ValueError("batch_not_published")
        contract=runtime.quarantine_repository.contract_for(body.published_batch_id,principal.user_id,scope.global_access)
        capability={x["target_domain"] for x in contract["files"] if x["target_domain"]!="custom_candidate"}
    if not body.available_domains: return capability
    requested={item.strip().upper() for item in body.available_domains if item.strip()}
    registry=getattr(runtime,"domain_registry",None)
    if registry is not None:
        unknown=requested-set(registry.names)
        if unknown: raise ValueError(f"unknown clinical data domain requested: {','.join(sorted(unknown))}")
    if not requested.issubset(capability):
        raise ValueError("requested clinical data domains are not published for this trial: "+",".join(sorted(requested-capability)))
    return requested

def execute_dynamic(
    runtime,
    principal,
    body: DynamicInvestigationBody,
    runtime_version: str | None = None,
    *,
    investigation_id: str | None = None,
    return_graph_state: bool = False,
):
    scope=DataScope.from_principal(principal)
    if not scope.allows_clinical(body.trial_id): raise KeyError("resource_not_found")
    domains=resolve_published_domains(runtime,principal,scope,body)
    clinical_scope=ClinicalDataScope(trial_ids=frozenset({body.trial_id}),regions=frozenset(scope.regions),site_ids=frozenset(scope.site_ids),published_batch_id=body.published_batch_id,published_domains=frozenset(domains))
    version = configured_runtime_version(runtime_version)
    # Fake runs never need Settings (and therefore never require an API key or database URL in
    # unit tests).  External providers resolve the same deployment settings for V16 and V17.
    settings = None if body.provider == "fake" else get_settings()
    initial_graph_state = None
    if version == "v17" and investigation_id:
        repository = getattr(runtime, "enterprise_repository", None)
        get_graph = getattr(repository, "get_graph_investigation", None)
        if callable(get_graph):
            try:
                initial_graph_state = get_graph(investigation_id, principal)
            except KeyError:
                # The first attempt legitimately has no durable row yet.  Existing rows are
                # loaded through the canonical graph adapter and therefore carry CAS version.
                initial_graph_state = None
    state = execute_clinical_investigation(
        runtime=runtime,
        question=body.question,
        trial_id=body.trial_id,
        provider=body.provider,
        model=body.model,
        published_batch_id=body.published_batch_id,
        domains=domains,
        clinical_scope=clinical_scope,
        runtime_version=version,
        settings=settings,
        investigation_id=investigation_id,
        initial_graph_state=initial_graph_state,
        return_graph_state=return_graph_state,
    )
    if version == "v16":
        # Keep the V8 response metadata exactly as before while the feature flag is off.
        state.audit_metadata.update({"runtime_version":"8","catalog_version":"V15","provider":state.provider,"model":state.model,"published_batch_id":body.published_batch_id,"available_domains":sorted(domains)})
    return state

def persist_dynamic(runtime,principal,state,request_id):
    repo=getattr(runtime,"enterprise_repository",None)
    if repo is None:return state.investigation_id
    graph_state = state if isinstance(state, InvestigationGraphState) else None
    legacy_state = graph_state.legacy if graph_state is not None else state
    scope = DataScope.from_principal(principal)
    publication_status = "pending_approval" if legacy_state.status.value == "pending_approval" else "draft"
    approval = ApprovalRequest(investigation_id=legacy_state.investigation_id,requested_by=principal.user_id,reason="dynamic_clinical_review") if legacy_state.status.value == "pending_approval" else None
    if graph_state is not None:
        save_graph = getattr(repo, "save_graph_investigation", None)
        save_graph_approval = getattr(repo, "save_graph_investigation_and_approval", None)
        if approval is not None and callable(save_graph_approval):
            item, _ = save_graph_approval(
                graph_state,
                principal,
                scope,
                publication_status,
                request_id,
                approval,
                expected_version=graph_state.enterprise_version,
            )
            return item.investigation_id
        if callable(save_graph):
            item = save_graph(
                graph_state,
                principal,
                scope,
                publication_status,
                request_id,
                expected_version=graph_state.enterprise_version,
            )
            return item.investigation_id
        # Compatibility-only path for injected repositories from V16/V20 tests.
        state = legacy_state
    atomic = getattr(repo, "save_investigation_and_approval", None)
    if approval is not None and callable(atomic):
        item, _ = atomic(state, principal, scope, publication_status, request_id, approval)
    else:
        item = repo.save_investigation(state, principal, scope, publication_status, request_id)
        if approval is not None:
            repo.create_approval(approval, principal, request_id)
    return item.investigation_id


def create_dynamic_runtime_router(get_runtime:Callable)->APIRouter:
    router=APIRouter(prefix="/api/v8/clinical"); inline=os.getenv("CLINICAL_INLINE_JOBS","true").lower() in {"1","true","yes"}
    @router.get("/trials")
    def list_trials(x_insightflow_user:str|None=Header(default=None)):
        principal=principal_from_header(x_insightflow_user)
        scope=DataScope.from_principal(principal)
        items=get_runtime().clinical_trial_catalog.list_trials()
        visible=[]
        for item in items:
            payload=item.model_dump(mode="json") if hasattr(item,"model_dump") else dict(item)
            if scope.allows_clinical(payload["trial_id"]):visible.append(payload)
        return visible
    @router.get("/catalog")
    def catalog(
        trial_id: str | None = None,
        published_batch_id: str | None = None,
        x_insightflow_user: str | None = Header(default=None),
    ):
        """Return metadata discovered from published rows and allowlisted marts.

        This endpoint never returns payload values.  When a trial is supplied,
        the result is narrowed to the caller's governed trial capability.
        """

        principal=principal_from_header(x_insightflow_user)
        runtime=get_runtime()
        provider=getattr(runtime,"clinical_data_catalog",None)
        if provider is None:
            raise HTTPException(status_code=503,detail={"code":"catalog_unavailable"})
        if published_batch_id and not trial_id:
            raise HTTPException(status_code=422,detail={"code":"trial_id_required_for_batch_catalog"})
        domains: set[str] | None = None
        if trial_id:
            scope=DataScope.from_principal(principal)
            if not scope.allows_clinical(trial_id):
                raise HTTPException(status_code=404,detail={"code":"resource_not_found"})
            body=DynamicInvestigationBody(question="查看已发布数据目录",trial_id=trial_id,published_batch_id=published_batch_id)
            try:
                domains=resolve_published_domains(runtime,principal,scope,body)
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=422,detail={"code":"catalog_scope_invalid","message":str(exc)}) from exc
        try:
            snapshot=provider.snapshot(trial_id=trial_id,published_batch_id=published_batch_id)
        except Exception as exc:
            raise HTTPException(status_code=503,detail={"code":"catalog_probe_failed","message":str(exc)}) from exc
        if domains is not None:
            snapshot=_scope_catalog(snapshot,domains)
        return snapshot.model_dump(mode="json")
    @router.post("/investigations")
    def investigate(body:DynamicInvestigationBody,x_insightflow_user:str|None=Header(default=None),x_request_id:str|None=Header(default=None)):
        principal=principal_from_header(x_insightflow_user)
        if principal.role is Role.VIEWER:raise HTTPException(403,detail={"code":"permission_denied"})
        try:
            state=execute_dynamic(get_runtime(),principal,body,return_graph_state=True)
            persist_dynamic(get_runtime(),principal,state,x_request_id or state.investigation_id)
            response_state = state.to_legacy_state() if isinstance(state, InvestigationGraphState) else state
            return response_state.model_dump(mode="json")
        except KeyError as exc:raise HTTPException(404,detail={"code":"resource_not_found"}) from exc
        except VersionConflict as exc:raise HTTPException(409,detail={"code":"version_conflict","message":str(exc)}) from exc
        except IdempotencyConflict as exc:raise HTTPException(409,detail={"code":"idempotency_conflict","message":str(exc)}) from exc
        except ValueError as exc:raise HTTPException(422,detail={"code":"dynamic_investigation_invalid","message":str(exc)}) from exc
    def run_job(job_id,principal):
        repo=get_runtime().clinical_jobs
        try:
            queued=repo.get(job_id)
            if queued.status!="queued":return
            running=repo.transition(job_id,queued.version,"running")
            request=dict(running.request);requested_version=request.pop("runtime_version",None)
            state=execute_dynamic(get_runtime(),principal,DynamicInvestigationBody.model_validate(request),requested_version,investigation_id=job_id if configured_runtime_version(requested_version)=="v17" else None,return_graph_state=True);investigation_id=persist_dynamic(get_runtime(),principal,state,job_id)
            current=repo.get(job_id);cancelled=current.cancel_requested;repo.transition(job_id,current.version,"cancelled" if cancelled else "succeeded",investigation_id=None if cancelled else investigation_id)
        except Exception as exc:
            current=repo.get(job_id)
            if current.status=="running":repo.transition(job_id,current.version,"failed",error_code=type(exc).__name__)
    @router.post("/jobs",status_code=202)
    def create_job(body:DynamicInvestigationBody,tasks:BackgroundTasks,x_insightflow_user:str|None=Header(default=None)):
        principal=principal_from_header(x_insightflow_user)
        if principal.role is Role.VIEWER:raise HTTPException(403,detail={"code":"permission_denied"})
        if not DataScope.from_principal(principal).allows_clinical(body.trial_id):raise HTTPException(404,detail={"code":"resource_not_found"})
        configured_version = configured_runtime_version()
        if configured_version == "v16":
            try:
                validate_v16_fallback()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail={"code": "dynamic_investigation_invalid", "message": str(exc)}) from exc
        item=get_runtime().clinical_jobs.create(ClinicalInvestigationJob(owner_user_id=principal.user_id,request={**body.model_dump(mode="json"),"runtime_version":persisted_runtime_version(configured_version)}))
        if inline:tasks.add_task(run_job,item.job_id,principal)
        return item.model_dump(mode="json")
    @router.get("/jobs/{job_id}")
    def get_job(job_id:str,x_insightflow_user:str|None=Header(default=None)):
        principal=principal_from_header(x_insightflow_user)
        try:item=get_runtime().clinical_jobs.get(job_id)
        except KeyError as exc:raise HTTPException(404,detail={"code":"resource_not_found"}) from exc
        if principal.role is not Role.ADMIN and item.owner_user_id!=principal.user_id:raise HTTPException(404,detail={"code":"resource_not_found"})
        return item.model_dump(mode="json")
    return router


def create_catalog_router(get_runtime: Callable) -> APIRouter:
    """Expose the V15 metadata contract without changing V8 investigation routes."""

    router=APIRouter(prefix="/api/v15/clinical")

    @router.get("/catalog")
    def catalog(
        trial_id: str | None = None,
        published_batch_id: str | None = None,
        x_insightflow_user: str | None = Header(default=None),
    ):
        principal=principal_from_header(x_insightflow_user)
        runtime=get_runtime()
        provider=getattr(runtime,"clinical_data_catalog",None)
        if provider is None:
            raise HTTPException(status_code=503,detail={"code":"catalog_unavailable"})
        if published_batch_id and not trial_id:
            raise HTTPException(status_code=422,detail={"code":"trial_id_required_for_batch_catalog"})
        domains: set[str] | None = None
        if trial_id:
            scope=DataScope.from_principal(principal)
            if not scope.allows_clinical(trial_id):
                raise HTTPException(status_code=404,detail={"code":"resource_not_found"})
            body=DynamicInvestigationBody(question="查看已发布数据目录",trial_id=trial_id,published_batch_id=published_batch_id)
            try:
                domains=resolve_published_domains(runtime,principal,scope,body)
            except (KeyError, ValueError) as exc:
                raise HTTPException(status_code=422,detail={"code":"catalog_scope_invalid","message":str(exc)}) from exc
        try:
            snapshot=provider.snapshot(trial_id=trial_id,published_batch_id=published_batch_id)
        except Exception as exc:
            raise HTTPException(status_code=503,detail={"code":"catalog_probe_failed","message":str(exc)}) from exc
        if domains is not None:
            snapshot=_scope_catalog(snapshot,domains)
        return snapshot.model_dump(mode="json")

    return router

