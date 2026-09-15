from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Role(StrEnum):
    ADMIN="admin"
    ANALYST="analyst"
    VIEWER="viewer"


class Capability(StrEnum):
    INVESTIGATION_CREATE="investigation:create"
    INVESTIGATION_READ="investigation:read"
    APPROVAL_DECIDE="approval:decide"
    SCHEDULE_MANAGE="schedule:manage"
    AUDIT_READ="audit:read"


class Principal(BaseModel):
    model_config=ConfigDict(frozen=True)
    user_id: str
    email: str
    role: Role
    allowed_countries: tuple[str,...]=()
    allowed_account_manager_ids: tuple[str,...]=()
    allowed_trial_ids: tuple[str,...]=()
    allowed_regions: tuple[str,...]=()
    allowed_site_ids: tuple[str,...]=()
    active: bool=True


class DataScope(BaseModel):
    model_config=ConfigDict(frozen=True)
    global_access: bool=False
    countries: tuple[str,...]=()
    account_manager_ids: tuple[str,...]=()
    trial_ids: tuple[str,...]=()
    regions: tuple[str,...]=()
    site_ids: tuple[str,...]=()
    role: Role

    @model_validator(mode="after")
    def validate_global(self):
        if self.global_access and self.role is not Role.ADMIN:
            raise ValueError("Only administrators may have global access")
        return self

    @classmethod
    def from_principal(cls,principal:Principal):
        return cls(global_access=principal.role is Role.ADMIN,countries=principal.allowed_countries,account_manager_ids=principal.allowed_account_manager_ids,trial_ids=principal.allowed_trial_ids,regions=principal.allowed_regions,site_ids=principal.allowed_site_ids,role=principal.role)

    def allows(self,country:str,account_manager_id:str)->bool:
        return self.global_access or (country in self.countries and account_manager_id in self.account_manager_ids)

    def allows_clinical(self,trial_id:str,region:str|None=None,site_id:str|None=None)->bool:
        if self.global_access:
            return True
        if trial_id not in self.trial_ids:
            return False
        if region is not None and self.regions and region not in self.regions:
            return False
        if site_id is not None and self.site_ids and site_id not in self.site_ids:
            return False
        return True


class ApprovalStatus(StrEnum):
    PENDING="pending"
    APPROVED="approved"
    REJECTED="rejected"
    CANCELLED="cancelled"


class ApprovalRequest(BaseModel):
    approval_id: str=Field(default_factory=lambda:str(uuid4()))
    investigation_id: str
    requested_by: str
    status: ApprovalStatus=ApprovalStatus.PENDING
    reason: str
    decided_by: str|None=None
    decision_comment: str|None=None
    created_at: datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    decided_at: datetime|None=None
    version: int=1


class AuditEvent(BaseModel):
    event_id: str=Field(default_factory=lambda:str(uuid4()))
    actor_user_id: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str="success"
    metadata: dict[str,Any]=Field(default_factory=dict)
    request_id: str
    created_at: datetime=Field(default_factory=lambda:datetime.now(timezone.utc))


class EnterpriseInvestigation(BaseModel):
    investigation_id: str
    owner_user_id: str
    scope: DataScope
    publication_status: str
    state: Any
    # ``state`` remains the V4-V16-compatible response projection.  For canonical V17 writes,
    # ``graph_state`` is the durable typed snapshot and is the only state accepted for replay.
    # Keeping the projection additive avoids changing existing API serializers during migration.
    graph_state: Any | None = Field(default=None, exclude=True)
    graph_state_version: int | None = Field(default=None, exclude=True)
    created_at: datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    updated_at: datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    version: int=1
    request_id: str | None = None


class InvestigationSchedule(BaseModel):
    schedule_id:str=Field(default_factory=lambda:str(uuid4()))
    owner_user_id:str
    name:str
    question:str
    cron_expression:str
    timezone:str
    scope:DataScope
    provider:str|None=None
    enabled:bool=True
    next_run_at:datetime|None=None
    last_run_at:datetime|None=None
    created_at:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    version:int=1


class Alert(BaseModel):
    alert_id:str=Field(default_factory=lambda:str(uuid4()))
    investigation_id:str
    recipient_user_id:str
    severity:str="info"
    title:str
    body:str
    status:str="unread"
    created_at:datetime=Field(default_factory=lambda:datetime.now(timezone.utc))
    read_at:datetime|None=None

