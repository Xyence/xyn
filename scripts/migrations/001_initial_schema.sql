-- Migration: 001_initial_schema
-- Purpose:
--   Establish a deterministic baseline schema for environments that do not run
--   SQLAlchemy create_all() at startup (e.g., production, CI, ephemeral workers).
-- Notes:
--   - Idempotent (IF NOT EXISTS / guarded type creation).
--   - Safe to run after SQLAlchemy-created schema already exists.
--   - This migration is documentation + bootstrapping.
--   - Excludes run_edges and parent_run_id (added in 003_add_run_edges_for_dag).
--   - Excludes schema_migrations (added in 000_migrations_ledger).

BEGIN;

-- ============================================================================
-- 1) Create enum types (guarded)
-- ============================================================================

DO $$
BEGIN
  -- draftstatus enum
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'draftstatus') THEN
    CREATE TYPE draftstatus AS ENUM ('DRAFT', 'VALIDATED', 'PROMOTED');
  END IF;

  -- packstatus enum
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'packstatus') THEN
    CREATE TYPE packstatus AS ENUM ('AVAILABLE', 'INSTALLING', 'INSTALLED', 'UPGRADING', 'FAILED', 'UNINSTALLING');
  END IF;

  -- runstatus enum
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'runstatus') THEN
    CREATE TYPE runstatus AS ENUM ('QUEUED', 'CREATED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');
  END IF;

  -- stepstatus enum
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'stepstatus') THEN
    CREATE TYPE stepstatus AS ENUM ('CREATED', 'RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED');
  END IF;
END $$;

-- ============================================================================
-- 1.5) Ensure UUID generation functions are available
-- ============================================================================

-- gen_random_uuid() is built-in on Postgres 13+, but requires pgcrypto on older versions
-- This is idempotent and safe to run on any Postgres version
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================
-- 2) Create core tables (idempotent)
-- ============================================================================

-- blueprints: Blueprint definitions
CREATE TABLE IF NOT EXISTS blueprints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    version VARCHAR(50) NOT NULL,
    trigger_event_type VARCHAR(255),
    definition JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- packs: Pack metadata
CREATE TABLE IF NOT EXISTS packs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pack_ref VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    version VARCHAR(50) NOT NULL,
    description TEXT,
    schema_name VARCHAR(255),
    manifest JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- pack_installations: Installed packs per environment
CREATE TABLE IF NOT EXISTS pack_installations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pack_id UUID NOT NULL REFERENCES packs(id),
    pack_ref VARCHAR(255) NOT NULL,
    env_id VARCHAR(255) NOT NULL,
    status packstatus NOT NULL,
    schema_mode VARCHAR(50) NOT NULL,
    schema_name VARCHAR(255),
    installed_version VARCHAR(50),
    migration_state VARCHAR(255),
    installed_at TIMESTAMP,
    installed_by_run_id UUID,
    error JSON,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    migration_provider VARCHAR(50) NOT NULL DEFAULT 'sql',
    updated_by_run_id UUID,
    last_error_at TIMESTAMP,
    CONSTRAINT ck_pack_installations_schema_mode CHECK (schema_mode IN ('per_pack', 'shared')),
    CONSTRAINT ck_pack_installations_installed_invariants CHECK (
        status != 'INSTALLED' OR (
            schema_name IS NOT NULL AND
            installed_version IS NOT NULL AND
            installed_at IS NOT NULL AND
            installed_by_run_id IS NOT NULL
        )
    )
);

-- events: Event stream for observability
CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_name VARCHAR(255) NOT NULL,
    occurred_at TIMESTAMP NOT NULL DEFAULT NOW(),
    env_id VARCHAR(255) NOT NULL,
    actor VARCHAR(255) NOT NULL,
    correlation_id VARCHAR(255),
    run_id UUID,
    step_id UUID,
    resource_type VARCHAR(255),
    resource_id VARCHAR(255),
    data JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- runs: Job execution records with queue/lease management
-- NOTE: parent_run_id column is NOT included here (added in 003_add_run_edges_for_dag)
CREATE TABLE IF NOT EXISTS runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    blueprint_id UUID REFERENCES blueprints(id),
    event_id UUID REFERENCES events(id),
    status runstatus NOT NULL DEFAULT 'QUEUED',
    actor VARCHAR(255) NOT NULL,
    correlation_id VARCHAR(255) NOT NULL,
    inputs JSON NOT NULL,
    outputs JSON,
    error JSON,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    queued_at TIMESTAMP,
    locked_at TIMESTAMP,
    locked_by VARCHAR(255),
    lease_expires_at TIMESTAMP,
    run_at TIMESTAMP NOT NULL DEFAULT NOW(),
    priority INTEGER NOT NULL DEFAULT 100,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER
);

