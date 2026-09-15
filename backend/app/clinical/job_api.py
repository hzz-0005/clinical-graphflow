from __future__ import annotations

from typing import Callable
from time import perf_counter
import os

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException
from pydantic import BaseModel

from app.clinical.api import ClinicalInvestigationRequest, execute_clinical_investigation
from app.clinical.jobs import ClinicalInvestigationJob
from app.clinical.operations import OPERATIONS
from app.enterprise.api import principal_from_header
from app.enterprise.auth import AuthorizationError
from app.enterprise.models import Role


class JobVersionBody(BaseModel):
    version: int


def create_clinical_job_router(get_runtime: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v5/clinical/jobs")
    inline_jobs = os.getenv("CLINICAL_INLINE_JOBS", "true").lower() in {"1", "true", "yes"}

    def visible(job, actor) -> bool:
        return actor.role is Role.ADMIN or job.owner_user_id == actor.user_id

    def run(job_id: str, actor, request_id: str) -> None:
        repository = get_runtime().clinical_jobs
        started = perf_counter()
        try:
            queued = repository.get(job_id)
            if queued.status != "queued":
                return
            running = repository.transition(job_id, queued.version, "running")
            body = ClinicalInvestigationRequest.model_validate(running.request)
            item = execute_clinical_investigation(get_runtime(), actor, body, request_id)
            current = repository.get(job_id)
            if current.cancel_requested:
                repository.transition(job_id, current.version, "cancelled")
                OPERATIONS.record_event("clinical_job", "cancelled", (perf_counter() - started) * 1000)
            else:
                repository.transition(job_id, current.version, "succeeded", investigation_id=item.investigation_id)
                OPERATIONS.record_event("clinical_job", "succeeded", (perf_counter() - started) * 1000)
        except Exception as exc:
            try:
                current = repository.get(job_id)
                if current.status == "running":
                    repository.transition(job_id, current.version, "failed", error_code=type(exc).__name__)
                    OPERATIONS.record_event("clinical_job", "failed", (perf_counter() - started) * 1000)
            except Exception:
                pass

    @router.post("", status_code=202)
    def create_job(body: ClinicalInvestigationRequest, background_tasks: BackgroundTasks, x_insightflow_user: str | None = Header(default=None), x_request_id: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        if actor.role is Role.VIEWER:
            raise HTTPException(status_code=403, detail={"code": "permission_denied"})
        item = get_runtime().clinical_jobs.create(ClinicalInvestigationJob(owner_user_id=actor.user_id, request=body.model_dump(mode="json")))
        if inline_jobs:
            background_tasks.add_task(run, item.job_id, actor, x_request_id or item.job_id)
        return item.model_dump(mode="json")

    @router.get("/{job_id}")
    def get_job(job_id: str, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        try:
            item = get_runtime().clinical_jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        if not visible(item, actor):
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        return item.model_dump(mode="json")

    @router.post("/{job_id}/cancel")
    def cancel(job_id: str, body: JobVersionBody, x_insightflow_user: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        try:
            item = get_runtime().clinical_jobs.get(job_id)
            if not visible(item, actor):
                raise KeyError(job_id)
            return get_runtime().clinical_jobs.cancel(job_id, body.version).model_dump(mode="json")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "version_conflict", "message": str(exc)}) from exc

    @router.post("/{job_id}/resume", status_code=202)
    def resume(job_id: str, body: JobVersionBody, background_tasks: BackgroundTasks, x_insightflow_user: str | None = Header(default=None), x_request_id: str | None = Header(default=None)):
        actor = principal_from_header(x_insightflow_user)
        try:
            item = get_runtime().clinical_jobs.get(job_id)
            if not visible(item, actor):
                raise KeyError(job_id)
            resumed = get_runtime().clinical_jobs.resume(job_id, body.version)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"code": "invalid_job_transition", "message": str(exc)}) from exc
        if inline_jobs:
            background_tasks.add_task(run, job_id, actor, x_request_id or job_id)
        return resumed.model_dump(mode="json")

    return router

