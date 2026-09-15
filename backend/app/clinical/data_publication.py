from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.clinical.cdisc import CanonicalClinicalRecord
from app.clinical.domain_registry import DomainRegistry
from app.clinical.ingestion import CdiscImportBatch, CdiscQualityReport
from app.clinical.tabular import TabularFile
from app.clinical.transformation import _transform
from app.clinical.understanding_models import MappingContract

#: Participant-level domains that the canonical ingestion contract understands.
PROJECTED_DOMAINS = ("DM", "ADSL", "ADEFF")
PARTICIPANT_IDENTIFIER = "USUBJID"
TRIAL_IDENTIFIER = "STUDYID"
SUPPORTED_ARMS = {"control": "control", "treatment": "treatment"}


class IngestionProjection(Protocol):
    def save(self, batch: CdiscImportBatch, records: tuple[CanonicalClinicalRecord, ...]) -> CdiscImportBatch: ...
    def replace(self, batch: CdiscImportBatch, records: tuple[CanonicalClinicalRecord, ...]) -> CdiscImportBatch: ...
    def remove_batch(self, batch_id: str) -> None: ...
    def replace_domain_records(self, batch_id: str, records: tuple["PublishedDomainRecord", ...]) -> None: ...


class PublishedDomainRecord(BaseModel):
    """One governed row in a registered plugin domain.

    The generic store is deliberately not an EAV analytics model. It preserves a validated,
    versioned canonical payload so a domain-specific mart/plugin can consume it later without
    forcing drug, device, procedure and EHR records into one fixed participant schema.
    """

    model_config = ConfigDict(frozen=True)
    domain_name: str
    domain_version: str
    row_key: str
    source_filename: str
    payload: dict[str, Any]


def _number(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value).strip()