-- steps: Atomic units within a run
CREATE TABLE IF NOT EXISTS steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES runs(id),
    name VARCHAR(255) NOT NULL,
    idx INTEGER NOT NULL,
    kind VARCHAR(50) NOT NULL,
    status stepstatus NOT NULL DEFAULT 'CREATED',
    inputs JSON,
    outputs JSON,
    error JSON,
    logs_artifact_id UUID,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    started_at TIMESTAMP,
    completed_at TIMESTAMP
);

-- artifacts: File attachments linked to runs/steps
CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    kind VARCHAR(50) NOT NULL,
    content_type VARCHAR(255) NOT NULL,
    byte_length BIGINT,
    sha256 VARCHAR(64),
    run_id UUID REFERENCES runs(id),
    step_id UUID REFERENCES steps(id),
    created_by VARCHAR(255) NOT NULL,
    extra_metadata JSON NOT NULL,
    storage_path VARCHAR(1024),
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- drafts: Draft storage
CREATE TABLE IF NOT EXISTS drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    kind VARCHAR(50) NOT NULL,
    trigger_event_type VARCHAR(255),
    definition JSON NOT NULL,
    status draftstatus NOT NULL DEFAULT 'DRAFT',
    notes TEXT,
    source_run_id UUID REFERENCES runs(id),
    revision INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- nodes: Node/worker registration
CREATE TABLE IF NOT EXISTS nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    parent_id UUID REFERENCES nodes(id),
    version VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 3) Create baseline indexes (idempotent)
-- ============================================================================

-- Blueprints indexes
CREATE UNIQUE INDEX IF NOT EXISTS ix_blueprints_name ON blueprints(name);
CREATE INDEX IF NOT EXISTS ix_blueprints_trigger_event_type ON blueprints(trigger_event_type);

-- Packs indexes
-- (No additional indexes beyond PK)

-- Pack installations indexes
CREATE INDEX IF NOT EXISTS ix_pack_installations_pack_id ON pack_installations(pack_id);
CREATE INDEX IF NOT EXISTS ix_pack_installations_pack_ref ON pack_installations(pack_ref);
CREATE INDEX IF NOT EXISTS ix_pack_installations_env_id ON pack_installations(env_id);

-- Events indexes
CREATE INDEX IF NOT EXISTS ix_events_event_name ON events(event_name);
CREATE INDEX IF NOT EXISTS ix_events_occurred_at ON events(occurred_at);
CREATE INDEX IF NOT EXISTS ix_events_correlation_id ON events(correlation_id);
CREATE INDEX IF NOT EXISTS ix_events_run_id ON events(run_id);

-- Runs indexes (baseline - additional indexes added in 002, 004)
CREATE INDEX IF NOT EXISTS ix_runs_blueprint_id ON runs(blueprint_id);
CREATE INDEX IF NOT EXISTS ix_runs_correlation_id ON runs(correlation_id);
CREATE INDEX IF NOT EXISTS ix_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS ix_runs_created_at ON runs(created_at);
CREATE INDEX IF NOT EXISTS ix_runs_queued_at ON runs(queued_at);
CREATE INDEX IF NOT EXISTS ix_runs_lease_expires_at ON runs(lease_expires_at);

-- Steps indexes (baseline - additional indexes added in 005)
CREATE INDEX IF NOT EXISTS ix_steps_run_id ON steps(run_id);
CREATE INDEX IF NOT EXISTS ix_steps_status ON steps(status);

-- Artifacts indexes
CREATE INDEX IF NOT EXISTS ix_artifacts_run_id ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_step_id ON artifacts(step_id);
CREATE INDEX IF NOT EXISTS ix_artifacts_created_at ON artifacts(created_at);

-- Drafts indexes
DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'drafts' AND column_name = 'name'
  ) THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS ix_drafts_name ON drafts(name)';
  ELSIF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'drafts' AND column_name = 'title'
  ) THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS ix_drafts_title ON drafts(title)';
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_drafts_status ON drafts(status);

-- Nodes indexes
-- (No additional indexes beyond PK and FK)

COMMIT;

-- ============================================================================
-- Verification
-- ============================================================================

-- Show table count
SELECT COUNT(*) AS baseline_table_count
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('blueprints', 'packs', 'pack_installations', 'events', 'runs', 'steps', 'artifacts', 'drafts', 'nodes');

-- Show enum types
SELECT typname
FROM pg_type
WHERE typname IN ('draftstatus', 'packstatus', 'runstatus', 'stepstatus')
ORDER BY typname;

-- ============================================================================
-- Record migration
-- ============================================================================

INSERT INTO schema_migrations (id)
VALUES ('001_initial_schema')
ON CONFLICT (id) DO NOTHING;
