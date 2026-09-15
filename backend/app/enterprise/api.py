from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter,Header,HTTPException
from pydantic import BaseModel,Field

from app.enterprise.auth import AuthorizationError,AuthorizationPolicy
from app.enterprise.models import Alert,ApprovalRequest,Capability,DataScope,Principal,Role
from app.enterprise.policy import PublicationPolicy,PublicationStatus
from app.enterprise.approval import ApprovalConflict,ApprovalMachine
from app.enterprise.scheduler import ScheduleService,ScheduleValidationError


DEV_USERS={
 "admin":Principal(user_id="admin",email="admin@cloudflow.test",role=Role.ADMIN),
 "analyst-jp":Principal(user_id="analyst-jp",email="analyst-jp@cloudflow.test",role=Role.ANALYST,allowed_countries=("Japan",),allowed_account_manager_ids=("manager_17",)),
 "viewer-jp":Principal(user_id="viewer-jp",email="viewer-jp@cloudflow.test",role=Role.VIEWER,allowed_countries=("Japan",),allowed_account_manager_ids=("manager_17",)),
 "analyst-de":Principal(user_id="analyst-de",email="analyst-de@cloudflow.test",role=Role.ANALYST,allowed_countries=("Germany",),allowed_account_manager_ids=("manager_22",)),
 "clinical-analyst-asia":Principal(user_id="clinical-analyst-asia",email="clinical-analyst-asia@insightflow.test",role=Role.ANALYST,allowed_trial_ids=("TRIAL-CF-101",),allowed_regions=("Asia",),allowed_site_ids=("SITE-17","SITE-03")),
 "clinical-viewer-asia":Principal(user_id="clinical-viewer-asia",email="clinical-viewer-asia@insightflow.test",role=Role.VIEWER,allowed_trial_ids=("TRIAL-CF-101",),allowed_regions=("Asia",),allowed_site_ids=("SITE-17","SITE-03")),
 "clinical-analyst-europe":Principal(user_id="clinical-analyst-europe",email="clinical-analyst-europe@insightflow.test",role=Role.ANALYST,allowed_trial_ids=("TRIAL-CF-202",),allowed_regions=("Europe",),allowed_site_ids=("SITE-05",)),
}


class V3InvestigationRequest(BaseModel):
    question:str=Field(min_length=5,max_length=500)
    causal:bool=False


class ApprovalDecisionBody(BaseModel):
    version:int=Field(ge=1)
    comment:str=Field(min_length=1,max_length=500)


class ScheduleBody(BaseModel):
    name:str=Field(min_length=2,max_length=100)
    question:str=Field(min_length=5,max_length=500)
    cron_expression:str
    timezone:str
    provider:str|None=None


def principal_from_header(x_insightflow_user:str|None=Header(default=None))->Principal:
    if not x_insightflow_user or x_insightflow_user not in DEV_USERS: raise HTTPException(status_code=401,detail={"code":"authentication_required"})
    return DEV_USERS[x_insightflow_user]


def _request_id(value:str|None)->str: return value or str(uuid4())


def _serialize(item,actor):
    payload=item.model_dump(mode="json")
    if actor.role is Role.VIEWER:
        for evidence in payload["state"]["evidence"]: evidence["sql"]=""
    return payload


