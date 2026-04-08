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
