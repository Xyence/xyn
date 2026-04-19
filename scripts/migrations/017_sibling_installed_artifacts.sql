-- Migration: 017_sibling_installed_artifacts
-- Purpose: Add normalized multi-artifact sibling install state while preserving legacy singular columns.

BEGIN;

CREATE TABLE IF NOT EXISTS sibling_installed_artifacts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  sibling_id UUID NOT NULL REFERENCES siblings(id) ON DELETE CASCADE,
  artifact_slug VARCHAR(255) NOT NULL,
  artifact_id VARCHAR(255) NULL,
  artifact_version VARCHAR(64) NULL,
  artifact_revision_id VARCHAR(255) NULL,
  workspace_id VARCHAR(255) NULL,
  workspace_slug VARCHAR(255) NULL,
  source VARCHAR(64) NOT NULL DEFAULT 'generated',
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_sibling_installed_artifacts_sibling_slug UNIQUE (sibling_id, artifact_slug)
);

CREATE INDEX IF NOT EXISTS ix_sibling_installed_artifacts_sibling
  ON sibling_installed_artifacts(sibling_id);
CREATE INDEX IF NOT EXISTS ix_sibling_installed_artifacts_slug_revision
  ON sibling_installed_artifacts(artifact_slug, artifact_revision_id);

INSERT INTO schema_migrations (id, applied_at)
VALUES ('017_sibling_installed_artifacts', NOW())
ON CONFLICT (id) DO NOTHING;

COMMIT;
