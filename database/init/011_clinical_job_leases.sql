\set ON_ERROR_STOP on

ALTER TABLE enterprise.clinical_investigation_jobs ADD COLUMN IF NOT EXISTS worker_id text;
ALTER TABLE enterprise.clinical_investigation_jobs ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz;
ALTER TABLE enterprise.clinical_investigation_jobs ADD COLUMN IF NOT EXISTS attempt integer NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_clinical_jobs_claim ON enterprise.clinical_investigation_jobs(status, lease_expires_at, created_at);

