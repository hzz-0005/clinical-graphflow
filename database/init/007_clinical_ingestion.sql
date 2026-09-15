\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS clinical_ingestion;

CREATE TABLE IF NOT EXISTS clinical_ingestion.import_batches (
    batch_id text PRIMARY KEY,
    actor_user_id text NOT NULL,
    status text NOT NULL CHECK (status IN ('committed')),
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    trial_ids text[] NOT NULL,
    record_count integer NOT NULL CHECK (record_count >= 0),
    quality jsonb NOT NULL,
    committed_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS clinical_ingestion.canonical_records (
    batch_id text NOT NULL REFERENCES clinical_ingestion.import_batches(batch_id),
    participant_key text NOT NULL CHECK (participant_key ~ '^[a-f0-9]{64}$'),
    trial_id text NOT NULL,
    site_id text NOT NULL,
    arm text NOT NULL CHECK (arm IN ('control', 'treatment')),
    region text,
    intention_to_treat boolean NOT NULL,
    safety_population boolean NOT NULL,
    per_protocol boolean NOT NULL,
    baseline_value numeric,
    week12_value numeric,
    week12_improvement numeric,
    PRIMARY KEY (batch_id, participant_key)
);

CREATE INDEX IF NOT EXISTS ix_clinical_import_batches_committed ON clinical_ingestion.import_batches(committed_at DESC);

