"""Durable, retryable approval-signal outbox for the optional Temporal path.

The outbox stores the decision metadata needed to recreate a short-lived resume token.  It does
not store the bearer token itself, SQL, evidence rows, or patient data.  A dispatcher mints a
fresh token from the deployment secret for each delivery attempt, so a transient Temporal outage
does not leave the database decision without a retry path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter
import threading
import re
from typing import Any, Literal, Protocol
from uuid import uuid4

import psycopg
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.clinical.temporal_boundary import TemporalApprovalSignal
from app.enterprise.approval import ApprovalTokenError, issue_resume_token
from app.enterprise.models import ApprovalStatus
from app.clinical.operations import OPERATIONS


class TemporalOutboxError(RuntimeError):
    """An outbox operation or delivery attempt failed."""


class TemporalOutboxPermanentError(TemporalOutboxError):
    """The durable decision can never match this outbox event on a later retry."""


_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")


def _error_code(error: str | BaseException) -> str:
    """Reduce an exception/string to a stable operational code, never its message."""

    if isinstance(error, BaseException):
        return type(error).__name__[:80]
    candidate = str(error).strip()
    return candidate if _ERROR_CODE.fullmatch(candidate) else "outbox_delivery_error"


class TemporalOutboxEvent(BaseModel):
    """Metadata required to retry one approval signal without storing the token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=120)
    workflow_id: str = Field(min_length=1, max_length=120)
    approval_id: str = Field(min_length=1, max_length=120)
    expected_version: int = Field(ge=1)
    approved: bool
    decided_by: str = Field(min_length=1, max_length=120)
    decided_at: datetime
    comment: str = Field(default="", max_length=2000)
    status: Literal["pending", "sent", "dead"] = "pending"
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=80)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    sent_at: datetime | None = None
    # Lease metadata is operational only and deliberately excluded from API/event serialization.
    # A worker may reclaim an event after the lease expires if it crashed mid-delivery.
    claim_id: str | None = Field(default=None, max_length=120, exclude=True)
    claim_expires_at: datetime | None = Field(default=None, exclude=True)

    @field_validator("last_error")
    @classmethod
    def normalize_last_error(cls, value: str | None) -> str | None:
        return None if value is None else _error_code(value)


class TemporalSignalOutbox(Protocol):
    def enqueue(self, event: TemporalOutboxEvent) -> TemporalOutboxEvent: ...

    def pending(self, *, limit: int = 20) -> list[TemporalOutboxEvent]: ...

    def pending_for_event(self, event_id: str) -> TemporalOutboxEvent | None: ...

    def mark_sent(self, event_id: str, sent_at: datetime | None = None, *, claim_id: str | None = None) -> TemporalOutboxEvent: ...

    def mark_failed(self, event_id: str, error: str | BaseException, *, max_attempts: int = 5, claim_id: str | None = None) -> TemporalOutboxEvent: ...


