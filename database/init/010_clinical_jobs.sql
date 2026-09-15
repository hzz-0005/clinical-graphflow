\set ON_ERROR_STOP on

CREATE TABLE IF NOT EXISTS enterprise.clinical_investigation_jobs (
  job_id uuid PRIMARY KEY,
  owner_user_id text NOT NULL,
  request_json jsonb NOT NULL,
  status text NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
  investigation_id uuid REFERENCES enterprise.investigations(investigation_id),
  error_code text,
  cancel_requested boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  version integer NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_clinical_jobs_owner_created ON enterprise.clinical_investigation_jobs(owner_user_id, created_at DESC);

