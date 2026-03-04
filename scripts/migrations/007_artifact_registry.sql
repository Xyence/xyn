-- Migration: 007_artifact_registry
-- Purpose:
--   Add workspace-level default artifact registry setting and minimal secret table.

BEGIN;

CREATE TABLE IF NOT EXISTS workspace_settings (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_slug VARCHAR(255) NOT NULL UNIQUE,
  default_artifact_registry_slug VARCHAR(255) NOT NULL DEFAULT 'default-registry',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_workspace_settings_workspace_slug ON workspace_settings(workspace_slug);

CREATE TABLE IF NOT EXISTS secrets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name VARCHAR(255) NOT NULL UNIQUE,
  value TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_secrets_name ON secrets(name);

INSERT INTO schema_migrations (id)
VALUES ('007_artifact_registry')
ON CONFLICT (id) DO NOTHING;

COMMIT;
