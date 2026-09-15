CREATE TABLE IF NOT EXISTS enterprise.temporal_signal_outbox (
  event_id text PRIMARY KEY,
  workflow_id text NOT NULL,
  approval_id text NOT NULL,
  expected_version integer NOT NULL CHECK (expected_version >= 1),
  approved boolean NOT NULL,
  decided_by text NOT NULL,
  decided_at timestamptz NOT NULL,
  comment text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sent','dead')),
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz
);

CREATE INDEX IF NOT EXISTS ix_temporal_signal_outbox_pending
  ON enterprise.temporal_signal_outbox(status, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS ux_temporal_signal_outbox_decision
  ON enterprise.temporal_signal_outbox(approval_id, expected_version);