class InMemoryTemporalSignalOutbox:
    """Process-local implementation used in tests and explicit memory deployments."""

    def __init__(self, *, claim_ttl_seconds: float = 60.0) -> None:
        if claim_ttl_seconds <= 0:
            raise ValueError("claim_ttl_seconds must be positive")
        self._events: dict[str, TemporalOutboxEvent] = {}
        self._claim_ttl_seconds = claim_ttl_seconds
        self._lock = threading.RLock()

    def enqueue(self, event: TemporalOutboxEvent) -> TemporalOutboxEvent:
        with self._lock:
            if event.event_id in self._events:
                raise TemporalOutboxError("duplicate temporal outbox event")
            if any(
                item.approval_id == event.approval_id
                and item.expected_version == event.expected_version
                for item in self._events.values()
            ):
                raise TemporalOutboxError("duplicate approval decision outbox event")
            self._events[event.event_id] = event
            return event

    def pending(self, *, limit: int = 20) -> list[TemporalOutboxEvent]:
        if limit < 1:
            raise ValueError("outbox limit must be positive")
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=self._claim_ttl_seconds)
        claimed: list[TemporalOutboxEvent] = []
        with self._lock:
            for event_id, item in self._events.items():
                if item.status != "pending":
                    continue
                if item.claim_expires_at is not None and item.claim_expires_at > now:
                    continue
                updated = item.model_copy(
                    update={"claim_id": str(uuid4()), "claim_expires_at": expires}
                )
                self._events[event_id] = updated
                claimed.append(updated)
                if len(claimed) >= limit:
                    break
        return claimed

    def pending_for_event(self, event_id: str) -> TemporalOutboxEvent | None:
        """Atomically claim one known event without scanning unrelated outbox work.

        This narrow operation is used by isolated migration/canary checks.  It prevents a test
        or administrative probe from leasing the oldest production event merely because it shares
        the same database.
        """

        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=self._claim_ttl_seconds)
        with self._lock:
            item = self._events.get(event_id)
            if item is None or item.status != "pending":
                return None
            if item.claim_expires_at is not None and item.claim_expires_at > now:
                return None
            updated = item.model_copy(update={"claim_id": str(uuid4()), "claim_expires_at": expires})
            self._events[event_id] = updated
            return updated

    def mark_sent(self, event_id: str, sent_at: datetime | None = None, *, claim_id: str | None = None) -> TemporalOutboxEvent:
        with self._lock:
            item = self._get(event_id)
            if item.status != "pending" or (claim_id is not None and item.claim_id != claim_id) or (claim_id is None and item.claim_id is not None):
                return item
            updated = item.model_copy(
                update={"status": "sent", "sent_at": sent_at or datetime.now(timezone.utc), "last_error": None, "claim_id": None, "claim_expires_at": None}
            )
            self._events[event_id] = updated
            return updated

    def mark_failed(self, event_id: str, error: str | BaseException, *, max_attempts: int = 5, claim_id: str | None = None) -> TemporalOutboxEvent:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        with self._lock:
            item = self._get(event_id)
            if item.status != "pending" or (claim_id is not None and item.claim_id != claim_id) or (claim_id is None and item.claim_id is not None):
                return item
            attempts = item.attempts + 1
            status = "dead" if attempts >= max_attempts else "pending"
            updated = item.model_copy(update={"attempts": attempts, "status": status, "last_error": _error_code(error), "claim_id": None, "claim_expires_at": None})
            self._events[event_id] = updated
            return updated

    def _get(self, event_id: str) -> TemporalOutboxEvent:
        try:
            return self._events[event_id]
        except KeyError as exc:
            raise TemporalOutboxError("unknown temporal outbox event") from exc


