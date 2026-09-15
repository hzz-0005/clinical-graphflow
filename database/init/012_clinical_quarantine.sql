\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS clinical_quarantine;

CREATE TABLE IF NOT EXISTS clinical_quarantine.import_batches (
    batch_id uuid PRIMARY KEY,
    owner_user_id text NOT NULL,
    status text NOT NULL CHECK (status IN (
        'parsed','parse_failed','profiled','profiling_failed','mapping_proposed',
        'validated','validation_failed','approved','published'
    )),
    visibility_level text NOT NULL CHECK (visibility_level IN ('L0','L1','L2','L3','L4')),
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    file_count integer NOT NULL CHECK (file_count > 0),
    row_count integer NOT NULL CHECK (row_count >= 0),
    version integer NOT NULL CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clinical_quarantine.files (
    file_id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES clinical_quarantine.import_batches(batch_id) ON DELETE CASCADE,
    filename text NOT NULL,
    format text NOT NULL,
    adapter_version text NOT NULL,
    columns_json jsonb NOT NULL,
    row_count integer NOT NULL CHECK (row_count >= 0),
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    UNIQUE (batch_id, filename)
);

CREATE TABLE IF NOT EXISTS clinical_quarantine.rows (
    batch_id uuid NOT NULL REFERENCES clinical_quarantine.import_batches(batch_id) ON DELETE CASCADE,
    file_id uuid NOT NULL REFERENCES clinical_quarantine.files(file_id) ON DELETE CASCADE,
    row_number integer NOT NULL CHECK (row_number > 0),
    payload_json jsonb NOT NULL,
    PRIMARY KEY (batch_id, file_id, row_number)
);

CREATE TABLE IF NOT EXISTS clinical_quarantine.column_profiles (
    batch_id uuid PRIMARY KEY REFERENCES clinical_quarantine.import_batches(batch_id) ON DELETE CASCADE,
    profile_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clinical_quarantine.mapping_contracts (
    batch_id uuid PRIMARY KEY REFERENCES clinical_quarantine.import_batches(batch_id) ON DELETE CASCADE,
    contract_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clinical_quarantine.validation_runs (
    batch_id uuid PRIMARY KEY REFERENCES clinical_quarantine.import_batches(batch_id) ON DELETE CASCADE,
    report_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_clinical_quarantine_owner_created
    ON clinical_quarantine.import_batches(owner_user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_clinical_quarantine_rows_file
    ON clinical_quarantine.rows(file_id, row_number);

