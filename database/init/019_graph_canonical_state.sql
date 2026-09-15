-- G2: additive canonical Graph snapshot storage.
--
-- Existing V0-V26 rows intentionally remain untouched.  A nullable column lets the V17 Graph
-- writer dual-read old rows while every new Graph write stores one version-bound typed snapshot
-- alongside the legacy response projection.  The repository writes both columns and evidence in
-- one transaction; no application caller should update state_json after a Graph save.

ALTER TABLE enterprise.investigations
  ADD COLUMN IF NOT EXISTS graph_state_json jsonb;

ALTER TABLE enterprise.investigations
  ADD COLUMN IF NOT EXISTS graph_state_version integer;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conrelid = 'enterprise.investigations'::regclass
      AND conname = 'investigations_graph_state_version_positive'
  ) THEN
    ALTER TABLE enterprise.investigations
      ADD CONSTRAINT investigations_graph_state_version_positive
      CHECK (graph_state_version IS NULL OR graph_state_version >= 1);
  END IF;
END
$$;

CREATE INDEX IF NOT EXISTS ix_investigations_graph_state_version
  ON enterprise.investigations(investigation_id, graph_state_version)
  WHERE graph_state_json IS NOT NULL;

