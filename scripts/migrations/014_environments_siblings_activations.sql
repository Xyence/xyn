-- Migration: 014_environments_siblings_activations
-- Purpose: Add explicit write-through state for environments, siblings, activations.

BEGIN;

CREATE TABLE IF NOT EXISTS environments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  slug VARCHAR(128) NOT NULL,
  title VARCHAR(255) NOT NULL,
  kind VARCHAR(32) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'active',
  is_ephemeral BOOLEAN NOT NULL DEFAULT false,
  ttl_expires_at TIMESTAMPTZ NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_environments_workspace_slug UNIQUE (workspace_id, slug)
);

CREATE INDEX IF NOT EXISTS ix_environments_workspace_kind
  ON environments(workspace_id, kind);
CREATE INDEX IF NOT EXISTS ix_environments_status
  ON environments(status);

CREATE TABLE IF NOT EXISTS siblings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  environment_id UUID NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name VARCHAR(255) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'provisioning',
  compose_project VARCHAR(255) NULL,
  deployment_id VARCHAR(255) NULL,
  ui_url TEXT NULL,
  api_url TEXT NULL,
  runtime_base_url TEXT NULL,
  runtime_public_url TEXT NULL,
  runtime_target_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  runtime_registration_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  installed_artifact_slug VARCHAR(255) NULL,
  installed_artifact_version VARCHAR(64) NULL,
  installed_artifact_revision_id VARCHAR(255) NULL,
  workspace_app_instance_id VARCHAR(255) NULL,
  source_job_id UUID NULL REFERENCES jobs(id) ON DELETE SET NULL,
  last_seen_at TIMESTAMPTZ NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_siblings_environment_status
  ON siblings(environment_id, status);
CREATE INDEX IF NOT EXISTS ix_siblings_workspace_instance
  ON siblings(workspace_id, workspace_app_instance_id);
CREATE INDEX IF NOT EXISTS ix_siblings_artifact_revision
  ON siblings(installed_artifact_slug, installed_artifact_revision_id);

CREATE TABLE IF NOT EXISTS activations (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  environment_id UUID NOT NULL REFERENCES environments(id) ON DELETE CASCADE,
  sibling_id UUID NULL REFERENCES siblings(id) ON DELETE SET NULL,
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  artifact_slug VARCHAR(255) NOT NULL,
  artifact_revision_id VARCHAR(255) NULL,
  artifact_version VARCHAR(64) NULL,
  workspace_app_instance_id VARCHAR(255) NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'pending',
  source_job_id UUID NULL REFERENCES jobs(id) ON DELETE SET NULL,
  idempotency_key VARCHAR(255) NULL,
  capability_entry_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  error_text TEXT NULL,
  requested_by VARCHAR(255) NOT NULL DEFAULT 'system',
  requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  activated_at TIMESTAMPTZ NULL,
  failed_at TIMESTAMPTZ NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_activations_environment_idempotency UNIQUE (environment_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS ix_activations_environment_status
  ON activations(environment_id, status);
CREATE INDEX IF NOT EXISTS ix_activations_artifact_revision
  ON activations(artifact_slug, artifact_revision_id);
CREATE INDEX IF NOT EXISTS ix_activations_workspace_instance
  ON activations(workspace_id, workspace_app_instance_id);

INSERT INTO schema_migrations (id, applied_at)
VALUES ('014_environments_siblings_activations', NOW())
ON CONFLICT (id) DO NOTHING;

COMMIT;
