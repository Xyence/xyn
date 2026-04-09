-- Migration: 016_change_effort_promotions_and_releases
-- Purpose: Add promotion intent tracking and explicit release declaration provenance.

BEGIN;

CREATE TABLE IF NOT EXISTS change_effort_promotions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  effort_id UUID NOT NULL REFERENCES change_efforts(id) ON DELETE CASCADE,
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  artifact_slug VARCHAR(255) NOT NULL,
  from_branch VARCHAR(255) NOT NULL,
  to_branch VARCHAR(255) NOT NULL,
  strategy VARCHAR(64) NOT NULL DEFAULT 'merge_commit',
  status VARCHAR(32) NOT NULL DEFAULT 'requested',
  preflight_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  approval_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  merge_commit_sha VARCHAR(64) NULL,
  requested_by VARCHAR(255) NOT NULL DEFAULT 'system',
  approved_by VARCHAR(255) NULL,
  requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_change_effort_promotions_effort
  ON change_effort_promotions(effort_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_change_effort_promotions_workspace_status
  ON change_effort_promotions(workspace_id, status);
CREATE INDEX IF NOT EXISTS ix_change_effort_promotions_artifact
  ON change_effort_promotions(artifact_slug, created_at DESC);

CREATE TABLE IF NOT EXISTS release_declarations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  environment_id UUID NULL REFERENCES environments(id) ON DELETE SET NULL,
  effort_id UUID NULL REFERENCES change_efforts(id) ON DELETE SET NULL,
  artifact_slug VARCHAR(255) NOT NULL,
  target_commit_sha VARCHAR(64) NOT NULL,
  artifact_revision_map_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  image_digest_map_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  pipeline_provider VARCHAR(64) NOT NULL DEFAULT 'github_actions',
  status VARCHAR(32) NOT NULL DEFAULT 'declared',
  declared_by VARCHAR(255) NOT NULL DEFAULT 'system',
  declared_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_release_declarations_workspace_created
  ON release_declarations(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_release_declarations_artifact_created
  ON release_declarations(artifact_slug, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_release_declarations_target_commit
  ON release_declarations(target_commit_sha);

INSERT INTO schema_migrations (id, applied_at)
VALUES ('016_change_effort_promotions_and_releases', NOW())
ON CONFLICT (id) DO NOTHING;

COMMIT;

