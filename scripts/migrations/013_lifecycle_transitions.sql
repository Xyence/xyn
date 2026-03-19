-- Migration: 013_lifecycle_transitions
-- Purpose: Add reusable lifecycle transition history primitive.

BEGIN;

CREATE TABLE IF NOT EXISTS lifecycle_transitions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NULL REFERENCES workspaces(id) ON DELETE SET NULL,
  lifecycle_name VARCHAR(128) NOT NULL,
  object_type VARCHAR(128) NOT NULL,
  object_id VARCHAR(255) NOT NULL,
  from_state VARCHAR(64),
  to_state VARCHAR(64) NOT NULL,
  actor VARCHAR(255),
  reason TEXT,
  metadata_json JSON NOT NULL DEFAULT '{}'::json,
  correlation_id VARCHAR(255),
  run_id UUID NULL REFERENCES runs(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_lifecycle_transitions_workspace_id
  ON lifecycle_transitions(workspace_id);
CREATE INDEX IF NOT EXISTS ix_lifecycle_transitions_object
  ON lifecycle_transitions(object_type, object_id);
CREATE INDEX IF NOT EXISTS ix_lifecycle_transitions_lifecycle_name
  ON lifecycle_transitions(lifecycle_name);
CREATE INDEX IF NOT EXISTS ix_lifecycle_transitions_created_at
  ON lifecycle_transitions(created_at);
CREATE INDEX IF NOT EXISTS ix_lifecycle_transitions_correlation_id
  ON lifecycle_transitions(correlation_id);
CREATE INDEX IF NOT EXISTS ix_lifecycle_transitions_run_id
  ON lifecycle_transitions(run_id);

INSERT INTO schema_migrations (id, applied_at)
VALUES ('013_lifecycle_transitions', NOW())
ON CONFLICT (id) DO NOTHING;

COMMIT;