class PostgresTemporalSignalOutbox:
    """PostgreSQL implementation; only decision metadata is persisted."""

    def __init__(self, database_url: str, *, claim_ttl_seconds: float = 60.0) -> None:
        if claim_ttl_seconds <= 0:
            raise ValueError("claim_ttl_seconds must be positive")
        self._database_url = database_url
        self._claim_ttl_seconds = claim_ttl_seconds

    @staticmethod
    def _from_row(row: tuple[Any, ...]) -> TemporalOutboxEvent:
        return TemporalOutboxEvent(
            event_id=str(row[0]),
            workflow_id=row[1],
            approval_id=row[2],
            expected_version=row[3],
            approved=row[4],
            decided_by=row[5],
            decided_at=row[6],
            comment=row[7],
            status=row[8],
            attempts=row[9],
            last_error=row[10],
            created_at=row[11],
            sent_at=row[12],
            claim_id=row[13],
            claim_expires_at=row[14],
        )

    def enqueue(self, event: TemporalOutboxEvent) -> TemporalOutboxEvent:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                self.enqueue_with_cursor(cursor, event)
        return event

    @staticmethod
    def enqueue_with_cursor(cursor: Any, event: TemporalOutboxEvent) -> None:
        """Insert into an existing transaction used by the approval repository."""

        cursor.execute(
            """INSERT INTO enterprise.temporal_signal_outbox
            (event_id, workflow_id, approval_id, expected_version, approved, decided_by,
             decided_at, comment, status, attempts, last_error, created_at, sent_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                event.event_id,
                event.workflow_id,
                event.approval_id,
                event.expected_version,
                event.approved,
                event.decided_by,
                event.decided_at,
                event.comment,
                event.status,
                event.attempts,
                event.last_error,
                event.created_at,
                event.sent_at,
            ),
        )

    def pending(self, *, limit: int = 20) -> list[TemporalOutboxEvent]:
        if limit < 1:
            raise ValueError("outbox limit must be positive")
        claim_id = str(uuid4())
        claim_expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._claim_ttl_seconds)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """WITH candidates AS (
                        SELECT event_id
                        FROM enterprise.temporal_signal_outbox
                        WHERE status='pending'
                          AND (claim_expires_at IS NULL OR claim_expires_at <= now())
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT %s
                    )
                    UPDATE enterprise.temporal_signal_outbox AS outbox
                    SET claim_id=%s, claim_expires_at=%s
                    FROM candidates
                    WHERE outbox.event_id=candidates.event_id
                    RETURNING outbox.event_id,outbox.workflow_id,outbox.approval_id,outbox.expected_version,
                    outbox.approved,outbox.decided_by,outbox.decided_at,outbox.comment,outbox.status,
                    outbox.attempts,outbox.last_error,outbox.created_at,outbox.sent_at,
                    outbox.claim_id,outbox.claim_expires_at""",
                    (limit, claim_id, claim_expires_at),
                )
                rows = cursor.fetchall()
        return [self._from_row(row) for row in rows]

    def pending_for_event(self, event_id: str) -> TemporalOutboxEvent | None:
        """Atomically claim a specific pending event, leaving unrelated rows untouched."""

        claim_id = str(uuid4())
        claim_expires_at = datetime.now(timezone.utc) + timedelta(seconds=self._claim_ttl_seconds)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """WITH candidate AS (
                        SELECT event_id
                        FROM enterprise.temporal_signal_outbox
                        WHERE event_id=%s AND status='pending'
                          AND (claim_expires_at IS NULL OR claim_expires_at <= now())
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE enterprise.temporal_signal_outbox AS outbox
                    SET claim_id=%s, claim_expires_at=%s
                    FROM candidate
                    WHERE outbox.event_id=candidate.event_id
                    RETURNING outbox.event_id,outbox.workflow_id,outbox.approval_id,outbox.expected_version,
                    outbox.approved,outbox.decided_by,outbox.decided_at,outbox.comment,outbox.status,
                    outbox.attempts,outbox.last_error,outbox.created_at,outbox.sent_at,
                    outbox.claim_id,outbox.claim_expires_at""",
                    (event_id, claim_id, claim_expires_at),
                )
                row = cursor.fetchone()
        return self._from_row(row) if row else None

    def mark_sent(self, event_id: str, sent_at: datetime | None = None, *, claim_id: str | None = None) -> TemporalOutboxEvent:
        timestamp = sent_at or datetime.now(timezone.utc)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                claim_clause = "AND claim_id=%s" if claim_id is not None else "AND claim_id IS NULL"
                params: tuple[Any, ...] = (timestamp, event_id, claim_id) if claim_id is not None else (timestamp, event_id)
                cursor.execute(
                    f"""UPDATE enterprise.temporal_signal_outbox
                    SET status='sent', sent_at=%s, last_error=NULL, claim_id=NULL, claim_expires_at=NULL
                    WHERE event_id=%s AND status='pending' {claim_clause}
                    RETURNING event_id,workflow_id,approval_id,expected_version,approved,decided_by,
                    decided_at,comment,status,attempts,last_error,created_at,sent_at,claim_id,claim_expires_at""",
                    params,
                )
                row = cursor.fetchone()
        if row is None:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT event_id,workflow_id,approval_id,expected_version,approved,decided_by,
                        decided_at,comment,status,attempts,last_error,created_at,sent_at,claim_id,claim_expires_at
                        FROM enterprise.temporal_signal_outbox WHERE event_id=%s""",
                        (event_id,),
                    )
                    row = cursor.fetchone()
            if row is None:
                raise TemporalOutboxError("unknown temporal outbox event")
        return self._from_row(row)

    def mark_failed(self, event_id: str, error: str | BaseException, *, max_attempts: int = 5, claim_id: str | None = None) -> TemporalOutboxEvent:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                claim_clause = "AND claim_id=%s" if claim_id is not None else "AND claim_id IS NULL"
                params: tuple[Any, ...] = (max_attempts, _error_code(error), event_id, claim_id) if claim_id is not None else (max_attempts, _error_code(error), event_id)
                cursor.execute(
                    f"""UPDATE enterprise.temporal_signal_outbox
                    SET attempts=attempts+1,
                        status=CASE WHEN attempts+1 >= %s THEN 'dead' ELSE 'pending' END,
                        last_error=%s, claim_id=NULL, claim_expires_at=NULL
                    WHERE event_id=%s AND status='pending' {claim_clause}
                    RETURNING event_id,workflow_id,approval_id,expected_version,approved,decided_by,
                    decided_at,comment,status,attempts,last_error,created_at,sent_at,claim_id,claim_expires_at""",
                    params,
                )
                row = cursor.fetchone()
        if row is None:
            with psycopg.connect(self._database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT event_id,workflow_id,approval_id,expected_version,approved,decided_by,
                        decided_at,comment,status,attempts,last_error,created_at,sent_at,claim_id,claim_expires_at
                        FROM enterprise.temporal_signal_outbox WHERE event_id=%s""",
                        (event_id,),
                    )
                    row = cursor.fetchone()
            if row is None:
                raise TemporalOutboxError("unknown temporal outbox event")
        return self._from_row(row)


class TemporalOutboxDispatcher:
    """Deliver pending signals and keep failures retryable."""

    def __init__(
        self,
        outbox: TemporalSignalOutbox,
        *,
        boundary_factory: Any,
        approval_repository: Any,
        settings: Any,
        max_attempts: int = 5,
    ) -> None:
        self._outbox = outbox
        self._boundary_factory = boundary_factory
        self._approval_repository = approval_repository
        self._settings = settings
        self._max_attempts = max_attempts

    async def dispatch(
        self,
        event: TemporalOutboxEvent,
        *,
        resume_token: str | None = None,
    ) -> TemporalOutboxEvent:
        if event.status != "pending":
            return event
        started = perf_counter()

        def record(result: TemporalOutboxEvent) -> TemporalOutboxEvent:
            # ``event.attempts`` counts failures committed before this delivery.  This keeps the
            # metric a count of actual retries (not a cumulative sum repeated on every attempt),
            # while the durable outbox remains the source of truth for replay decisions.
            is_retry = event.attempts > 0
            OPERATIONS.record_event(
                "temporal_outbox",
                result.status,
                (perf_counter() - started) * 1000,
                retry=is_retry,
            )
            if is_retry:
                OPERATIONS.record_integrity("retry", "temporal_outbox")
            return result

        try:
            approval = self._approval_repository.get_approval(event.approval_id)
            expected_status = ApprovalStatus.APPROVED if event.approved else ApprovalStatus.REJECTED
            if approval.status is not expected_status or approval.version != event.expected_version + 1:
                raise TemporalOutboxPermanentError("approval state no longer matches outbox decision")
            # Recreate a short-lived token bound to the version that was checked before the
            # decision.  The token is not persisted in the outbox.
            secret = str(getattr(self._settings, "insightflow_approval_resume_secret", ""))
            token_source = approval.model_copy(update={"version": event.expected_version})
            token = resume_token or issue_resume_token(
                token_source,
                secret=secret,
                ttl_seconds=int(getattr(self._settings, "insightflow_approval_resume_ttl_seconds", 900)),
            )
            signal = TemporalApprovalSignal(
                approval_id=event.approval_id,
                version=event.expected_version,
                approved=event.approved,
                decided_by=event.decided_by,
                decided_at=event.decided_at,
                comment=event.comment,
                resume_token=token,
            )
            boundary = self._boundary_factory(self._settings)
            await boundary.signal_approval(event.workflow_id, signal)
            delivered = self._outbox.mark_sent(event.event_id, claim_id=event.claim_id)
            if delivered.status != "sent":
                # The signal may have been sent by another worker after this lease expired.  Do
                # not publish or overwrite that worker's durable state from a stale attempt.
                return record(delivered)
            OPERATIONS.record_integrity("approval", "temporal_signal_delivered")
            if event.approved:
                self._approval_repository.publish_investigation(event.workflow_id)
            return record(delivered)
        except TemporalOutboxPermanentError as exc:
            # A stale/contradictory decision is deterministic; retrying it only creates noise and
            # can hide a real operator or data-version conflict behind a retry budget.
            return record(self._outbox.mark_failed(event.event_id, exc, max_attempts=1, claim_id=event.claim_id))
        except (ApprovalTokenError, TemporalOutboxError, KeyError, ValueError) as exc:
            return record(self._outbox.mark_failed(event.event_id, exc, max_attempts=self._max_attempts, claim_id=event.claim_id))
        except Exception as exc:  # network/client failures are retryable
            return record(self._outbox.mark_failed(event.event_id, exc, max_attempts=self._max_attempts, claim_id=event.claim_id))

    async def dispatch_pending(self, *, limit: int = 20) -> list[TemporalOutboxEvent]:
        results: list[TemporalOutboxEvent] = []
        for event in self._outbox.pending(limit=limit):
            results.append(await self.dispatch(event))
        return results

