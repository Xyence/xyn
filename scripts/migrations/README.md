# Database Migrations

This directory contains SQL migrations for the Xyn core schema.

## Migration System

**Style**: Plain SQL files with manual application (no Alembic/framework)

**Tracking**: `schema_migrations` table records applied migrations

**Idempotency**: All migrations use `IF NOT EXISTS` / guarded blocks

## Numbering Scheme

```
000_migrations_ledger.sql           - Migration tracking ledger (foundation)
001_initial_schema.sql              - Baseline schema (retroactive)
002_add_scheduling_and_priority.sql - Phase 1: Scheduled + Priority Queues
003_add_run_edges_for_dag.sql       - Phase 2: DAG execution (parent/child)
004_add_queue_claim_indexes.sql     - Production: Claim query optimization
005_steps_run_idx_constraints.sql   - Production: Step ordering constraints
006_normalize_core_timestamps_timestamptz.sql - Normalize core timestamps to TIMESTAMPTZ
007_artifact_registry.sql           - Artifact registry + workspace settings
008_workspace_drafts_jobs_phase1.sql - Workspace-scoped drafts/jobs foundation
009_locations_primitive.sql         - Shared workspace-scoped location primitive
010_palette_commands_registry.sql   - Palette command registry foundation
011_artifact_scopes_and_context_packs.sql - Artifact scope/sync fields + context-pack defaults
012_runtime_execution_layer.sql     - Runtime execution contract + worker registrations
013_lifecycle_transitions.sql       - Lifecycle transition history primitive
014_environments_siblings_activations.sql - Write-through environment/sibling/activation state
```

## Migration Philosophy

**001 is retroactive**: The initial schema is created by SQLAlchemy models in dev, but 001 provides a deterministic baseline for production environments.

**Excludes from 001**:
- `run_edges` table (added in 003)
- `parent_run_id` column on runs (added in 003)

**Each migration**:
- Must be idempotent (safe to run multiple times)
- Must record itself in `schema_migrations` ledger
- Should include verification queries
- Should document what it changes and why

## Applying Migrations

### Automated (recommended)

```bash
./scripts/apply_migrations.sh
```

This script:
1. Checks `schema_migrations` ledger
2. Applies missing migrations in order
3. Prints final ledger state
4. Supports both local-dev and production/external DB execution targets:
   - host `psql` + `DATABASE_URL`
   - `docker compose exec core` (uses container `DATABASE_URL`)
   - local compose `postgres` service fallback
   - legacy `xyn-postgres` fallback

### Manual

```bash
psql $DATABASE_URL -f scripts/migrations/001_initial_schema.sql
```

Or via Docker:

```bash
cat scripts/migrations/001_initial_schema.sql | docker exec -i xyn-postgres psql -U xyn -d xyn
```

For production/external DB mode (`XYN_DB_MODE=external`), prefer:

```bash
DATABASE_URL=postgresql://... ./scripts/apply_migrations.sh
```

or run it from a host where `docker compose` `core` service is running with the correct `DATABASE_URL`.

## Development vs Production

**Development** (local-dev):
- Set `XYN_AUTO_CREATE_SCHEMA=true` to allow SQLAlchemy `create_all()`
- Schema created automatically on first app startup
- Migrations can be applied later for testing

**Production** (staging, prod):
- Set `XYN_AUTO_CREATE_SCHEMA=false` (default)
- Schema must exist via migrations before app starts
- App startup verifies required migrations are applied

**Migration Requirements** (via `XYN_REQUIRED_MIGRATIONS`):

Default (minimal):
```bash
XYN_REQUIRED_MIGRATIONS=001_initial_schema
```

Strict mode (recommended for production):
```bash
XYN_REQUIRED_MIGRATIONS=001_initial_schema,002_add_scheduling_and_priority,003_add_run_edges_for_dag,004_add_queue_claim_indexes,005_steps_run_idx_constraints,006_normalize_core_timestamps_timestamptz,007_artifact_registry,008_workspace_drafts_jobs_phase1,009_locations_primitive,010_palette_commands_registry,011_artifact_scopes_and_context_packs,012_runtime_execution_layer,013_lifecycle_transitions,014_environments_siblings_activations
```

This provides a "fail fast on boot" contract - the app will refuse to start if required migrations are missing.

## Checking Migration Status

```sql
SELECT id, applied_at FROM schema_migrations ORDER BY id;
```

## Creating New Migrations

1. **Choose next number**: `015_your_feature.sql`

2. **Follow template**:
```sql
-- Migration: 006_your_feature
-- Purpose: Brief description
-- Notes: Important context

BEGIN;

-- DDL changes here (idempotent)
CREATE TABLE IF NOT EXISTS ...;
ALTER TABLE ... ADD COLUMN IF NOT EXISTS ...;

COMMIT;

-- Verification queries
SELECT ...;

-- Record migration
INSERT INTO schema_migrations (id)
VALUES ('006_your_feature')
ON CONFLICT (id) DO NOTHING;
```

3. **Test idempotency**: Run migration twice to verify it's safe

4. **Update this README**: Add to numbering scheme above

## Historical Migrations

**scripts/migrate_to_queue_model.sql**: Pre-001 ad-hoc migration that converted from synchronous execution to queue-based model. Not numbered because it predates the migration system.

## Future: core.migrations.apply@v1 Blueprint

The `core.migrations.apply@v1` blueprint (registered but not implemented) will automate migration application:

1. List files in `scripts/migrations/`
2. Check `schema_migrations` ledger for each
3. Apply if missing
4. Record in ledger

This will enable automated migration runs as part of deployment pipelines.
