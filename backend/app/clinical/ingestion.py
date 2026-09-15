from __future__ import annotations

import csv
import hashlib
import io
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.clinical.cdisc import CanonicalClinicalRecord, CdiscClinicalAdapter


class CdiscImportManifest(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_format: str = "CDISC SDTM DM + ADaM ADSL/ADEFF CSV"
    trial_ids: tuple[str, ...]
    record_count: int
    records: tuple[CanonicalClinicalRecord, ...]


class CdiscCsvIngestionService:
    required_files = ("dm.csv", "adsl.csv", "adeff.csv")

    def __init__(self, pseudonym_salt: str, max_file_bytes: int = 10_000_000) -> None:
        self._adapter = CdiscClinicalAdapter(pseudonym_salt)
        self._max_file_bytes = max_file_bytes

    def read_directory(self, directory: Path) -> CdiscImportManifest:
        root = directory.resolve(strict=True)
        rows = {}
        for filename in self.required_files:
            path = (root / filename).resolve()
            if path.parent != root or not path.is_file():
                raise ValueError(f"required CDISC file is missing: {filename}")
            if path.stat().st_size > self._max_file_bytes:
                raise ValueError(f"CDISC file exceeds size limit: {filename}")
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows[filename] = list(csv.DictReader(handle))
        records = self._adapter.map(rows["dm.csv"], rows["adsl.csv"], rows["adeff.csv"])
        return CdiscImportManifest(trial_ids=tuple(sorted({item.trial_id for item in records})), record_count=len(records), records=records)


STANDARD_FIELDS = {
    "dm": ("STUDYID", "USUBJID", "SITEID", "ARM"),
    "adsl": ("STUDYID", "USUBJID", "REGION1", "ITTFL", "SAFFL", "PPROTFL"),
    "adeff": ("STUDYID", "USUBJID", "PARAMCD", "AVISIT", "BASE", "AVAL", "CHG"),
}


def domain_row_counts(rows: dict[str, object] | None) -> dict[str, int]:
    """Normalise per-domain row counts onto the lowercase CDISC domain keys the API promises.

    The V5 CDISC import path and the V8 publication bridge record the same fact with different
    key casing (``adeff`` vs ``ADEFF``). An in-memory dict join does not care, but the JSON
    round-trip through PostgreSQL preserves whatever casing was written, so a reader that only
    looks for lowercase keys silently reports "no coverage" for every bridge-published version.
    Normalising on read keeps the published contract stable while making both writers visible to
    the same consumers.
    """

    source = rows or {}
    return {
        domain: int(source.get(domain) or source.get(domain.upper()) or 0)
        for domain in STANDARD_FIELDS
    }


class DomainFieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_mapping(cls, value):
        return {"root": value} if isinstance(value, dict) and "root" not in value else value

    @model_validator(mode="after")
    def unique_sources(self):
        values = [item.strip() for item in self.root.values()]
        if len(values) != len(set(values)):
            raise ValueError("source columns must be unique")
        return self


class CdiscFieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dm: DomainFieldMapping = Field(default_factory=DomainFieldMapping)
    adsl: DomainFieldMapping = Field(default_factory=DomainFieldMapping)
    adeff: DomainFieldMapping = Field(default_factory=DomainFieldMapping)

    def for_domain(self, domain: str) -> dict[str, str]:
        supplied = getattr(self, domain).root
        return {field: supplied.get(field, field) for field in STANDARD_FIELDS[domain]}


class CdiscQualityReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    valid: bool
    rows: dict[str, int]
    completeness_percent: float
    trial_ids: tuple[str, ...]
    site_ids: tuple[str, ...]
    arm_counts: dict[str, int]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class CdiscInspection(BaseModel):
    model_config = ConfigDict(frozen=True)
    quality: CdiscQualityReport
    preview: tuple[CanonicalClinicalRecord, ...]
    records: tuple[CanonicalClinicalRecord, ...] = Field(exclude=True)
    content_hash: str


class CdiscBundleInspector:
    def __init__(self, pseudonym_salt: str, max_file_bytes: int = 10_000_000, preview_rows: int = 20) -> None:
        self._adapter = CdiscClinicalAdapter(pseudonym_salt)
        self._max_file_bytes = max_file_bytes
        self._preview_rows = preview_rows

    def inspect(self, dm: bytes, adsl: bytes, adeff: bytes, mapping: CdiscFieldMapping | None = None) -> CdiscInspection:
        mapping = mapping or CdiscFieldMapping()
        payloads = {"dm": dm, "adsl": adsl, "adeff": adeff}
        parsed = {domain: self._parse(domain, payload, mapping.for_domain(domain)) for domain, payload in payloads.items()}
        records = self._adapter.map(parsed["dm"], parsed["adsl"], parsed["adeff"])
        values = [value for rows in parsed.values() for row in rows for value in row.values()]
        completeness = 100.0 if not values else round(sum(value not in (None, "") for value in values) / len(values) * 100, 1)
        warnings = []
        if not records:
            warnings.append("No canonical participant records were produced（未生成规范参与者记录）")
        quality = CdiscQualityReport(
            valid=True,
            rows={name: len(rows) for name, rows in parsed.items()},
            completeness_percent=completeness,
            trial_ids=tuple(sorted({record.trial_id for record in records})),
            site_ids=tuple(sorted({record.site_id for record in records})),
            arm_counts=dict(sorted(Counter(record.arm for record in records).items())),
            warnings=tuple(warnings),
        )
        digest = hashlib.sha256(b"\0".join(payloads[name] for name in ("dm", "adsl", "adeff"))).hexdigest()
        return CdiscInspection(quality=quality, preview=records[: self._preview_rows], records=records, content_hash=digest)

    def _parse(self, domain: str, payload: bytes, mapping: dict[str, str]) -> list[dict[str, str]]:
        if len(payload) > self._max_file_bytes:
            raise ValueError(f"{domain.upper()} file exceeds 10 MB limit")
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{domain.upper()} must be UTF-8 CSV") from exc
        reader = csv.DictReader(io.StringIO(text, newline=""))
        headers = set(reader.fieldnames or ())
        for standard, source in mapping.items():
            if source not in headers:
                raise ValueError(f"{domain.upper()} source column '{source}' for {standard} is missing")
        return [{standard: (row.get(source) or "").strip() for standard, source in mapping.items()} for row in reader]


class CdiscImportBatch(BaseModel):
    model_config = ConfigDict(frozen=True)
    batch_id: str
    actor_user_id: str
    status: str = "committed"
    content_hash: str
    trial_ids: tuple[str, ...]
    record_count: int
    quality: CdiscQualityReport
    committed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    published_at: datetime | None = None
    published_by: str | None = None
    withdrawn_at: datetime | None = None
    withdrawn_by: str | None = None
    withdrawal_reason: str | None = None


class CdiscImportRepository(Protocol):
    def save(self, batch: CdiscImportBatch, records: tuple[CanonicalClinicalRecord, ...]) -> CdiscImportBatch: ...
    def list_batches(self) -> list[CdiscImportBatch]: ...
    def get_batch(self, batch_id: str) -> CdiscImportBatch: ...
    def records_for(self, batch_id: str) -> tuple[CanonicalClinicalRecord, ...]: ...
    def publish(self, batch_id: str, actor_user_id: str, published_at: datetime) -> CdiscImportBatch: ...
    def withdraw(self, batch_id: str, actor_user_id: str, withdrawn_at: datetime, reason: str) -> CdiscImportBatch: ...


class InMemoryCdiscImportRepository:
    def __init__(self) -> None:
        self._batches: list[CdiscImportBatch] = []
        self._records: dict[str, tuple[CanonicalClinicalRecord, ...]] = {}

    def save(self, batch: CdiscImportBatch, records: tuple[CanonicalClinicalRecord, ...]) -> CdiscImportBatch:
        self._batches.append(batch)
        self._records[batch.batch_id] = records
        return batch

    def list_batches(self) -> list[CdiscImportBatch]:
        return list(reversed(self._batches))

    def records_for(self, batch_id: str) -> tuple[CanonicalClinicalRecord, ...]:
        return self._records[batch_id]

    def get_batch(self, batch_id: str) -> CdiscImportBatch:
        return next(item for item in self._batches if item.batch_id == batch_id)

    def publish(self, batch_id: str, actor_user_id: str, published_at: datetime) -> CdiscImportBatch:
        current = self.get_batch(batch_id)
        updated = current.model_copy(update={"status": "published", "published_by": actor_user_id, "published_at": published_at})
        self._batches[self._batches.index(current)] = updated
        return updated

    def withdraw(self, batch_id: str, actor_user_id: str, withdrawn_at: datetime, reason: str) -> CdiscImportBatch:
        current = self.get_batch(batch_id)
        updated = current.model_copy(update={"status": "withdrawn", "withdrawn_by": actor_user_id, "withdrawn_at": withdrawn_at, "withdrawal_reason": reason})
        self._batches[self._batches.index(current)] = updated
        return updated


class CdiscPreview(BaseModel):
    model_config = ConfigDict(frozen=True)
    token: str
    quality: CdiscQualityReport
    preview: tuple[CanonicalClinicalRecord, ...]
    expires_at: datetime


class _PendingPreview(BaseModel):
    inspection: CdiscInspection
    actor_user_id: str
    expires_at: datetime
    used: bool = False


class CdiscImportCoordinator:
    def __init__(self, pseudonym_salt: str, repository: CdiscImportRepository, token_ttl: timedelta = timedelta(minutes=30)) -> None:
        self._inspector = CdiscBundleInspector(pseudonym_salt)
        self._repository = repository
        self._token_ttl = token_ttl
        self._pending: dict[str, _PendingPreview] = {}

    def preview(self, dm: bytes, adsl: bytes, adeff: bytes, mapping: CdiscFieldMapping, actor_user_id: str) -> CdiscPreview:
        inspection = self._inspector.inspect(dm, adsl, adeff, mapping)
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + self._token_ttl
        self._pending[token] = _PendingPreview(inspection=inspection, actor_user_id=actor_user_id, expires_at=expires_at)
        return CdiscPreview(token=token, quality=inspection.quality, preview=inspection.preview, expires_at=expires_at)

    def commit(self, token: str, actor_user_id: str) -> CdiscImportBatch:
        pending = self._pending.get(token)
        if pending is None:
            raise ValueError("preview token is invalid")
        if pending.used:
            raise ValueError("preview token was already used")
        if datetime.now(timezone.utc) >= pending.expires_at:
            raise ValueError("preview token expired")
        pending.used = True
        inspection = pending.inspection
        batch = CdiscImportBatch(
            batch_id=secrets.token_hex(16), actor_user_id=actor_user_id,
            content_hash=inspection.content_hash, trial_ids=inspection.quality.trial_ids,
            record_count=len(inspection.records), quality=inspection.quality,
        )
        return self._repository.save(batch, inspection.records)

    def list_batches(self) -> list[CdiscImportBatch]:
        return self._repository.list_batches()


class CdiscPublishAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)
    batch_id: str
    eligible: bool
    passed_rules: tuple[str, ...]
    failed_rules: tuple[str, ...]
    domain_coverage: dict[str, bool]
    arm_counts: dict[str, int]
    record_count: int