def create_v3_router(get_runtime:Callable)->APIRouter:
    router=APIRouter(prefix="/api/v3"); auth=AuthorizationPolicy(); publication=PublicationPolicy(); machine=ApprovalMachine(); scheduler=ScheduleService()

    @router.get("/me")
    def me(x_insightflow_user:str|None=Header(default=None)):
        return principal_from_header(x_insightflow_user).model_dump(mode="json")

    @router.post("/investigations")
    def create_investigation(body:V3InvestigationRequest,x_insightflow_user:str|None=Header(default=None),x_request_id:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        try: auth.require(actor,Capability.INVESTIGATION_CREATE)
        except AuthorizationError as exc: raise HTTPException(status_code=403,detail={"code":"permission_denied"}) from exc
        runtime=get_runtime(); state=runtime.investigator.investigate(body.question)
        flags=[flag for evidence in state.evidence for flag in evidence.quality_flags]
        status=publication.decide(causal=body.causal,confidence=state.confidence,quality_flags=flags,scheduled=False)
        item=runtime.enterprise_repository.save_investigation(state,actor,DataScope.from_principal(actor),status.value,_request_id(x_request_id))
        if status is PublicationStatus.PENDING_APPROVAL:
            runtime.enterprise_repository.create_approval(ApprovalRequest(investigation_id=state.investigation_id,requested_by=actor.user_id,reason="causal_or_low_confidence"),actor,_request_id(x_request_id))
        return _serialize(item,actor)

    @router.get("/investigations")
    def list_investigations(x_insightflow_user:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user); auth.require(actor,Capability.INVESTIGATION_READ)
        return [_serialize(x,actor) for x in get_runtime().enterprise_repository.list_investigations(actor)]

    @router.get("/investigations/{investigation_id}")
    def get_investigation(investigation_id:str,x_insightflow_user:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user); auth.require(actor,Capability.INVESTIGATION_READ)
        try: item=get_runtime().enterprise_repository.get_investigation(investigation_id,actor)
        except KeyError as exc: raise HTTPException(status_code=404,detail={"code":"resource_not_found"}) from exc
        return _serialize(item,actor)

    @router.get("/approvals")
    def list_approvals(x_insightflow_user:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        return [x.model_dump(mode="json") for x in get_runtime().enterprise_repository.list_approvals(actor)]

    @router.post("/approvals/{approval_id}/approve")
    def approve(approval_id:str,body:ApprovalDecisionBody,x_insightflow_user:str|None=Header(default=None),x_request_id:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        try: auth.require(actor,Capability.APPROVAL_DECIDE)
        except AuthorizationError as exc: raise HTTPException(status_code=403,detail={"code":"permission_denied"}) from exc
        repo=get_runtime().enterprise_repository
        try: decided=machine.approve(repo.get_approval(approval_id),actor,body.version,body.comment)
        except KeyError as exc: raise HTTPException(status_code=404,detail={"code":"resource_not_found"}) from exc
        except ApprovalConflict as exc: raise HTTPException(status_code=409,detail={"code":"approval_conflict","message":str(exc)}) from exc
        repo.save_approval(decided,actor,_request_id(x_request_id)); repo.publish_investigation(decided.investigation_id)
        return decided.model_dump(mode="json")

    @router.get("/audit-events")
    def audit_events(x_insightflow_user:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        try: auth.require(actor,Capability.AUDIT_READ)
        except AuthorizationError as exc: raise HTTPException(status_code=403,detail={"code":"permission_denied"}) from exc
        events=get_runtime().enterprise_repository.audit_events
        if actor.role is not Role.ADMIN: events=[x for x in events if x.actor_user_id==actor.user_id]
        return [x.model_dump(mode="json") for x in events]

    @router.post("/schedules")
    def create_schedule(body:ScheduleBody,x_insightflow_user:str|None=Header(default=None),x_request_id:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        try: auth.require(actor,Capability.SCHEDULE_MANAGE)
        except AuthorizationError as exc: raise HTTPException(status_code=403,detail={"code":"permission_denied"}) from exc
        try:item=scheduler.create(owner=actor,name=body.name,question=body.question,cron_expression=body.cron_expression,timezone_name=body.timezone,provider=body.provider)
        except (ScheduleValidationError,PermissionError) as exc:raise HTTPException(status_code=422,detail={"code":"schedule_invalid","message":str(exc)}) from exc
        return get_runtime().enterprise_repository.save_schedule(item,actor,_request_id(x_request_id)).model_dump(mode="json")

    @router.get("/schedules")
    def list_schedules(x_insightflow_user:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        return [x.model_dump(mode="json") for x in get_runtime().enterprise_repository.list_schedules(actor)]

    @router.post("/schedules/{schedule_id}/run")
    def run_schedule(schedule_id:str,x_insightflow_user:str|None=Header(default=None),x_request_id:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        if actor.role is not Role.ADMIN:raise HTTPException(status_code=403,detail={"code":"permission_denied"})
        repo=get_runtime().enterprise_repository
        try:schedule=repo.get_schedule(schedule_id)
        except KeyError as exc:raise HTTPException(status_code=404,detail={"code":"resource_not_found"}) from exc
        owner=DEV_USERS[schedule.owner_user_id]; state=get_runtime().investigator.investigate(schedule.question)
        saved=repo.save_investigation(state,owner,schedule.scope,PublicationStatus.PENDING_APPROVAL.value,_request_id(x_request_id))
        repo.create_approval(ApprovalRequest(investigation_id=state.investigation_id,requested_by=owner.user_id,reason="scheduled_investigation"),owner,_request_id(x_request_id))
        alert=repo.save_alert(Alert(investigation_id=state.investigation_id,recipient_user_id=owner.user_id,title="Scheduled investigation completed（定时调查完成）",body=state.answer or "Investigation completed"))
        return {"investigation_id":saved.investigation_id,"alert_id":alert.alert_id,"status":"completed"}

    @router.get("/alerts")
    def list_alerts(x_insightflow_user:str|None=Header(default=None)):
        actor=principal_from_header(x_insightflow_user)
        return [x.model_dump(mode="json") for x in get_runtime().enterprise_repository.list_alerts(actor)]
    return router

