from __future__ import annotations

import json

import psycopg
from psycopg.types.json import Jsonb

from app.clinical.cdisc import CanonicalClinicalRecord
from app.clinical.ingestion import CdiscImportBatch
from app.clinical.data_publication import PublishedDomainRecord


class PostgresCdiscImportRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    def save(self, batch: CdiscImportBatch, records: tuple[CanonicalClinicalRecord, ...]) -> CdiscImportBatch:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO clinical_ingestion.import_batches
                    (batch_id, actor_user_id, status, content_hash, trial_ids, record_count, quality, committed_at,
                     published_at, published_by, withdrawn_at, withdrawn_by, withdrawal_reason)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (batch.batch_id, batch.actor_user_id, batch.status, batch.content_hash, list(batch.trial_ids), batch.record_count, Jsonb(batch.quality.model_dump(mode="json")), batch.committed_at, batch.published_at, batch.published_by, batch.withdrawn_at, batch.withdrawn_by, batch.withdrawal_reason),
                )
                cursor.executemany(
                    """INSERT INTO clinical_ingestion.canonical_records
                    (batch_id, participant_key, trial_id, site_id, arm, region, intention_to_treat,
                     safety_population, per_protocol, baseline_value, week12_value, week12_improvement)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(batch.batch_id, r.participant_key, r.trial_id, r.site_id, r.arm, r.region, r.intention_to_treat, r.safety_population, r.per_protocol, r.baseline_value, r.week12_value, r.week12_improvement) for r in records],
                )
        return batch

    def replace(self, batch: CdiscImportBatch, records: tuple[CanonicalClinicalRecord, ...]) -> CdiscImportBatch:
        """Idempotent projection used when a governed batch is (re)published.

        ``source_batch_id`` in the analytics marts is the ingestion batch id, so a republish of the
        same governed batch must not duplicate canonical rows.
        """

        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM clinical_ingestion.canonical_records WHERE batch_id=%s", (batch.batch_id,))
                cursor.execute("DELETE FROM clinical_ingestion.import_batches WHERE batch_id=%s", (batch.batch_id,))
        return self.save(batch, records)

    def remove_batch(self, batch_id: str) -> None:
        """Withdraw the analytics projection so a withdrawn governed batch stops feeding the marts."""

        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM clinical_ingestion.domain_records WHERE batch_id=%s", (batch_id,))
                cursor.execute("DELETE FROM clinical_ingestion.canonical_records WHERE batch_id=%s", (batch_id,))
                cursor.execute("DELETE FROM clinical_ingestion.import_batches WHERE batch_id=%s", (batch_id,))

    def replace_domain_records(
        self, batch_id: str, records: tuple[PublishedDomainRecord, ...]
    ) -> None:
        """Replace validated plugin-domain rows for one governed publication version."""

        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM clinical_ingestion.domain_records WHERE batch_id=%s",
                    (batch_id,),
                )
                cursor.executemany(
                    """INSERT INTO clinical_ingestion.domain_records
                    (batch_id,domain_name,domain_version,row_key,source_filename,payload_json)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    [
                        (
                            batch_id,
                            row.domain_name,
                            row.domain_version,
                            row.row_key,
                            row.source_filename,
                            Jsonb(row.payload),
                        )
                        for row in records
                    ],
                )

    def list_batches(self) -> list[CdiscImportBatch]:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT batch_id,actor_user_id,status,content_hash,trial_ids,record_count,quality,committed_at,published_at,published_by,withdrawn_at,withdrawn_by,withdrawal_reason FROM clinical_ingestion.import_batches WHERE quality ? 'valid' ORDER BY committed_at DESC")
                return [self._batch(row) for row in cursor.fetchall()]

    @staticmethod
    def _batch(row) -> CdiscImportBatch:
        return CdiscImportBatch(batch_id=row[0], actor_user_id=row[1], status=row[2], content_hash=row[3], trial_ids=tuple(row[4]), record_count=row[5], quality=row[6] if isinstance(row[6], dict) else json.loads(row[6]), committed_at=row[7], published_at=row[8], published_by=row[9], withdrawn_at=row[10], withdrawn_by=row[11], withdrawal_reason=row[12])

    def get_batch(self, batch_id: str) -> CdiscImportBatch:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT batch_id,actor_user_id,status,content_hash,trial_ids,record_count,quality,committed_at,published_at,published_by,withdrawn_at,withdrawn_by,withdrawal_reason FROM clinical_ingestion.import_batches WHERE batch_id=%s", (batch_id,))
                row = cursor.fetchone()
                if row is None:
                    raise KeyError(batch_id)
                return self._batch(row)

    def records_for(self, batch_id: str) -> tuple[CanonicalClinicalRecord, ...]:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT participant_key,trial_id,site_id,arm,region,intention_to_treat,safety_population,per_protocol,baseline_value,week12_value,week12_improvement FROM clinical_ingestion.canonical_records WHERE batch_id=%s ORDER BY participant_key", (batch_id,))
                return tuple(CanonicalClinicalRecord(participant_key=r[0], trial_id=r[1], site_id=r[2], arm=r[3], region=r[4], intention_to_treat=r[5], safety_population=r[6], per_protocol=r[7], baseline_value=r[8], week12_value=r[9], week12_improvement=r[10]) for r in cursor.fetchall())

    def publish(self, batch_id: str, actor_user_id: str, published_at) -> CdiscImportBatch:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE clinical_ingestion.import_batches SET status='published',published_by=%s,published_at=%s WHERE batch_id=%s AND status='committed'", (actor_user_id, published_at, batch_id))
        return self.get_batch(batch_id)

    def withdraw(self, batch_id: str, actor_user_id: str, withdrawn_at, reason: str) -> CdiscImportBatch:
        with psycopg.connect(self._database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE clinical_ingestion.import_batches SET status='withdrawn',withdrawn_by=%s,withdrawn_at=%s,withdrawal_reason=%s WHERE batch_id=%s AND status='published'", (actor_user_id, withdrawn_at, reason, batch_id))
                if cursor.rowcount != 1:
                    raise ValueError("only a published batch can be withdrawn")
        return self.get_batch(batch_id)

