-- Migration: 008_workspace_drafts_jobs_phase1
-- Purpose:
--   Add workspace-scoped draft/job foundation for app-intent workflow.

BEGIN;

CREATE TABLE IF NOT EXISTS workspaces (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slug VARCHAR(255) NOT NULL UNIQUE,
  title VARCHAR(255) NOT NULL DEFAULT 'Default Workspace',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_workspaces_slug ON workspaces(slug);

INSERT INTO workspaces (slug, title)
VALUES ('default', 'Default Workspace')
ON CONFLICT (slug) DO NOTHING;

ALTER TABLE drafts ADD COLUMN IF NOT EXISTS workspace_id UUID;
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS type VARCHAR(100);
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS title VARCHAR(255);
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS content_json JSON;
ALTER TABLE drafts ADD COLUMN IF NOT EXISTS created_by VARCHAR(255);

UPDATE drafts
SET workspace_id = COALESCE(workspace_id, (SELECT id FROM workspaces WHERE slug = 'default' LIMIT 1));

DO $$
BEGIN
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='kind'
  ) THEN
    UPDATE drafts SET type = COALESCE(type, kind);
  END IF;
END $$;
UPDATE drafts SET type = COALESCE(type, 'app_intent');

DO $$
BEGIN
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='name'
  ) THEN
    UPDATE drafts SET title = COALESCE(title, name);
  END IF;
END $$;
UPDATE drafts SET title = COALESCE(title, 'Untitled Draft');

DO $$
BEGIN
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='definition'
  ) THEN
    UPDATE drafts SET content_json = COALESCE(content_json, definition);
  END IF;
END $$;
UPDATE drafts SET content_json = COALESCE(content_json, '{}'::json);
UPDATE drafts SET created_by = COALESCE(created_by, 'system');

DO $$
BEGIN
  IF NOT EXISTS (
      SELECT 1
      FROM pg_constraint
      WHERE conname = 'fk_drafts_workspace_id'
  ) THEN
    ALTER TABLE drafts
      ADD CONSTRAINT fk_drafts_workspace_id
      FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT;
  END IF;
END $$;

ALTER TABLE drafts ALTER COLUMN workspace_id SET NOT NULL;
ALTER TABLE drafts ALTER COLUMN type SET NOT NULL;
ALTER TABLE drafts ALTER COLUMN title SET NOT NULL;
ALTER TABLE drafts ALTER COLUMN content_json SET NOT NULL;
ALTER TABLE drafts ALTER COLUMN created_by SET NOT NULL;

ALTER TABLE drafts ADD COLUMN IF NOT EXISTS status_v2 VARCHAR(32);
DO $$
BEGIN
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='status'
  ) THEN
    UPDATE drafts
    SET status_v2 = COALESCE(
      status_v2,
      CASE UPPER(COALESCE(status::text, 'DRAFT'))
        WHEN 'DRAFT' THEN 'draft'
        WHEN 'VALIDATED' THEN 'ready'
        WHEN 'PROMOTED' THEN 'submitted'
        ELSE 'draft'
      END
    );
  END IF;
END $$;
UPDATE drafts SET status_v2 = COALESCE(status_v2, 'draft');
ALTER TABLE drafts ALTER COLUMN status_v2 SET NOT NULL;

DO $$
BEGIN
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='status'
  ) THEN
    ALTER TABLE drafts DROP COLUMN status;
  END IF;
END $$;

ALTER TABLE drafts RENAME COLUMN status_v2 TO status;

DO $$
BEGIN
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='kind'
  ) THEN
    ALTER TABLE drafts DROP COLUMN kind;
  END IF;
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='name'
  ) THEN
    ALTER TABLE drafts DROP COLUMN name;
  END IF;
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='definition'
  ) THEN
    ALTER TABLE drafts DROP COLUMN definition;
  END IF;
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='trigger_event_type'
  ) THEN
    ALTER TABLE drafts DROP COLUMN trigger_event_type;
  END IF;
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='notes'
  ) THEN
    ALTER TABLE drafts DROP COLUMN notes;
  END IF;
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='source_run_id'
  ) THEN
    ALTER TABLE drafts DROP COLUMN source_run_id;
  END IF;
  IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_name='drafts' AND column_name='revision'
  ) THEN
    ALTER TABLE drafts DROP COLUMN revision;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS ix_drafts_workspace_id ON drafts(workspace_id);
CREATE INDEX IF NOT EXISTS ix_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS ix_drafts_type ON drafts(type);

CREATE TABLE IF NOT EXISTS jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
  type VARCHAR(100) NOT NULL,
  status VARCHAR(32) NOT NULL DEFAULT 'queued',
  input_json JSON NOT NULL DEFAULT '{}'::json,
  output_json JSON,
  logs_text TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_jobs_workspace_id ON jobs(workspace_id);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status);

INSERT INTO schema_migrations (id)
VALUES ('008_workspace_drafts_jobs_phase1')
ON CONFLICT (id) DO NOTHING;

COMMIT;
