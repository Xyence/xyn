#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

DATABASE_URL="${DATABASE_URL:-postgresql://xyn:xyn_dev_password@localhost:5432/xyn}"
WAIT_SECONDS="${XYN_DB_BOOTSTRAP_WAIT_SECONDS:-60}"

export DATABASE_URL

wait_for_db() {
  python - <<'PY'
import os
import time
import sys
from sqlalchemy import create_engine, text

url = os.environ["DATABASE_URL"]
wait_seconds = int(os.environ.get("XYN_DB_BOOTSTRAP_WAIT_SECONDS", "60"))
engine = create_engine(url, pool_pre_ping=True)
deadline = time.time() + max(1, wait_seconds)
last_error = "unknown"
while time.time() < deadline:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            print("[db-bootstrap] database is reachable")
            sys.exit(0)
    except Exception as exc:
        last_error = str(exc)
        time.sleep(1)
print(f"[db-bootstrap] database did not become ready within {wait_seconds}s: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

verify_schema() {
  python - <<'PY'
import os
import sys
from sqlalchemy import create_engine, text

required_tables = ["schema_migrations", "workspaces", "artifacts", "jobs", "drafts"]
engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
with engine.connect() as conn:
    missing = []
    for name in required_tables:
        row = conn.execute(text("SELECT to_regclass(:name)"), {"name": name}).first()
        if not row or not row[0]:
            missing.append(name)
    if missing:
        print(f"[db-bootstrap] missing required tables after migrations: {missing}", file=sys.stderr)
        sys.exit(1)
    migration_count = conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one()
    print(f"[db-bootstrap] schema ready; applied migrations={migration_count}")
PY
}

echo "[db-bootstrap] waiting for Postgres: ${DATABASE_URL}"
wait_for_db

echo "[db-bootstrap] applying migrations"
chmod +x scripts/apply_migrations.sh
./scripts/apply_migrations.sh

echo "[db-bootstrap] verifying schema"
verify_schema

echo "[db-bootstrap] complete"
