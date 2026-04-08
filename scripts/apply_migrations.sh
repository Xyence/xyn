#!/bin/bash
# Apply SQL migrations from scripts/migrations/ in order
# Uses schema_migrations ledger for tracking

set -euo pipefail

cd "$(dirname "$0")/.."

DB_MODE="${XYN_DB_MODE:-local}"
DATABASE_URL="${DATABASE_URL:-}"

_docker_compose_service_running() {
  local service="$1"
  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi
  local service_id
  service_id="$(docker compose ps -q "${service}" 2>/dev/null | tr -d '[:space:]' || true)"
  [[ -n "${service_id}" ]]
}

_legacy_container_exists() {
  local name="$1"
  if ! command -v docker >/dev/null 2>&1; then
    return 1
  fi
  docker ps -a --format '{{.Names}}' | grep -qx "${name}"
}

MODE=""
TARGET_DESC=""
if command -v psql >/dev/null 2>&1; then
  MODE="host_psql"
  if [[ -z "${DATABASE_URL}" ]]; then
    DATABASE_URL="postgresql://xyn:xyn_dev_password@localhost:5432/xyn"
  fi
  TARGET_DESC="${DATABASE_URL}"
elif _docker_compose_service_running "core"; then
  MODE="core_container_psql"
  TARGET_DESC='(via Docker Compose service: core -> $DATABASE_URL)'
elif _docker_compose_service_running "postgres"; then
  MODE="compose_postgres_service"
  TARGET_DESC="(via Docker Compose service: postgres)"
elif _legacy_container_exists "xyn-postgres"; then
  MODE="legacy_postgres_container"
  TARGET_DESC="(via Docker container: xyn-postgres)"
fi

if [[ -z "${MODE}" ]]; then
  echo "ERROR: Unable to find a migration execution target."
  echo "Checked in order: host psql, docker compose core, docker compose postgres, legacy xyn-postgres."
  echo "For production/external DB, set DATABASE_URL and either:"
  echo "  - install psql on host, or"
  echo "  - run this script where docker compose 'core' is running."
  exit 1
fi

if [[ "${DB_MODE}" == "external" && "${MODE}" == "compose_postgres_service" ]]; then
  echo "ERROR: XYN_DB_MODE=external but migration target resolved to local compose postgres service."
  echo "Set DATABASE_URL for external DB and run with host psql or via running docker compose core service."
  exit 1
fi

run_query_scalar() {
  local query="$1"
  case "${MODE}" in
    host_psql)
      psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -t -A -c "${query}"
      ;;
    core_container_psql)
      docker compose exec -T -e XYN_MIGRATION_SQL="${query}" core \
        sh -lc 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -t -A -c "$XYN_MIGRATION_SQL"'
      ;;
    compose_postgres_service)
      docker compose exec -T postgres psql -U xyn -d xyn -v ON_ERROR_STOP=1 -t -A -c "${query}"
      ;;
    legacy_postgres_container)
      docker exec -i xyn-postgres psql -U xyn -d xyn -v ON_ERROR_STOP=1 -t -A -c "${query}"
      ;;
    *)
      echo "ERROR: unsupported migration mode ${MODE}" >&2
      return 1
      ;;
  esac
}

record_migration_if_signature_present() {
  local migration_id="$1"
  local signature_sql="$2"
  local signature_present
  local already_applied
  signature_present="$(run_query_scalar "SELECT CASE WHEN (${signature_sql}) THEN 1 ELSE 0 END;" | tr -d '[:space:]' || echo "0")"
  if [[ "${signature_present}" != "1" ]]; then
    return 0
  fi
  already_applied="$(run_query_scalar "SELECT COUNT(*) FROM schema_migrations WHERE id='${migration_id}';" | tr -d '[:space:]' || echo "0")"
  if [[ "${already_applied}" != "0" ]]; then
    return 0
  fi
  run_query_scalar "INSERT INTO schema_migrations (id, applied_at) VALUES ('${migration_id}', NOW()) ON CONFLICT (id) DO NOTHING;" >/dev/null
  echo "Backfilled migration ledger entry: ${migration_id}"
}

execute_sql_file() {
  local file="$1"
  case "${MODE}" in
    host_psql)
      psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -f "${file}"
      ;;
    core_container_psql)
      cat "${file}" | docker compose exec -T core sh -lc 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f -'
      ;;
    compose_postgres_service)
      cat "${file}" | docker compose exec -T postgres psql -U xyn -d xyn -v ON_ERROR_STOP=1 -f -
      ;;
    legacy_postgres_container)
      cat "${file}" | docker exec -i xyn-postgres psql -U xyn -d xyn -v ON_ERROR_STOP=1 -f -
      ;;
    *)
      echo "ERROR: unsupported migration mode ${MODE}" >&2
      return 1
      ;;
  esac
}

