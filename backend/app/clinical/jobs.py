from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
ALLOWED_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
}


class ClinicalInvestigationJob(BaseModel):
    model_config = ConfigDict(frozen=True)
    job_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_user_id: str
    request: dict[str, Any]
    status: str = "queued"
    investigation_id: str | None = None
    error_code: str | None = None
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt: int = 0
    version: int = 1


class ClinicalJobRepository(Protocol):
    def create(self, item: ClinicalInvestigationJob) -> ClinicalInvestigationJob: ...
    def get(self, job_id: str) -> ClinicalInvestigationJob: ...
    def transition(self, job_id: str, expected_version: int, status: str, investigation_id: str | None = None, error_code: str | None = None) -> ClinicalInvestigationJob: ...
    def cancel(self, job_id: str, expected_version: int) -> ClinicalInvestigationJob: ...
    def resume(self, job_id: str, expected_version: int) -> ClinicalInvestigationJob: ...

    def requeue(self, job_id: str, expected_version: int, error_code: str | None = None) -> ClinicalInvestigationJob: ...

    def claim_next(self, worker_id: str, now: datetime, lease_seconds: float = 60) -> ClinicalInvestigationJob | None: ...
    def heartbeat(self, job_id: str, worker_id: str, expected_version: int, now: datetime, lease_seconds: float = 60) -> ClinicalInvestigationJob: ...


def _transition(item: ClinicalInvestigationJob, expected_version: int, status: str, investigation_id: str | None = None, error_code: str | None = None) -> ClinicalInvestigationJob:
    if item.version != expected_version:
        raise ValueError("version_conflict")
    if status not in ALLOWED_TRANSITIONS.get(item.status, frozenset()):
        raise ValueError(f"job in {item.status} state cannot transition to {status}")
    now = datetime.now(timezone.utc)
    updates: dict[str, Any] = {"status": status, "version": item.version + 1}
    if status == "running":
        updates.update(started_at=now, finished_at=None, cancel_requested=False, error_code=None)
    if status in TERMINAL_STATUSES:
        updates.update(finished_at=now, lease_expires_at=None, worker_id=None)
    if investigation_id is not None:
        updates["investigation_id"] = investigation_id
    if error_code is not None:
        updates["error_code"] = error_code
    return item.model_copy(update=updates)


class InMemoryClinicalJobRepository:
    def __init__(self) -> None:
        self._items: dict[str, ClinicalInvestigationJob] = {}

    def create(self, item: ClinicalInvestigationJob) -> ClinicalInvestigationJob:
        self._items[item.job_id] = item
        return item

    def get(self, job_id: str) -> ClinicalInvestigationJob:
        try:
            return self._items[job_id]
        except KeyError as exc:
            raise KeyError(job_id) from exc

    def transition(self, job_id: str, expected_version: int, status: str, investigation_id: str | None = None, error_code: str | None = None) -> ClinicalInvestigationJob:
        updated = _transition(self.get(job_id), expected_version, status, investigation_id, error_code)
        self._items[job_id] = updated
        return updated

    def cancel(self, job_id: str, expected_version: int) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.status == "running":
            if item.version != expected_version:
                raise ValueError("version_conflict")
            updated = item.model_copy(update={"cancel_requested": True, "version": item.version + 1})
            self._items[job_id] = updated
            return updated
        return self.transition(job_id, expected_version, "cancelled")

    def resume(self, job_id: str, expected_version: int) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.version != expected_version:
            raise ValueError("version_conflict")
        if item.status not in {"failed", "cancelled"}:
            raise ValueError(f"job in {item.status} state cannot be resumed")
        updated = item.model_copy(update={"status": "queued", "finished_at": None, "error_code": None, "cancel_requested": False, "version": item.version + 1})
        self._items[job_id] = updated
        return updated

    def requeue(self, job_id: str, expected_version: int, error_code: str | None = None) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.version != expected_version:
            raise ValueError("version_conflict")
        if item.status != "running":
            raise ValueError(f"job in {item.status} state cannot be requeued")
        updated = item.model_copy(
            update={
                "status": "queued",
                "finished_at": None,
                "worker_id": None,
                "lease_expires_at": None,
                "cancel_requested": False,
                "error_code": error_code,
                "version": item.version + 1,
            }
        )
        self._items[job_id] = updated
        return updated

    def claim_next(self, worker_id: str, now: datetime, lease_seconds: float = 60) -> ClinicalInvestigationJob | None:
        candidate = next((item for item in self._items.values() if item.status == "queued" or (item.status == "running" and item.lease_expires_at is not None and item.lease_expires_at < now)), None)
        if candidate is None:
            return None
        updated = candidate.model_copy(update={
            "status": "running", "started_at": candidate.started_at or now,
            "worker_id": worker_id, "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "attempt": candidate.attempt + 1, "cancel_requested": False, "version": candidate.version + 1,
        })
        self._items[candidate.job_id] = updated
        return updated

    def heartbeat(self, job_id: str, worker_id: str, expected_version: int, now: datetime, lease_seconds: float = 60) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.version != expected_version:
            raise ValueError("version_conflict")
        if item.status != "running" or item.worker_id != worker_id:
            raise ValueError("lease_owner_mismatch")
        updated = item.model_copy(update={"lease_expires_at": now + timedelta(seconds=lease_seconds), "version": item.version + 1})
        self._items[job_id] = updated
        return updated


class PostgresClinicalJobRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    @staticmethod
    def _item(row) -> ClinicalInvestigationJob:
        request = row[2] if isinstance(row[2], dict) else json.loads(row[2])
        return ClinicalInvestigationJob(job_id=str(row[0]), owner_user_id=row[1], request=request, status=row[3], investigation_id=str(row[4]) if row[4] else None, error_code=row[5], cancel_requested=row[6], created_at=row[7], started_at=row[8], finished_at=row[9], version=row[10], worker_id=row[11], lease_expires_at=row[12], attempt=row[13])

    def create(self, item: ClinicalInvestigationJob) -> ClinicalInvestigationJob:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO enterprise.clinical_investigation_jobs(job_id,owner_user_id,request_json,status,version) VALUES (%s,%s,%s,%s,%s)", (item.job_id, item.owner_user_id, Jsonb(item.request), item.status, item.version))
        return item

    def get(self, job_id: str) -> ClinicalInvestigationJob:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT job_id,owner_user_id,request_json,status,investigation_id,error_code,cancel_requested,created_at,started_at,finished_at,version,worker_id,lease_expires_at,attempt FROM enterprise.clinical_investigation_jobs WHERE job_id=%s", (job_id,))
                row = cursor.fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._item(row)

    def _save(self, item: ClinicalInvestigationJob, expected_version: int) -> ClinicalInvestigationJob:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE enterprise.clinical_investigation_jobs SET status=%s,investigation_id=%s,error_code=%s,cancel_requested=%s,started_at=%s,finished_at=%s,version=%s,worker_id=%s,lease_expires_at=%s,attempt=%s WHERE job_id=%s AND version=%s", (item.status, item.investigation_id, item.error_code, item.cancel_requested, item.started_at, item.finished_at, item.version, item.worker_id, item.lease_expires_at, item.attempt, item.job_id, expected_version))
                if cursor.rowcount != 1:
                    raise ValueError("version_conflict")
        return item

    def transition(self, job_id: str, expected_version: int, status: str, investigation_id: str | None = None, error_code: str | None = None) -> ClinicalInvestigationJob:
        return self._save(_transition(self.get(job_id), expected_version, status, investigation_id, error_code), expected_version)

    def cancel(self, job_id: str, expected_version: int) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.status == "running":
            if item.version != expected_version:
                raise ValueError("version_conflict")
            return self._save(item.model_copy(update={"cancel_requested": True, "version": item.version + 1}), expected_version)
        return self.transition(job_id, expected_version, "cancelled")

    def resume(self, job_id: str, expected_version: int) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.version != expected_version:
            raise ValueError("version_conflict")
        if item.status not in {"failed", "cancelled"}:
            raise ValueError(f"job in {item.status} state cannot be resumed")
        return self._save(item.model_copy(update={"status": "queued", "finished_at": None, "error_code": None, "cancel_requested": False, "version": item.version + 1}), expected_version)

    def requeue(self, job_id: str, expected_version: int, error_code: str | None = None) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.version != expected_version:
            raise ValueError("version_conflict")
        if item.status != "running":
            raise ValueError(f"job in {item.status} state cannot be requeued")
        return self._save(
            item.model_copy(
                update={
                    "status": "queued",
                    "finished_at": None,
                    "worker_id": None,
                    "lease_expires_at": None,
                    "cancel_requested": False,
                    "error_code": error_code,
                    "version": item.version + 1,
                }
            ),
            expected_version,
        )

    def claim_next(self, worker_id: str, now: datetime, lease_seconds: float = 60) -> ClinicalInvestigationJob | None:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """WITH candidate AS (
                        SELECT job_id FROM enterprise.clinical_investigation_jobs
                        WHERE status='queued' OR (status='running' AND lease_expires_at < %s)
                        ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                    )
                    UPDATE enterprise.clinical_investigation_jobs j
                    SET status='running', started_at=COALESCE(started_at,%s), worker_id=%s,
                        lease_expires_at=%s + (%s * interval '1 second'), attempt=attempt+1,
                        cancel_requested=false, version=version+1
                    FROM candidate WHERE j.job_id=candidate.job_id
                    RETURNING j.job_id,j.owner_user_id,j.request_json,j.status,j.investigation_id,j.error_code,j.cancel_requested,j.created_at,j.started_at,j.finished_at,j.version,j.worker_id,j.lease_expires_at,j.attempt""",
                    (now, now, worker_id, now, lease_seconds),
                )
                row = cursor.fetchone()
        return self._item(row) if row else None

    def heartbeat(self, job_id: str, worker_id: str, expected_version: int, now: datetime, lease_seconds: float = 60) -> ClinicalInvestigationJob:
        item = self.get(job_id)
        if item.version != expected_version:
            raise ValueError("version_conflict")
        if item.status != "running" or item.worker_id != worker_id:
            raise ValueError("lease_owner_mismatch")
        return self._save(item.model_copy(update={"lease_expires_at": now + timedelta(seconds=lease_seconds), "version": item.version + 1}), expected_version)

