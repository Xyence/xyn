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
from sqlalchemy import inspect

from core.database import Base, engine
from core import models  # noqa: F401 - register SQLAlchemy metadata

for table in Base.metadata.sorted_tables:
    with engine.connect() as conn:
        tx = conn.begin()
        try:
            table.create(bind=conn, checkfirst=True)
            tx.commit()
        except Exception as exc:  # pragma: no cover - defensive CI guard
            tx.rollback()
            msg = str(exc).lower()
            if "already exists" not in msg:
                raise

# Guard critical runtime tables explicitly; SQLAlchemy may skip some cyclic
# FK-related DDL on initial passes when legacy partial schemas exist.
for name in ("workspaces", "runs", "steps", "artifacts", "events"):
    table = Base.metadata.tables.get(name)
    if table is not None:
        table.create(bind=engine, checkfirst=True)

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
docker run --rm --network xyn_default --entrypoint /bin/sh \
  -e XYN_MINIO_ROOT_USER="${XYN_MINIO_ROOT_USER:-xynminio}" \
  -e XYN_MINIO_ROOT_PASSWORD="${XYN_MINIO_ROOT_PASSWORD:-xynminio123}" \
  -e XYN_RUNTIME_ARTIFACT_S3_BUCKET="${XYN_RUNTIME_ARTIFACT_S3_BUCKET:-xyn-runtime-artifacts}" \
  -e XYN_RUNTIME_ARTIFACT_S3_PREFIX="${XYN_RUNTIME_ARTIFACT_S3_PREFIX:-xyn/runtime}" \
  minio/mc:RELEASE.2025-03-12T17-29-24Z -lc '
    mc alias set local http://minio:9000 "$XYN_MINIO_ROOT_USER" "$XYN_MINIO_ROOT_PASSWORD" >/dev/null;
    mc ls --recursive "local/$XYN_RUNTIME_ARTIFACT_S3_BUCKET/$XYN_RUNTIME_ARTIFACT_S3_PREFIX" || true
  '

echo "[runtime-s3] Validation complete."