def _flag(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in {"Y", "YES", "TRUE"}


class QuarantinePublicationBridge:
    """Project an approved V8 quarantine batch into the governed ingestion tables.

    Without this bridge the V8 lifecycle would end inside ``clinical_quarantine``: the analytics
    marts read ``clinical_ingestion``, so a batch that was imported, profiled, mapped, validated,
    approved and published through V8 would be invisible to the V8 dynamic runtime. The bridge makes
    ``published_batch_id`` mean the same thing on both sides: one governed data version, bound by
    ``source_batch_id`` in every batch-bound mart.

    It never invents data. Rows that cannot be projected (unsupported arm, missing identifiers,
    custom candidate domains) raise, so a successful publish always means the published version is
    actually queryable.
    """

    def __init__(self, quarantine_repository, domain_registry: DomainRegistry, ingestion_repository: IngestionProjection, pseudonym_salt: str) -> None:
        if not pseudonym_salt:
            raise ValueError("pseudonym salt is required")
        self._quarantine = quarantine_repository
        self._domains = domain_registry
        self._ingestion = ingestion_repository
        self._salt = pseudonym_salt

    def project(self, batch_id: str, actor_user_id: str, global_access: bool = True) -> dict[str, Any]:
        batch = self._quarantine.get_batch(batch_id, actor_user_id, global_access)
        contract = MappingContract.model_validate(self._quarantine.contract_for(batch_id, actor_user_id, global_access))
        stored = self._quarantine.files_for(batch_id, actor_user_id, global_access)
        files = {
            item.file_id: TabularFile(
                filename=item.filename,
                format=item.format,
                columns=item.columns,
                row_count=item.row_count,
                rows=self._quarantine.rows_for(batch_id, item.file_id, actor_user_id, global_access),
                content_hash=item.content_hash,
            )
            for item in stored
        }
        generic_records, domain_counts = self._registered_domain_records(files, contract)
        projectable = [
            mapping
            for mapping in contract.files
            if mapping.target_domain.upper() in PROJECTED_DOMAINS and mapping.status != "custom_candidate"
        ]
        if not projectable:
            # Registered domains are publishable even when no participant-efficacy adapter exists.
            # They enter the governed generic store and remain explicitly non-bindable to the
            # current Week-12 runtime until a domain-specific mart/plugin is installed.
            projected = self._ingestion.replace(
                self._batch(batch, (), domain_counts, generic_records), ()
            )
            self._ingestion.replace_domain_records(batch_id, generic_records)
            return {
                "batch_id": projected.batch_id,
                "record_count": 0,
                "bindable": False,
                "reason": "数据已进入受治理数据域存储；当前尚无匹配该数据域的专用分析插件",
                "trial_ids": list(projected.trial_ids),
                "site_ids": [],
                "arm_counts": {},
                "domains": sorted(domain_counts),
                "domain_row_counts": domain_counts,
            }
        records, counts = self._canonical_records(files, contract)
        projected = self._ingestion.replace(
            self._batch(batch, records, counts, generic_records), records
        )
        self._ingestion.replace_domain_records(batch_id, generic_records)
        return {
            "batch_id": projected.batch_id,
            "record_count": projected.record_count,
            "bindable": True,
            "trial_ids": list(projected.trial_ids),
            "site_ids": list(projected.quality.site_ids),
            "arm_counts": projected.quality.arm_counts,
            "domains": sorted(domain_counts),
            "domain_row_counts": domain_counts,
        }

    def withdraw(self, batch_id: str) -> None:
        self._ingestion.remove_batch(batch_id)

    def _batch(
        self,
        batch,
        records: tuple[CanonicalClinicalRecord, ...],
        counts: dict[str, int],
        domain_records: tuple[PublishedDomainRecord, ...],
    ) -> CdiscImportBatch:
        now = datetime.now(timezone.utc)
        trial_ids = tuple(
            sorted(
                {record.trial_id for record in records}
                | {
                    str(record.payload["STUDY_ID"])
                    for record in domain_records
                    if record.payload.get("STUDY_ID")
                }
                | {
                    str(record.payload["STUDYID"])
                    for record in domain_records
                    if record.payload.get("STUDYID")
                }
            )
        )
        site_ids = tuple(sorted({record.site_id for record in records}))
        arm_counts = {arm: sum(1 for record in records if record.arm == arm) for arm in SUPPORTED_ARMS}
        return CdiscImportBatch(
            batch_id=batch.batch_id,
            actor_user_id=batch.owner_user_id,
            status="published",
            content_hash=batch.content_hash,
            trial_ids=trial_ids,
            record_count=len(records),
            quality=CdiscQualityReport(
                valid=True,
                rows=dict(counts),
                completeness_percent=100.0,
                trial_ids=trial_ids,
                site_ids=site_ids,
                arm_counts=arm_counts,
            ),
            committed_at=now,
            published_at=now,
            published_by=batch.owner_user_id,
        )

    def _registered_domain_records(
        self, files: dict[str, TabularFile], contract: MappingContract
    ) -> tuple[tuple[PublishedDomainRecord, ...], dict[str, int]]:
        records: list[PublishedDomainRecord] = []
        counts: dict[str, int] = {}
        for mapping in contract.files:
            if mapping.target_domain.lower() == "custom_candidate" or mapping.status == "custom_candidate":
                raise ValueError("自定义数据域需要管理员单独审批后才能发布到分析层")
            domain = self._domains.get(mapping.target_domain, mapping.target_version)
            source = files.get(mapping.file_id)
            if source is None:
                raise ValueError(f"映射引用了不存在的文件：{mapping.file_id}")
            for index, row in enumerate(source.rows, 1):
                payload: dict[str, Any] = {}
                for field in mapping.fields:
                    try:
                        payload[field.target] = _transform(row.get(field.source), field.transform)
                    except (ValueError, TypeError, OverflowError) as exc:
                        raise ValueError(
                            f"{source.filename} 第 {index} 行无法执行 {field.transform} 转换"
                        ) from exc
                key_payload = {
                    key: payload.get(key)
                    for key in domain.keys
                }
                row_key = hashlib.sha256(
                    json.dumps(
                        [domain.name, domain.version, source.filename, index, key_payload],
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ).encode("utf-8")
                ).hexdigest()
                records.append(
                    PublishedDomainRecord(
                        domain_name=domain.name,
                        domain_version=domain.version,
                        row_key=row_key,
                        source_filename=source.filename,
                        payload=payload,
                    )
                )
                counts[domain.name] = counts.get(domain.name, 0) + 1
        return tuple(records), dict(sorted(counts.items()))

    def _canonical_records(
        self, files: dict[str, TabularFile], contract: MappingContract
    ) -> tuple[tuple[CanonicalClinicalRecord, ...], dict[str, int]]:
        by_domain: dict[str, dict[str, dict[str, Any]]] = {}
        for mapping in contract.files:
            domain = mapping.target_domain.upper()
            if domain == "CUSTOM_CANDIDATE" or mapping.status == "custom_candidate":
                raise ValueError("自定义数据域需要管理员单独审批后才能发布到分析层")
            if domain not in PROJECTED_DOMAINS:
                continue
            source = files.get(mapping.file_id)
            if source is None:
                raise ValueError(f"映射引用了不存在的文件：{mapping.file_id}")
            rows = by_domain.setdefault(domain, {})
            for index, row in enumerate(source.rows, 1):
                subject: str | None = None
                values: dict[str, Any] = {}
                for field in mapping.fields:
                    try:
                        value = _transform(row.get(field.source), field.transform)
                    except (ValueError, TypeError, OverflowError) as exc:
                        raise ValueError(f"{source.filename} 第 {index} 行无法执行 {field.transform} 转换") from exc
                    if field.target == PARTICIPANT_IDENTIFIER:
                        subject = _text(value)
                    else:
                        values[field.target] = value
                if not subject:
                    raise ValueError(f"{source.filename} 第 {index} 行缺少受试者标识，无法发布")
                rows.setdefault(subject, {}).update(values)

        dm, adsl, adeff = (by_domain.get(name, {}) for name in PROJECTED_DOMAINS)
        records: list[CanonicalClinicalRecord] = []
        for subject, fields in sorted(dm.items()):
            trial_id = _text(fields.get(TRIAL_IDENTIFIER))
            site_id = _text(fields.get("SITEID"))
            arm = str(fields.get("ARM") or "").strip().lower()
            if not trial_id or not site_id:
                raise ValueError("发布批次缺少 STUDYID 或 SITEID，无法绑定到分析层")
            if arm not in SUPPORTED_ARMS:
                raise ValueError(f"发布批次包含不支持的分组：{fields.get('ARM')}")
            baseline = adsl.get(subject, {})
            endpoint = adeff.get(subject, {})
            change = _number(endpoint.get("CHG"))
            records.append(
                CanonicalClinicalRecord(
                    participant_key=self._pseudonym(trial_id, subject),
                    trial_id=trial_id,
                    site_id=site_id,
                    arm=SUPPORTED_ARMS[arm],
                    region=_text(baseline.get("REGION1")),
                    intention_to_treat=_flag(baseline.get("ITTFL"), True),
                    safety_population=_flag(baseline.get("SAFFL"), True),
                    per_protocol=_flag(baseline.get("PPROTFL"), False),
                    baseline_value=_number(endpoint.get("BASE")),
                    week12_value=_number(endpoint.get("AVAL")),
                    week12_improvement=-change if change is not None else None,
                )
            )
        if not records:
            raise ValueError("发布批次没有产生任何可绑定的受试者记录")
        return tuple(records), {name: len(by_domain.get(name, {})) for name in PROJECTED_DOMAINS}

    def _pseudonym(self, trial_id: str, subject_id: str) -> str:
        return hashlib.sha256(f"{self._salt}:{trial_id}:{subject_id}".encode("utf-8")).hexdigest()

