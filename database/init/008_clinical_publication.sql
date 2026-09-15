\set ON_ERROR_STOP on

ALTER TABLE clinical_ingestion.import_batches DROP CONSTRAINT IF EXISTS import_batches_status_check;
-- Keep the constraint forward-compatible when this migration is replayed after
-- 009 has introduced the withdrawn lifecycle state.
ALTER TABLE clinical_ingestion.import_batches ADD CONSTRAINT import_batches_status_check CHECK (status IN ('committed', 'published', 'withdrawn'));
ALTER TABLE clinical_ingestion.import_batches ADD COLUMN IF NOT EXISTS published_at timestamptz;
ALTER TABLE clinical_ingestion.import_batches ADD COLUMN IF NOT EXISTS published_by text;
ALTER TABLE clinical_ingestion.import_batches DROP CONSTRAINT IF EXISTS import_batches_publication_consistency;
ALTER TABLE clinical_ingestion.import_batches ADD CONSTRAINT import_batches_publication_consistency CHECK (
    (status = 'committed' AND published_at IS NULL AND published_by IS NULL)
    OR (status = 'published' AND published_at IS NOT NULL AND published_by IS NOT NULL)
) NOT VALID;

