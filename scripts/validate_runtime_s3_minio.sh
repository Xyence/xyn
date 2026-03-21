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
docker compose -f compose.yml -f compose.minio.yml up -d --build

echo "[runtime-s3] Waiting for xyn-core health..."
healthy=0
for i in {1..180}; do
  if docker exec -i xyn-core python - <<'PY' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://localhost:8000/health", timeout=3)
print("ok")
PY
  then
    healthy=1
    break
  fi
  sleep 2
done
if [[ "$healthy" -ne 1 ]]; then
  echo "[runtime-s3] ERROR: xyn-core did not become healthy in time."
  docker compose -f compose.yml -f compose.minio.yml ps || true
  docker compose -f compose.yml -f compose.minio.yml logs --tail=200 core minio minio-init postgres redis || true
  exit 1
fi

echo "[runtime-s3] Ensuring runtime schema exists for integration test..."
docker exec -i xyn-core python - <<'PY'
from sqlalchemy import inspect

from core.database import Base, engine
from core import models  # noqa: F401 - registers ORM models on Base metadata

Base.metadata.create_all(bind=engine)
tables = set(inspect(engine).get_table_names())
required = {"artifacts", "runs", "steps", "events"}
missing = sorted(required - tables)
print(f"tables={sorted(tables)}")
if missing:
    raise SystemExit(f"Missing required runtime tables after bootstrap: {missing}")
PY

echo "[runtime-s3] Running runtime S3 integration tests..."
docker exec \
  -e XYN_RUNTIME_ARTIFACT_PROVIDER=s3 \
  -e XYN_RUNTIME_ARTIFACT_S3_BUCKET="${XYN_RUNTIME_ARTIFACT_S3_BUCKET:-xyn-runtime-artifacts}" \
  -e XYN_RUNTIME_ARTIFACT_S3_REGION="${XYN_RUNTIME_ARTIFACT_S3_REGION:-us-east-1}" \
  -e XYN_RUNTIME_ARTIFACT_S3_PREFIX="${XYN_RUNTIME_ARTIFACT_S3_PREFIX:-xyn/runtime}" \
  -e XYN_RUNTIME_ARTIFACT_S3_ENDPOINT_URL="${XYN_RUNTIME_ARTIFACT_S3_ENDPOINT_URL:-http://minio:9000}" \
  -e XYN_RUNTIME_ARTIFACT_S3_ACCESS_KEY_ID="${XYN_RUNTIME_ARTIFACT_S3_ACCESS_KEY_ID:-${XYN_MINIO_ROOT_USER:-xynminio}}" \
  -e XYN_RUNTIME_ARTIFACT_S3_SECRET_ACCESS_KEY="${XYN_RUNTIME_ARTIFACT_S3_SECRET_ACCESS_KEY:-${XYN_MINIO_ROOT_PASSWORD:-xynminio123}}" \
  -e XYN_RUNTIME_ARTIFACT_S3_FORCE_PATH_STYLE="${XYN_RUNTIME_ARTIFACT_S3_FORCE_PATH_STYLE:-true}" \
  xyn-core \
  python -m unittest -v core.tests.test_runtime_s3_minio_integration

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
