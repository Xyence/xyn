#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# The compose stack mounts sibling repos/dirs from ${ROOT_DIR}/.. in local dev.
# CI checkouts often include only xyn, so create empty placeholders to avoid
# mount/startup failures while keeping runtime validation focused on artifact IO.
SIBLING_ROOT="$(dirname "$ROOT_DIR")"
mkdir -p \
  "$SIBLING_ROOT/xyn-platform" \
  "$SIBLING_ROOT/xyn-api" \
  "$SIBLING_ROOT/xyn-ui" \
  "$SIBLING_ROOT/xyn-contracts"

echo "[runtime-s3] Starting stack with MinIO overlay..."
docker compose -f compose.yml -f compose.minio.yml up -d --build traefik postgres redis minio minio-init

echo "[runtime-s3] Bootstrapping schema and running runtime S3 integration tests..."
docker compose -f compose.yml -f compose.minio.yml run --rm \
  -e XYN_AUTO_CREATE_SCHEMA=true \
  -e XYN_RUNTIME_ARTIFACT_PROVIDER=s3 \
  -e XYN_RUNTIME_ARTIFACT_S3_BUCKET="${XYN_RUNTIME_ARTIFACT_S3_BUCKET:-xyn-runtime-artifacts}" \
  -e XYN_RUNTIME_ARTIFACT_S3_REGION="${XYN_RUNTIME_ARTIFACT_S3_REGION:-us-east-1}" \
  -e XYN_RUNTIME_ARTIFACT_S3_PREFIX="${XYN_RUNTIME_ARTIFACT_S3_PREFIX:-xyn/runtime}" \
  -e XYN_RUNTIME_ARTIFACT_S3_ENDPOINT_URL="${XYN_RUNTIME_ARTIFACT_S3_ENDPOINT_URL:-http://minio:9000}" \
  -e XYN_RUNTIME_ARTIFACT_S3_ACCESS_KEY_ID="${XYN_RUNTIME_ARTIFACT_S3_ACCESS_KEY_ID:-${XYN_MINIO_ROOT_USER:-xynminio}}" \
  -e XYN_RUNTIME_ARTIFACT_S3_SECRET_ACCESS_KEY="${XYN_RUNTIME_ARTIFACT_S3_SECRET_ACCESS_KEY:-${XYN_MINIO_ROOT_PASSWORD:-xynminio123}}" \
  -e XYN_RUNTIME_ARTIFACT_S3_FORCE_PATH_STYLE="${XYN_RUNTIME_ARTIFACT_S3_FORCE_PATH_STYLE:-true}" \
  core \
  /bin/sh -lc '
    set -e
    python - <<'"'"'PY'"'"'
import sys
from sqlalchemy import inspect

from core.database import engine, init_db
from core import models  # noqa: F401 - register SQLAlchemy metadata before init

# Use the canonical schema bootstrap path (init_db) rather than manual per-table
# creation here. It already applies dev-safe ordering and compatibility upgrades.
try:
    init_db()
except Exception as exc:  # pragma: no cover - defensive CI guard
    print(f"[runtime-s3/bootstrap] init_db failed: {exc!r}", file=sys.stderr)
    raise

tables = set(inspect(engine).get_table_names())
required = {"artifacts", "runs", "steps", "events"}
missing = sorted(required - tables)
print(f"tables={sorted(tables)}")
if missing:
    raise SystemExit(f"Missing required runtime tables after bootstrap: {missing}")
PY
    python -m unittest -v core.tests.test_runtime_s3_minio_integration
  '

echo "[runtime-s3] Listing MinIO objects under configured prefix..."
docker compose -f compose.yml -f compose.minio.yml run --rm --entrypoint /bin/sh \
  -e XYN_MINIO_ROOT_USER="${XYN_MINIO_ROOT_USER:-xynminio}" \
  -e XYN_MINIO_ROOT_PASSWORD="${XYN_MINIO_ROOT_PASSWORD:-xynminio123}" \
  -e XYN_RUNTIME_ARTIFACT_S3_BUCKET="${XYN_RUNTIME_ARTIFACT_S3_BUCKET:-xyn-runtime-artifacts}" \
  -e XYN_RUNTIME_ARTIFACT_S3_PREFIX="${XYN_RUNTIME_ARTIFACT_S3_PREFIX:-xyn/runtime}" \
  minio-init -lc '
    mc alias set local http://minio:9000 "$XYN_MINIO_ROOT_USER" "$XYN_MINIO_ROOT_PASSWORD" >/dev/null;
    mc ls --recursive "local/$XYN_RUNTIME_ARTIFACT_S3_BUCKET/$XYN_RUNTIME_ARTIFACT_S3_PREFIX" || true
  '

echo "[runtime-s3] Validation complete."
