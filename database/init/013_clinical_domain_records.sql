\set ON_ERROR_STOP on

-- Registered plugin-domain landing store / 已注册插件数据域发布区。
-- JSONB preserves domain-specific shape; domain_name + domain_version bind every row to an
-- audited registry contract. This table is not exposed to readonly free-SQL tools.
CREATE TABLE IF NOT EXISTS clinical_ingestion.domain_records (
    batch_id text NOT NULL REFERENCES clinical_ingestion.import_batches(batch_id) ON DELETE CASCADE,
    domain_name text NOT NULL CHECK (domain_name ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    domain_version text NOT NULL,
    row_key text NOT NULL CHECK (row_key ~ '^[a-f0-9]{64}$'),
    source_filename text NOT NULL,
    payload_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, domain_name, row_key)
);

CREATE INDEX IF NOT EXISTS ix_clinical_domain_records_domain_batch
    ON clinical_ingestion.domain_records(domain_name, batch_id);
CREATE INDEX IF NOT EXISTS ix_clinical_domain_records_payload
    ON clinical_ingestion.domain_records USING gin(payload_json);

