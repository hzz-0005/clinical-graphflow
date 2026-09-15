from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field

from app.enterprise.models import ApprovalRequest,ApprovalStatus,Principal,Role


class ApprovalConflict(ValueError): pass


class ApprovalTokenError(ValueError):
    """A signed approval resume token is malformed, expired, or bound to another version."""


class ApprovalResumeClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1, max_length=120)
    investigation_id: str = Field(min_length=1, max_length=120)
    requested_by: str = Field(min_length=1, max_length=120)
    version: int = Field(ge=1)
    issued_at: int = Field(ge=0)
    expires_at: int = Field(ge=1)


def _token_part(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_token_part(value: str) -> dict[str, object]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApprovalTokenError("invalid approval resume token") from exc
    if not isinstance(payload, dict):
        raise ApprovalTokenError("invalid approval resume token payload")
    return payload


def issue_resume_token(
    item: ApprovalRequest,
    *,
    secret: str,
    ttl_seconds: int = 900,
    now: datetime | None = None,
) -> str:
    if not secret:
        raise ApprovalTokenError("approval resume token secret is not configured")
    if ttl_seconds <= 0:
        raise ApprovalTokenError("approval resume token ttl must be positive")
    issued_at = int((now or datetime.now(timezone.utc)).timestamp())
    payload = {
        "approval_id": item.approval_id,
        "investigation_id": item.investigation_id,
        "requested_by": item.requested_by,
        "version": item.version,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
    }
    encoded = _token_part(payload)
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode('ascii')}"


def verify_resume_token(
    token: str,
    *,
    secret: str,
    expected_approval_id: str,
    expected_investigation_id: str,
    expected_version: int,
    now: datetime | None = None,
) -> ApprovalResumeClaims:
    if not secret:
        raise ApprovalTokenError("approval resume token secret is not configured")
    parts = token.split(".")
    if len(parts) != 2 or not all(parts):
        raise ApprovalTokenError("invalid approval resume token")
    encoded, supplied_signature = parts
    expected_signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ApprovalTokenError("invalid approval resume token signature")
    claims = ApprovalResumeClaims.model_validate(_decode_token_part(encoded))
    current = int((now or datetime.now(timezone.utc)).timestamp())
    if current >= claims.expires_at:
        raise ApprovalTokenError("approval resume token expired")
    if claims.approval_id != expected_approval_id:
        raise ApprovalTokenError("approval token approval mismatch")
    if claims.investigation_id != expected_investigation_id:
        raise ApprovalTokenError("approval token investigation mismatch")
    if claims.version != expected_version:
        raise ApprovalTokenError("approval token version conflict")
    return claims


class ApprovalMachine:
    def _check(self,item:ApprovalRequest,actor:Principal,version:int)->None:
        if actor.role is not Role.ADMIN: raise PermissionError("Only administrators can decide approvals")
        if actor.user_id==item.requested_by: raise ApprovalConflict("A requester cannot approve their own request")
        if item.version!=version: raise ApprovalConflict("Approval version conflict")
        if item.status is not ApprovalStatus.PENDING: raise ApprovalConflict("Approval is already terminal")

    def _decide(self,item,actor,version,comment,status):
        self._check(item,actor,version)
        return item.model_copy(update={"status":status,"decided_by":actor.user_id,"decision_comment":comment,"decided_at":datetime.now(timezone.utc),"version":item.version+1})

    def approve(self,item,actor,version,comment): return self._decide(item,actor,version,comment,ApprovalStatus.APPROVED)
    def reject(self,item,actor,version,comment): return self._decide(item,actor,version,comment,ApprovalStatus.REJECTED)

    def cancel(self,item:ApprovalRequest,actor:Principal,version:int)->ApprovalRequest:
        if item.requested_by!=actor.user_id: raise PermissionError("Only requester can cancel")
        if item.version!=version: raise ApprovalConflict("Approval version conflict")
        if item.status is not ApprovalStatus.PENDING: raise ApprovalConflict("Approval is already terminal")
        return item.model_copy(update={"status":ApprovalStatus.CANCELLED,"decided_by":actor.user_id,"decided_at":datetime.now(timezone.utc),"version":item.version+1})

