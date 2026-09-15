from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from app.clinical.tabular import TabularFile


ALLOWED_TRANSITIONS = {
    "parsed": frozenset({"profiled", "parse_failed"}),
    "parse_failed": frozenset({"parsed"}),
    "profiled": frozenset({"mapping_proposed", "profiling_failed"}),
    "profiling_failed": frozenset({"profiled"}),
    "mapping_proposed": frozenset({"validated", "validation_failed"}),
    "validation_failed": frozenset({"mapping_proposed"}),
    "validated": frozenset({"approved"}),
    "approved": frozenset({"published"}),
    # Withdrawal keeps the batch record and its audit trail but removes it from the analytics layer,
    # so a withdrawn governed version can no longer be selected or bound by an investigation.
    "published": frozenset({"withdrawn"}),
}


class QuarantineBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    batch_id: str = Field(default_factory=lambda: str(uuid4()))
    owner_user_id: str
    status: str = "parsed"
    visibility_level: str = Field(default="L1", pattern=r"^L[0-4]$")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    file_count: int = Field(ge=1)
    row_count: int = Field(ge=0)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class QuarantineFile(BaseModel):
    model_config = ConfigDict(frozen=True)

    file_id: str = Field(default_factory=lambda: str(uuid4()))
    batch_id: str
    filename: str
    format: str
    adapter_version: str = "1"
    columns: tuple[str, ...]
    row_count: int = Field(ge=0)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class QuarantineRepository(Protocol):
    def create_batch(self, owner_user_id: str, files: tuple[TabularFile, ...], visibility_level: str = "L1") -> QuarantineBatch: ...
    def get_batch(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> QuarantineBatch: ...
    def files_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> tuple[QuarantineFile, ...]: ...
    def rows_for(self, batch_id: str, file_id: str, actor_user_id: str, global_access: bool = False) -> tuple[dict[str, Any], ...]: ...
    def transition(self, batch_id: str, actor_user_id: str, expected_version: int, target: str, global_access: bool = False) -> QuarantineBatch: ...
    def save_profile(self, batch_id: str, actor_user_id: str, expected_version: int, profile: dict[str, Any], global_access: bool = False) -> QuarantineBatch: ...
    def profile_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]: ...
    def save_contract(self, batch_id: str, actor_user_id: str, expected_version: int, contract: dict[str, Any], global_access: bool = False) -> QuarantineBatch: ...
    def contract_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]: ...
    def save_validation(self, batch_id: str, actor_user_id: str, expected_version: int, report: dict[str, Any], global_access: bool = False) -> QuarantineBatch: ...
    def validation_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]: ...


def _batch_hash(files: tuple[TabularFile, ...]) -> str:
    joined = "\0".join(sorted(item.content_hash for item in files)).encode("ascii")
    return hashlib.sha256(joined).hexdigest()


def _transition(item: QuarantineBatch, expected_version: int, target: str) -> QuarantineBatch:
    if item.version != expected_version:
        raise ValueError("version_conflict")
    if target not in ALLOWED_TRANSITIONS.get(item.status, frozenset()):
        raise ValueError(f"batch in {item.status} state cannot transition to {target}")
    return item.model_copy(
        update={
            "status": target,
            "version": item.version + 1,
            "updated_at": datetime.now(timezone.utc),
        }
    )


