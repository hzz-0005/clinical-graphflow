CREATE SCHEMA IF NOT EXISTS enterprise;

CREATE TABLE IF NOT EXISTS enterprise.users (
  user_id text PRIMARY KEY,
  email text UNIQUE NOT NULL,
  display_name text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin','analyst','viewer')),
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS enterprise.user_country_scopes (user_id text REFERENCES enterprise.users ON DELETE CASCADE,country text NOT NULL,PRIMARY KEY(user_id,country));
CREATE TABLE IF NOT EXISTS enterprise.user_account_manager_scopes (user_id text REFERENCES enterprise.users ON DELETE CASCADE,account_manager_id text NOT NULL,PRIMARY KEY(user_id,account_manager_id));

CREATE TABLE IF NOT EXISTS enterprise.investigations (
  investigation_id uuid PRIMARY KEY, owner_user_id text NOT NULL, question text NOT NULL,
  metric text, status text NOT NULL, publication_status text NOT NULL,
  agent_mode text NOT NULL, provider text, model text, confidence numeric NOT NULL DEFAULT 0,
  answer text, state_json jsonb NOT NULL, scope_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), version integer NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_investigations_owner_created ON enterprise.investigations(owner_user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS enterprise.evidence (
  investigation_id uuid REFERENCES enterprise.investigations ON DELETE CASCADE, evidence_id text NOT NULL, sequence integer NOT NULL,
  claim text NOT NULL, source text NOT NULL, sql_text text NOT NULL, rows_json jsonb NOT NULL DEFAULT '[]', metric text,
  metric_version text NOT NULL, evidence_type text NOT NULL, sample_size integer, effect_json jsonb,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(investigation_id,evidence_id)
);
CREATE TABLE IF NOT EXISTS enterprise.approval_requests (
  approval_id uuid PRIMARY KEY, investigation_id uuid REFERENCES enterprise.investigations ON DELETE CASCADE,
  requested_by text NOT NULL,status text NOT NULL CHECK(status IN ('pending','approved','rejected','cancelled')),reason text NOT NULL,
  decided_by text,decision_comment text,created_at timestamptz NOT NULL DEFAULT now(),decided_at timestamptz,version integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS enterprise.audit_events (
  event_id bigserial PRIMARY KEY,actor_user_id text NOT NULL,action text NOT NULL,resource_type text NOT NULL,resource_id text NOT NULL,
  outcome text NOT NULL,metadata_json jsonb NOT NULL DEFAULT '{}',request_id text NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_actor_created ON enterprise.audit_events(actor_user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS enterprise.investigation_schedules (
  schedule_id uuid PRIMARY KEY,owner_user_id text NOT NULL,name text NOT NULL,question text NOT NULL,provider text,
  cron_expression text NOT NULL,timezone text NOT NULL,enabled boolean NOT NULL DEFAULT true,next_run_at timestamptz,last_run_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),version integer NOT NULL DEFAULT 1,scope_json jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS enterprise.investigation_runs (
  run_id uuid PRIMARY KEY,schedule_id uuid REFERENCES enterprise.investigation_schedules ON DELETE CASCADE,investigation_id uuid,
  status text NOT NULL,scheduled_for timestamptz NOT NULL,started_at timestamptz,finished_at timestamptz,error_code text,
  UNIQUE(schedule_id,scheduled_for)
);
CREATE TABLE IF NOT EXISTS enterprise.alerts (
  alert_id uuid PRIMARY KEY,investigation_id uuid REFERENCES enterprise.investigations ON DELETE CASCADE,recipient_user_id text NOT NULL,
  severity text NOT NULL,title text NOT NULL,body text NOT NULL,status text NOT NULL DEFAULT 'unread',created_at timestamptz NOT NULL DEFAULT now(),read_at timestamptz
);

ALTER TABLE enterprise.investigations ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS investigations_scope ON enterprise.investigations;
CREATE POLICY investigations_scope ON enterprise.investigations USING (
  current_setting('app.current_role',true)='admin' OR owner_user_id=current_setting('app.user_id',true)
);