echo "Applying migrations to: ${TARGET_DESC}"
echo

# Ensure ledger exists before proceeding (helps first-run clarity)
if ! run_query_scalar "SELECT 1 FROM information_schema.tables WHERE table_name='schema_migrations';" | grep -q 1; then
  echo "schema_migrations table not found. Applying 000_migrations_ledger.sql first..."
  execute_sql_file "scripts/migrations/000_migrations_ledger.sql"
fi

echo "Checking for legacy schema footprint to backfill migration ledger..."
record_migration_if_signature_present "001_initial_schema" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='blueprints')
  AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='runs')
  AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='drafts')
"
record_migration_if_signature_present "002_add_scheduling_and_priority" "
  EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='run_at')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='priority')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='attempt')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='max_attempts')
"
record_migration_if_signature_present "003_add_run_edges_for_dag" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='run_edges')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='parent_run_id')
"
record_migration_if_signature_present "004_add_queue_claim_indexes" "
  EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='ix_runs_queue_claim')
  AND EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='ix_runs_queued_due')
  AND EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='ix_runs_running_expired')
"
record_migration_if_signature_present "005_steps_run_idx_constraints" "
  EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='uq_steps_run_idx')
"
record_migration_if_signature_present "006_normalize_core_timestamps_timestamptz" "
  EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name='runs' AND column_name='created_at' AND data_type='timestamp with time zone'
  )
"
record_migration_if_signature_present "007_artifact_registry" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='workspace_settings')
  AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='secrets')
"
record_migration_if_signature_present "008_workspace_drafts_jobs_phase1" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='workspaces')
  AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='jobs')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='drafts' AND column_name='workspace_id')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='drafts' AND column_name='type')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='drafts' AND column_name='title')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='drafts' AND column_name='content_json')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='drafts' AND column_name='created_by')
"
record_migration_if_signature_present "009_locations_primitive" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='locations')
"
record_migration_if_signature_present "010_palette_commands_registry" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='palette_commands')
"
record_migration_if_signature_present "011_artifact_scopes_and_context_packs" "
  EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='artifacts' AND column_name='workspace_id')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='artifacts' AND column_name='storage_scope')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='artifacts' AND column_name='sync_state')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='workspace_settings' AND column_name='default_context_pack_artifact_ids_json')
"
record_migration_if_signature_present "012_runtime_execution_layer" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='runtime_workers')
  AND EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='runs' AND column_name='heartbeat_at')
"
record_migration_if_signature_present "013_lifecycle_transitions" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='lifecycle_transitions')
"
record_migration_if_signature_present "014_environments_siblings_activations" "
  EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='environments')
  AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='siblings')
  AND EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='activations')
"

# Iterate migrations in sorted order (critical for numeric sorting)
mapfile -t MIGRATIONS < <(ls -1 scripts/migrations/*.sql | sort)

for migration in "${MIGRATIONS[@]}"; do
  filename="$(basename "${migration}")"
  migration_id="${filename%.sql}"

  echo -n "Checking ${migration_id}... "

  already_applied="$(run_query_scalar "SELECT COUNT(*) FROM schema_migrations WHERE id = '${migration_id}';" | tr -d '[:space:]' || echo "0")"

  if [[ "${already_applied}" != "0" ]]; then
    echo "✓ already applied"
    continue
  fi

  echo "applying..."
  execute_sql_file "${migration}"

  # Verify migration recorded itself in ledger
  applied_now="$(run_query_scalar "SELECT COUNT(*) FROM schema_migrations WHERE id = '${migration_id}';" | tr -d '[:space:]')"
  if [[ "${applied_now}" == "0" ]]; then
    echo "ERROR: Migration ${migration_id} did not record itself in schema_migrations."
    exit 1
  fi

  echo "  ✓ applied successfully"
done

echo
echo "Migration ledger:"
case "${MODE}" in
  host_psql)
    psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 -c "SELECT id, applied_at FROM schema_migrations ORDER BY id;"
    ;;
  core_container_psql)
    docker compose exec -T core sh -lc 'psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "SELECT id, applied_at FROM schema_migrations ORDER BY id;"'
    ;;
  compose_postgres_service)
    docker compose exec -T postgres psql -U xyn -d xyn -v ON_ERROR_STOP=1 -c "SELECT id, applied_at FROM schema_migrations ORDER BY id;"
    ;;
  legacy_postgres_container)
    docker exec -i xyn-postgres psql -U xyn -d xyn -v ON_ERROR_STOP=1 -c "SELECT id, applied_at FROM schema_migrations ORDER BY id;"
    ;;
esac
