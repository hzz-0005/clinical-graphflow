-- V26: lease pending outbox rows so concurrent workers do not deliver the same event at once.
-- An expired lease is reclaimable; this is recoverable coordination metadata, not clinical state.

ALTER TABLE enterprise.temporal_signal_outbox
  ADD COLUMN IF NOT EXISTS claim_id text;

ALTER TABLE enterprise.temporal_signal_outbox
  ADD COLUMN IF NOT EXISTS claim_expires_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_temporal_signal_outbox_claimable
  ON enterprise.temporal_signal_outbox(status, claim_expires_at, created_at);

