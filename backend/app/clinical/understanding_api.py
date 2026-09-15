from __future__ import annotations

from typing import Annotated, Callable

from fastapi import APIRouter, File, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, ValidationError

from app.clinical.profiling import DataProfiler
from app.clinical.tabular import TabularFile, TabularFileParser
from app.clinical.transformation import TransformationValidator
from app.clinical.understanding_agent import DataUnderstandingAgent, RulesMappingProvider
from app.clinical.understanding_models import MappingContract
from app.clinical.understanding_tools import DataUnderstandingTools
from app.enterprise.api import principal_from_header
from app.enterprise.models import Role

MAX_TOTAL_BYTES=25_000_000

class VersionBody(BaseModel): version:int=Field(ge=1)
class MappingBody(BaseModel):
    version:int=Field(ge=1)
    contract:MappingContract
class UnderstandBody(BaseModel):
    version:int=Field(ge=1)
    provider:str="rules"
class WithdrawBody(BaseModel):
    version:int=Field(ge=1)
    reason:str=Field(min_length=5,max_length=500)

def create_understanding_router(get_runtime:Callable)->APIRouter:
    router=APIRouter()
    def components():
        runtime=get_runtime(); tools=DataUnderstandingTools(runtime.quarantine_repository,runtime.domain_registry)
        return runtime,tools,DataUnderstandingAgent(tools,runtime.quarantine_repository,runtime.domain_registry)
    def actor(header): return principal_from_header(header)
    def serialize(runtime,batch,user):
        schema=DataUnderstandingTools(runtime.quarantine_repository,runtime.domain_registry).invoke("inspect_schema",{"batch_id":batch.batch_id},user)
        return {"batch":batch.model_dump(mode="json"),"schema":schema}

    @router.post("/api/v8/imports",status_code=status.HTTP_201_CREATED)
    async def upload(files:Annotated[list[UploadFile],File()],x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user)
        if principal.role is Role.VIEWER: raise HTTPException(403,detail={"code":"permission_denied"})
        if not 1<=len(files)<=20: raise HTTPException(422,detail={"code":"file_count_invalid"})
        payloads=[(f,await f.read()) for f in files]
        if sum(len(p) for _,p in payloads)>MAX_TOTAL_BYTES: raise HTTPException(413,detail={"code":"upload_too_large"})
        try: parsed=tuple(TabularFileParser(MAX_TOTAL_BYTES).parse(f.filename or "unnamed",f.content_type or "application/octet-stream",p) for f,p in payloads)
        except ValueError as exc: raise HTTPException(422,detail={"code":"parse_failed","message":str(exc)}) from exc
        return get_runtime().quarantine_repository.create_batch(principal.user_id,parsed).model_dump(mode="json")

    @router.get("/api/v8/imports/{batch_id}")
    def get_batch(batch_id:str,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user); runtime=get_runtime()
        try: batch=runtime.quarantine_repository.get_batch(batch_id,principal.user_id,principal.role is Role.ADMIN)
        except KeyError as exc: raise HTTPException(404,detail={"code":"resource_not_found"}) from exc
        return serialize(runtime,batch,principal.user_id if principal.role is not Role.ADMIN else batch.owner_user_id)

    @router.post("/api/v8/imports/{batch_id}/profile")
    def profile(batch_id:str,body:VersionBody,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user); runtime=get_runtime(); repo=runtime.quarantine_repository
        try:
            stored=repo.files_for(batch_id,principal.user_id); files=tuple(TabularFile(filename=f.filename,format=f.format,columns=f.columns,row_count=f.row_count,rows=repo.rows_for(batch_id,f.file_id,principal.user_id),content_hash=f.content_hash) for f in stored)
            result=DataProfiler().profile(files); batch=repo.save_profile(batch_id,principal.user_id,body.version,result.model_dump(mode="json"))
            return serialize(runtime,batch,principal.user_id)
        except KeyError as exc: raise HTTPException(404,detail={"code":"resource_not_found"}) from exc
        except ValueError as exc: raise HTTPException(409,detail={"code":"invalid_transition","message":str(exc)}) from exc

    @router.put("/api/v8/imports/{batch_id}/mapping")
    def mapping(batch_id:str,body:MappingBody,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user); runtime,_,agent=components()
        if body.contract.batch_id!=batch_id: raise HTTPException(422,detail={"code":"batch_mismatch"})
        try:
            agent.validate_contract(body.contract,principal.user_id)
            return runtime.quarantine_repository.save_contract(batch_id,principal.user_id,body.version,body.contract.model_dump(mode="json")).model_dump(mode="json")
        except (KeyError,ValueError,ValidationError) as exc: raise HTTPException(422,detail={"code":"mapping_invalid","message":str(exc)}) from exc

    @router.post("/api/v8/imports/{batch_id}/understand")
    def understand(batch_id:str,body:UnderstandBody,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user); runtime,_,agent=components()
        if body.provider not in {"rules","offline_rules"}: raise HTTPException(422,detail={"code":"provider_not_configured","message":"该映射模型尚未配置；可先使用 rules"})
        try:
            current=runtime.quarantine_repository.get_batch(batch_id,principal.user_id)
            if current.version!=body.version: raise ValueError("version_conflict")
            contract=agent.understand(batch_id,principal.user_id,RulesMappingProvider(runtime.domain_registry))
            batch=runtime.quarantine_repository.get_batch(batch_id,principal.user_id)
            return {"batch":batch.model_dump(mode="json"),"contract":contract.model_dump(mode="json")}
        except (KeyError,ValueError) as exc: raise HTTPException(422,detail={"code":"understanding_failed","message":str(exc)}) from exc

    @router.post("/api/v8/imports/{batch_id}/validate")
    def validate(batch_id:str,body:VersionBody,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user); runtime=get_runtime(); repo=runtime.quarantine_repository
        try:
            contract=MappingContract.model_validate(repo.contract_for(batch_id,principal.user_id)); stored=repo.files_for(batch_id,principal.user_id)
            files={f.file_id:TabularFile(filename=f.filename,format=f.format,columns=f.columns,row_count=f.row_count,rows=repo.rows_for(batch_id,f.file_id,principal.user_id),content_hash=f.content_hash) for f in stored}
            report=TransformationValidator(runtime.domain_registry).validate(files,contract)
            batch=repo.save_validation(batch_id,principal.user_id,body.version,report.model_dump(mode="json"))
            return {"batch":batch.model_dump(mode="json"),"report":report.model_dump(mode="json")}
        except KeyError as exc: raise HTTPException(404,detail={"code":"resource_not_found"}) from exc

    @router.get("/api/v8/domains")
    def domains(query:str="",x_insightflow_user:str|None=Header(default=None)):
        actor(x_insightflow_user); values=get_runtime().domain_registry.domains; q=query.lower().strip()
        selected=[d for d in values if not q or q in d.name.lower() or any(q in a.lower() for a in d.aliases)]
        return [d.model_dump(mode="json") for d in selected]

    @router.post("/api/v8/imports/{batch_id}/approve")
    def approve(batch_id:str,body:VersionBody,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user)
        if principal.role is not Role.ADMIN: raise HTTPException(403,detail={"code":"permission_denied"})
        repo=get_runtime().quarantine_repository
        try:
            current=repo.get_batch(batch_id,principal.user_id,True)
            return repo.transition(batch_id,principal.user_id,body.version,"approved",True).model_dump(mode="json")
        except KeyError as exc: raise HTTPException(404,detail={"code":"resource_not_found"}) from exc
        except ValueError as exc: raise HTTPException(409,detail={"code":"invalid_transition","message":str(exc)}) from exc

    @router.post("/api/v8/imports/{batch_id}/publish")
    def publish(batch_id:str,body:VersionBody,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user)
        if principal.role is not Role.ADMIN: raise HTTPException(403,detail={"code":"permission_denied"})
        runtime=get_runtime(); repo=runtime.quarantine_repository
        try:
            current=repo.get_batch(batch_id,principal.user_id,True)
            if current.version!=body.version: raise ValueError("version_conflict")
            # Project into the governed ingestion tables *before* flipping the lifecycle state, so a
            # batch is only marked published once its data is actually bindable by the dynamic runtime.
            projection=runtime.publication_bridge.project(batch_id,principal.user_id,True)
            try:
                batch=repo.transition(batch_id,principal.user_id,body.version,"published",True)
            except Exception:
                # Projection and quarantine state currently use separate repository transactions.
                # Compensate a failed optimistic-lock transition so an approved batch can never
                # leave queryable rows behind while still being marked unpublished.
                runtime.publication_bridge.withdraw(batch_id)
                raise
            return {**batch.model_dump(mode="json"),"projection":projection}
        except KeyError as exc: raise HTTPException(404,detail={"code":"resource_not_found"}) from exc
        except ValueError as exc: raise HTTPException(409,detail={"code":"invalid_transition","message":str(exc)}) from exc

    @router.post("/api/v8/imports/{batch_id}/withdraw")
    def withdraw(batch_id:str,body:WithdrawBody,x_insightflow_user:str|None=Header(default=None)):
        principal=actor(x_insightflow_user)
        if principal.role is not Role.ADMIN: raise HTTPException(403,detail={"code":"permission_denied"})
        runtime=get_runtime(); repo=runtime.quarantine_repository
        try:
            current=repo.get_batch(batch_id,principal.user_id,True)
            if current.version!=body.version: raise ValueError("version_conflict")
            # Remove the queryable projection first. If that fails, lifecycle state remains
            # published, matching what users can still query. If the subsequent optimistic-lock
            # transition fails, restore the projection as saga compensation.
            runtime.publication_bridge.withdraw(batch_id)
            try:
                batch=repo.transition(batch_id,principal.user_id,body.version,"withdrawn",True)
            except Exception:
                runtime.publication_bridge.project(batch_id,principal.user_id,True)
                raise
            return batch.model_dump(mode="json")
        except KeyError as exc: raise HTTPException(404,detail={"code":"resource_not_found"}) from exc
        except ValueError as exc: raise HTTPException(409,detail={"code":"invalid_transition","message":str(exc)}) from exc
    return router

