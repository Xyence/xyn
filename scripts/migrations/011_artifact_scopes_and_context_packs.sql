-- Artifact scope/state + explicit context-pack bindings

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS workspace_id UUID NULL REFERENCES workspaces(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS ix_artifacts_workspace_id
  ON artifacts(workspace_id);

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS storage_scope VARCHAR(32) NOT NULL DEFAULT 'instance-local';

CREATE INDEX IF NOT EXISTS ix_artifacts_storage_scope
  ON artifacts(storage_scope);

ALTER TABLE artifacts
  ADD COLUMN IF NOT EXISTS sync_state VARCHAR(32) NOT NULL DEFAULT 'local';

CREATE INDEX IF NOT EXISTS ix_artifacts_sync_state
  ON artifacts(sync_state);

ALTER TABLE workspace_settings
  ADD COLUMN IF NOT EXISTS default_context_pack_artifact_ids_json JSON NOT NULL DEFAULT '[]'::json;

ALTER TABLE workspace_settings
  ADD COLUMN IF NOT EXISTS artifact_sync_target VARCHAR(512);

INSERT INTO schema_migrations (id, applied_at)
VALUES ('011_artifact_scopes_and_context_packs', NOW())
ON CONFLICT (id) DO NOTHING;
