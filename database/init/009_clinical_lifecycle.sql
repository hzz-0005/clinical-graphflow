\set ON_ERROR_STOP on

ALTER TABLE clinical_ingestion.import_batches ADD COLUMN IF NOT EXISTS withdrawn_at timestamptz;
ALTER TABLE clinical_ingestion.import_batches ADD COLUMN IF NOT EXISTS withdrawn_by text;
ALTER TABLE clinical_ingestion.import_batches ADD COLUMN IF NOT EXISTS withdrawal_reason text;
ALTER TABLE clinical_ingestion.import_batches DROP CONSTRAINT IF EXISTS import_batches_status_check;
ALTER TABLE clinical_ingestion.import_batches ADD CONSTRAINT import_batches_status_check CHECK (status IN ('committed', 'published', 'withdrawn'));
ALTER TABLE clinical_ingestion.import_batches DROP CONSTRAINT IF EXISTS import_batches_publication_consistency;
ALTER TABLE clinical_ingestion.import_batches ADD CONSTRAINT import_batches_publication_consistency CHECK (
    (status = 'committed' AND published_at IS NULL AND published_by IS NULL AND withdrawn_at IS NULL)
    OR (status = 'published' AND published_at IS NOT NULL AND published_by IS NOT NULL AND withdrawn_at IS NULL)
    OR (status = 'withdrawn' AND published_at IS NOT NULL AND published_by IS NOT NULL AND withdrawn_at IS NOT NULL AND withdrawn_by IS NOT NULL AND length(trim(withdrawal_reason)) >= 5)
);