class InMemoryQuarantineRepository:
    def __init__(self) -> None:
        self._batches: dict[str, QuarantineBatch] = {}
        self._files: dict[str, tuple[QuarantineFile, ...]] = {}
        self._rows: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
        self._profiles: dict[str, dict[str, Any]] = {}
        self._contracts: dict[str, dict[str, Any]] = {}
        self._validations: dict[str, dict[str, Any]] = {}

    def create_batch(self, owner_user_id: str, files: tuple[TabularFile, ...], visibility_level: str = "L1") -> QuarantineBatch:
        if not files:
            raise ValueError("at least one parsed file is required")
        batch = QuarantineBatch(
            owner_user_id=owner_user_id,
            visibility_level=visibility_level,
            content_hash=_batch_hash(files),
            file_count=len(files),
            row_count=sum(item.row_count for item in files),
        )
        stored_files = tuple(
            QuarantineFile(
                batch_id=batch.batch_id,
                filename=item.filename,
                format=item.format,
                columns=item.columns,
                row_count=item.row_count,
                content_hash=item.content_hash,
            )
            for item in files
        )
        self._batches[batch.batch_id] = batch
        self._files[batch.batch_id] = stored_files
        for stored, source in zip(stored_files, files, strict=True):
            self._rows[(batch.batch_id, stored.file_id)] = tuple(dict(row) for row in source.rows)
        return batch

    def get_batch(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> QuarantineBatch:
        item = self._batches.get(batch_id)
        if item is None or (not global_access and item.owner_user_id != actor_user_id):
            raise KeyError(batch_id)
        return item

    def files_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> tuple[QuarantineFile, ...]:
        self.get_batch(batch_id, actor_user_id, global_access)
        return self._files[batch_id]

    def rows_for(self, batch_id: str, file_id: str, actor_user_id: str, global_access: bool = False) -> tuple[dict[str, Any], ...]:
        self.get_batch(batch_id, actor_user_id, global_access)
        try:
            return self._rows[(batch_id, file_id)]
        except KeyError as exc:
            raise KeyError(file_id) from exc

    def transition(self, batch_id: str, actor_user_id: str, expected_version: int, target: str, global_access: bool = False) -> QuarantineBatch:
        updated = _transition(self.get_batch(batch_id, actor_user_id, global_access), expected_version, target)
        self._batches[batch_id] = updated
        return updated

    def save_profile(self, batch_id: str, actor_user_id: str, expected_version: int, profile: dict[str, Any], global_access: bool = False) -> QuarantineBatch:
        updated = self.transition(batch_id, actor_user_id, expected_version, "profiled", global_access)
        self._profiles[batch_id] = profile
        return updated

    def profile_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]:
        self.get_batch(batch_id, actor_user_id, global_access)
        return self._profiles[batch_id]

    def save_contract(self, batch_id: str, actor_user_id: str, expected_version: int, contract: dict[str, Any], global_access: bool = False) -> QuarantineBatch:
        updated = self.transition(batch_id, actor_user_id, expected_version, "mapping_proposed", global_access)
        self._contracts[batch_id] = contract
        return updated

    def contract_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]:
        self.get_batch(batch_id, actor_user_id, global_access)
        return self._contracts[batch_id]

    def save_validation(self, batch_id: str, actor_user_id: str, expected_version: int, report: dict[str, Any], global_access: bool = False) -> QuarantineBatch:
        target = "validated" if bool(report.get("valid")) else "validation_failed"
        updated = self.transition(batch_id, actor_user_id, expected_version, target, global_access)
        self._validations[batch_id] = report
        return updated

    def validation_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]:
        self.get_batch(batch_id, actor_user_id, global_access)
        return self._validations[batch_id]


class PostgresQuarantineRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    @staticmethod
    def _batch(row) -> QuarantineBatch:
        return QuarantineBatch(
            batch_id=str(row[0]), owner_user_id=row[1], status=row[2],
            visibility_level=row[3], content_hash=row[4], file_count=row[5],
            row_count=row[6], version=row[7], created_at=row[8], updated_at=row[9],
        )

    def create_batch(self, owner_user_id: str, files: tuple[TabularFile, ...], visibility_level: str = "L1") -> QuarantineBatch:
        if not files:
            raise ValueError("at least one parsed file is required")
        batch = QuarantineBatch(
            owner_user_id=owner_user_id, visibility_level=visibility_level,
            content_hash=_batch_hash(files), file_count=len(files),
            row_count=sum(item.row_count for item in files),
        )
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO clinical_quarantine.import_batches
                    (batch_id,owner_user_id,status,visibility_level,content_hash,file_count,row_count,version,created_at,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (batch.batch_id, batch.owner_user_id, batch.status, batch.visibility_level,
                     batch.content_hash, batch.file_count, batch.row_count, batch.version,
                     batch.created_at, batch.updated_at),
                )
                for source in files:
                    stored = QuarantineFile(
                        batch_id=batch.batch_id, filename=source.filename, format=source.format,
                        columns=source.columns, row_count=source.row_count,
                        content_hash=source.content_hash,
                    )
                    cursor.execute(
                        """INSERT INTO clinical_quarantine.files
                        (file_id,batch_id,filename,format,adapter_version,columns_json,row_count,content_hash)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (stored.file_id, stored.batch_id, stored.filename, stored.format,
                         stored.adapter_version, Jsonb(list(stored.columns)), stored.row_count,
                         stored.content_hash),
                    )
                    cursor.executemany(
                        """INSERT INTO clinical_quarantine.rows(batch_id,file_id,row_number,payload_json)
                        VALUES (%s,%s,%s,%s)""",
                        [(batch.batch_id, stored.file_id, index, Jsonb(dict(row))) for index, row in enumerate(source.rows, 1)],
                    )
        return batch

    def get_batch(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> QuarantineBatch:
        clause = "batch_id=%s" if global_access else "batch_id=%s AND owner_user_id=%s"
        params = (batch_id,) if global_access else (batch_id, actor_user_id)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT batch_id,owner_user_id,status,visibility_level,content_hash,file_count,row_count,version,created_at,updated_at FROM clinical_quarantine.import_batches WHERE " + clause,
                    params,
                )
                row = cursor.fetchone()
        if row is None:
            raise KeyError(batch_id)
        return self._batch(row)

    def files_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> tuple[QuarantineFile, ...]:
        self.get_batch(batch_id, actor_user_id, global_access)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT file_id,batch_id,filename,format,adapter_version,columns_json,row_count,content_hash FROM clinical_quarantine.files WHERE batch_id=%s ORDER BY filename", (batch_id,))
                rows = cursor.fetchall()
        return tuple(QuarantineFile(file_id=str(row[0]), batch_id=str(row[1]), filename=row[2], format=row[3], adapter_version=row[4], columns=tuple(row[5] if isinstance(row[5], list) else json.loads(row[5])), row_count=row[6], content_hash=row[7]) for row in rows)

    def rows_for(self, batch_id: str, file_id: str, actor_user_id: str, global_access: bool = False) -> tuple[dict[str, Any], ...]:
        files = self.files_for(batch_id, actor_user_id, global_access)
        if file_id not in {item.file_id for item in files}:
            raise KeyError(file_id)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT payload_json FROM clinical_quarantine.rows WHERE batch_id=%s AND file_id=%s ORDER BY row_number", (batch_id, file_id))
                rows = cursor.fetchall()
        return tuple(row[0] if isinstance(row[0], dict) else json.loads(row[0]) for row in rows)

    def transition(self, batch_id: str, actor_user_id: str, expected_version: int, target: str, global_access: bool = False) -> QuarantineBatch:
        current = self.get_batch(batch_id, actor_user_id, global_access)
        updated = _transition(current, expected_version, target)
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE clinical_quarantine.import_batches SET status=%s,version=%s,updated_at=%s WHERE batch_id=%s AND version=%s",
                    (updated.status, updated.version, updated.updated_at, batch_id, expected_version),
                )
                if cursor.rowcount != 1:
                    raise ValueError("version_conflict")
        return updated

    def _save_json(self, table: str, column: str, batch_id: str, payload: dict[str, Any]) -> None:
        if table not in {"column_profiles", "mapping_contracts", "validation_runs"}:
            raise ValueError("unsupported quarantine document table")
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO clinical_quarantine.{table}(batch_id,{column}) VALUES (%s,%s) ON CONFLICT (batch_id) DO UPDATE SET {column}=EXCLUDED.{column},created_at=now()",
                    (batch_id, Jsonb(payload)),
                )

    def _load_json(self, table: str, column: str, batch_id: str) -> dict[str, Any]:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT {column} FROM clinical_quarantine.{table} WHERE batch_id=%s", (batch_id,))
                row = cursor.fetchone()
        if row is None:
            raise KeyError(batch_id)
        return row[0] if isinstance(row[0], dict) else json.loads(row[0])

    def save_profile(self, batch_id: str, actor_user_id: str, expected_version: int, profile: dict[str, Any], global_access: bool = False) -> QuarantineBatch:
        updated = self.transition(batch_id, actor_user_id, expected_version, "profiled", global_access)
        self._save_json("column_profiles", "profile_json", batch_id, profile)
        return updated

    def profile_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]:
        self.get_batch(batch_id, actor_user_id, global_access)
        return self._load_json("column_profiles", "profile_json", batch_id)

    def save_contract(self, batch_id: str, actor_user_id: str, expected_version: int, contract: dict[str, Any], global_access: bool = False) -> QuarantineBatch:
        updated = self.transition(batch_id, actor_user_id, expected_version, "mapping_proposed", global_access)
        self._save_json("mapping_contracts", "contract_json", batch_id, contract)
        return updated

    def contract_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]:
        self.get_batch(batch_id, actor_user_id, global_access)
        return self._load_json("mapping_contracts", "contract_json", batch_id)

    def save_validation(self, batch_id: str, actor_user_id: str, expected_version: int, report: dict[str, Any], global_access: bool = False) -> QuarantineBatch:
        target = "validated" if bool(report.get("valid")) else "validation_failed"
        updated = self.transition(batch_id, actor_user_id, expected_version, target, global_access)
        self._save_json("validation_runs", "report_json", batch_id, report)
        return updated

    def validation_for(self, batch_id: str, actor_user_id: str, global_access: bool = False) -> dict[str, Any]:
        self.get_batch(batch_id, actor_user_id, global_access)
        return self._load_json("validation_runs", "report_json", batch_id)

