\set ON_ERROR_STOP on

CREATE SCHEMA IF NOT EXISTS clinical_raw;

CREATE TABLE IF NOT EXISTS clinical_raw.clinical_trials (
    trial_id text PRIMARY KEY,
    protocol_version text NOT NULL,
    phase text NOT NULL,
    indication text NOT NULL,
    start_date date NOT NULL,
    planned_end_date date NOT NULL CHECK (planned_end_date >= start_date),
    primary_endpoint text NOT NULL,
    status text NOT NULL CHECK (status IN ('planned', 'active', 'completed', 'stopped'))
);

CREATE TABLE IF NOT EXISTS clinical_raw.trial_sites (
    site_id text PRIMARY KEY,
    trial_id text NOT NULL REFERENCES clinical_raw.clinical_trials(trial_id),
    site_name text NOT NULL,
    region text NOT NULL CHECK (region IN ('Asia', 'Europe', 'North America')),
    country text NOT NULL,
    opened_at date NOT NULL,
    closed_at date,
    CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

CREATE TABLE IF NOT EXISTS clinical_raw.participants (
    participant_id text PRIMARY KEY,
    trial_id text NOT NULL REFERENCES clinical_raw.clinical_trials(trial_id),
    site_id text NOT NULL REFERENCES clinical_raw.trial_sites(site_id),
    region text NOT NULL CHECK (region IN ('Asia', 'Europe', 'North America')),
    age_band text NOT NULL CHECK (age_band IN ('18-39', '40-59', '60-74', '75+')),
    sex text NOT NULL CHECK (sex IN ('female', 'male')),
    enrolled_at timestamptz NOT NULL,
    discontinued_at timestamptz,
    discontinuation_reason text,
    CHECK (discontinued_at IS NULL OR discontinued_at >= enrolled_at),
    CHECK ((discontinued_at IS NULL) = (discontinuation_reason IS NULL))
);

CREATE TABLE IF NOT EXISTS clinical_raw.randomization_assignments (
    participant_id text PRIMARY KEY REFERENCES clinical_raw.participants(participant_id),
    trial_id text NOT NULL REFERENCES clinical_raw.clinical_trials(trial_id),
    arm text NOT NULL CHECK (arm IN ('control', 'treatment')),
    randomized_at timestamptz NOT NULL,
    stratification_region text NOT NULL,
    randomization_block text NOT NULL
);

CREATE TABLE IF NOT EXISTS clinical_raw.baseline_assessments (
    assessment_id text PRIMARY KEY,
    participant_id text NOT NULL UNIQUE REFERENCES clinical_raw.participants(participant_id),
    assessment_date date NOT NULL,
    baseline_score numeric(8,2) NOT NULL,
    severity_band text NOT NULL CHECK (severity_band IN ('low', 'moderate', 'high')),
    disease_duration_months integer NOT NULL CHECK (disease_duration_months >= 0)
);

CREATE TABLE IF NOT EXISTS clinical_raw.outcome_assessments (
    assessment_id text PRIMARY KEY,
    participant_id text NOT NULL REFERENCES clinical_raw.participants(participant_id),
    visit_week integer NOT NULL CHECK (visit_week IN (4, 12)),
    scheduled_date date NOT NULL,
    assessed_at timestamptz,
    outcome_score numeric(8,2),
    assessment_status text NOT NULL CHECK (assessment_status IN ('completed', 'missing')),
    missing_reason text,
    UNIQUE (participant_id, visit_week),
    CHECK (
        (assessment_status = 'completed' AND assessed_at IS NOT NULL AND outcome_score IS NOT NULL AND missing_reason IS NULL)
        OR (assessment_status = 'missing' AND assessed_at IS NULL AND outcome_score IS NULL AND missing_reason IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS clinical_raw.treatment_exposures (
    exposure_id text PRIMARY KEY,
    participant_id text NOT NULL REFERENCES clinical_raw.participants(participant_id),
    dose_date date NOT NULL,
    planned_dose numeric(8,2) NOT NULL CHECK (planned_dose > 0),
    actual_dose numeric(8,2) NOT NULL CHECK (actual_dose >= 0 AND actual_dose <= planned_dose),
    lot_id text NOT NULL,
    adherence_status text NOT NULL CHECK (adherence_status IN ('taken', 'missed')),
    CHECK ((actual_dose > 0 AND adherence_status = 'taken') OR (actual_dose = 0 AND adherence_status = 'missed'))
);

CREATE TABLE IF NOT EXISTS clinical_raw.adverse_events (
    adverse_event_id text PRIMARY KEY,
    participant_id text NOT NULL REFERENCES clinical_raw.participants(participant_id),
    term_group text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('mild', 'moderate', 'severe')),
    serious boolean NOT NULL,
    relatedness text NOT NULL CHECK (relatedness IN ('unlikely', 'possible', 'probable')),
    started_at timestamptz NOT NULL,
    ended_at timestamptz,
    action_taken text NOT NULL,
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE TABLE IF NOT EXISTS clinical_raw.protocol_deviations (
    deviation_id text PRIMARY KEY,
    participant_id text NOT NULL REFERENCES clinical_raw.participants(participant_id),
    site_id text NOT NULL REFERENCES clinical_raw.trial_sites(site_id),
    category text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('minor', 'major', 'critical')),
    occurred_at timestamptz NOT NULL,
    detected_at timestamptz NOT NULL CHECK (detected_at >= occurred_at),
    description_code text NOT NULL
);

CREATE TABLE IF NOT EXISTS clinical_raw.participant_visits (
    visit_id text PRIMARY KEY,
    participant_id text NOT NULL REFERENCES clinical_raw.participants(participant_id),
    visit_week integer NOT NULL CHECK (visit_week IN (0, 4, 8, 12)),
    scheduled_at timestamptz NOT NULL,
    occurred_at timestamptz,
    visit_status text NOT NULL CHECK (visit_status IN ('completed', 'missed')),
    window_deviation_days integer,
    UNIQUE (participant_id, visit_week),
    CHECK (
        (visit_status = 'completed' AND occurred_at IS NOT NULL AND window_deviation_days IS NOT NULL)
        OR (visit_status = 'missed' AND occurred_at IS NULL AND window_deviation_days IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS clinical_raw.specimen_handling_events (
    handling_event_id text PRIMARY KEY,
    site_id text NOT NULL REFERENCES clinical_raw.trial_sites(site_id),
    lot_id text NOT NULL,
    event_type text NOT NULL,
    occurred_at timestamptz NOT NULL,
    temperature_c numeric(6,2) NOT NULL,
    duration_minutes integer NOT NULL CHECK (duration_minutes >= 0),
    excursion_flag boolean NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clinical_participants_trial_site
    ON clinical_raw.participants(trial_id, site_id);
CREATE INDEX IF NOT EXISTS idx_clinical_assignments_trial_arm
    ON clinical_raw.randomization_assignments(trial_id, arm);
CREATE INDEX IF NOT EXISTS idx_clinical_outcomes_participant_week
    ON clinical_raw.outcome_assessments(participant_id, visit_week);
CREATE INDEX IF NOT EXISTS idx_clinical_exposures_participant_date
    ON clinical_raw.treatment_exposures(participant_id, dose_date);
CREATE INDEX IF NOT EXISTS idx_clinical_deviations_site_date
    ON clinical_raw.protocol_deviations(site_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_clinical_handling_site_date
    ON clinical_raw.specimen_handling_events(site_id, occurred_at);

COMMENT ON SCHEMA clinical_raw IS
    'Fully synthetic clinical-trial records for InsightFlow V4; never real participant data.';

