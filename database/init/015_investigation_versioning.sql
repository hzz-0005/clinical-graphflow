-- V26: make investigation persistence retry-safe and optimistic-lockable.
-- This migration is additive so existing V0-V25 records remain readable.

ALTER TABLE enterprise.investigations
  ADD COLUMN IF NOT EXISTS request_id text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_investigations_request_id
  ON enterprise.investigations(request_id)
  WHERE request_id IS NOT NULL;

-- One investigation has one approval lifecycle.  A retried worker/API request must not create
-- a second pending approval (or a later "ghost" approval after a decision).
CREATE UNIQUE INDEX IF NOT EXISTS uq_approval_requests_investigation
  ON enterprise.approval_requests(investigation_id);

-- Repeated writes with the same correlation id are idempotent audit operations too.
CREATE UNIQUE INDEX IF NOT EXISTS uq_audit_events_request_operation
  ON enterprise.audit_events(action, resource_type, resource_id, request_id);

