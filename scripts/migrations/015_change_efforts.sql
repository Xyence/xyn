-- Migration: 015_change_efforts
-- Purpose: Add minimal control-plane model for branch-per-effort development.

BEGIN;

CREATE TABLE IF NOT EXISTS change_efforts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  environment_id UUID NULL REFERENCES environments(id) ON DELETE SET NULL,
  sibling_id UUID NULL REFERENCES siblings(id) ON DELETE SET NULL,
  artifact_slug VARCHAR(255) NOT NULL,
  repo_key VARCHAR(255) NULL,
  repo_url TEXT NULL,
  repo_subpath TEXT NULL,
  base_branch VARCHAR(255) NOT NULL DEFAULT 'develop',
  work_branch VARCHAR(255) NULL,
  target_branch VARCHAR(255) NOT NULL DEFAULT 'develop',
  worktree_path TEXT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'created',
  owner VARCHAR(255) NULL,
  created_by VARCHAR(255) NOT NULL DEFAULT 'system',
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_change_efforts_workspace_status
  ON change_efforts(workspace_id, status);
CREATE INDEX IF NOT EXISTS ix_change_efforts_artifact_status
  ON change_efforts(artifact_slug, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_change_efforts_workspace_work_branch
  ON change_efforts(workspace_id, work_branch)
  WHERE work_branch IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_change_efforts_worktree_path
  ON change_efforts(worktree_path)
  WHERE worktree_path IS NOT NULL;

INSERT INTO schema_migrations (id, applied_at)
VALUES ('015_change_efforts', NOW())
ON CONFLICT (id) DO NOTHING;

COMMIT;