class CdiscBatchComparison(BaseModel):
    model_config = ConfigDict(frozen=True)
    left_batch_id: str
    right_batch_id: str
    record_count_delta: int
    completeness_percent_delta: float
    arm_count_deltas: dict[str, int]
    added_site_ids: tuple[str, ...]
    removed_site_ids: tuple[str, ...]
    content_changed: bool


class CdiscPublicationService:
    def __init__(self, repository: CdiscImportRepository, minimum_total: int = 20, minimum_per_arm: int = 10) -> None:
        self._repository = repository
        self._minimum_total = minimum_total
        self._minimum_per_arm = minimum_per_arm

    def assess(self, batch_id: str) -> CdiscPublishAssessment:
        batch = self._repository.get_batch(batch_id)
        counts = domain_row_counts(batch.quality.rows)
        domains = {name: counts[name] > 0 for name in STANDARD_FIELDS}
        checks = {
            "required_domain_coverage": all(domains.values()),
            "minimum_total_sample": batch.record_count >= self._minimum_total,
            "minimum_per_arm_sample": all(batch.quality.arm_counts.get(arm, 0) >= self._minimum_per_arm for arm in ("control", "treatment")),
            "quality_validation": batch.quality.valid and not batch.quality.errors,
        }
        return CdiscPublishAssessment(
            batch_id=batch_id, eligible=all(checks.values()),
            passed_rules=tuple(name for name, passed in checks.items() if passed),
            failed_rules=tuple(name for name, passed in checks.items() if not passed),
            domain_coverage=domains, arm_counts=batch.quality.arm_counts, record_count=batch.record_count,
        )

    def publish(self, batch_id: str, actor_user_id: str) -> CdiscImportBatch:
        batch = self._repository.get_batch(batch_id)
        if batch.status == "published":
            return batch
        if batch.status == "withdrawn":
            raise ValueError("withdrawn batch cannot be republished")
        assessment = self.assess(batch_id)
        if not assessment.eligible:
            raise ValueError("publish gate failed: " + ", ".join(assessment.failed_rules))
        return self._repository.publish(batch_id, actor_user_id, datetime.now(timezone.utc))

    def withdraw(self, batch_id: str, actor_user_id: str, reason: str) -> CdiscImportBatch:
        batch = self._repository.get_batch(batch_id)
        reason = reason.strip()
        if batch.status != "published":
            raise ValueError("only a published batch can be withdrawn")
        if len(reason) < 5:
            raise ValueError("withdrawal reason is required")
        return self._repository.withdraw(batch_id, actor_user_id, datetime.now(timezone.utc), reason)

    def compare(self, left_batch_id: str, right_batch_id: str) -> CdiscBatchComparison:
        left = self._repository.get_batch(left_batch_id)
        right = self._repository.get_batch(right_batch_id)
        arms = sorted(set(left.quality.arm_counts) | set(right.quality.arm_counts))
        left_sites, right_sites = set(left.quality.site_ids), set(right.quality.site_ids)
        return CdiscBatchComparison(
            left_batch_id=left_batch_id,
            right_batch_id=right_batch_id,
            record_count_delta=right.record_count - left.record_count,
            completeness_percent_delta=round(right.quality.completeness_percent - left.quality.completeness_percent, 1),
            arm_count_deltas={arm: right.quality.arm_counts.get(arm, 0) - left.quality.arm_counts.get(arm, 0) for arm in arms},
            added_site_ids=tuple(sorted(right_sites - left_sites)),
            removed_site_ids=tuple(sorted(left_sites - right_sites)),
            content_changed=left.content_hash != right.content_hash,
        )

