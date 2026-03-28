#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

export XYN_DB_TEST_POLICY="${XYN_DB_TEST_POLICY:-required}"

echo "[db-tests] policy=${XYN_DB_TEST_POLICY}"

python -m unittest -q \
  core.tests.test_db_requirements \
  core.tests.test_appspec_hybrid_inference.AppSpecHybridInferencePersistenceIntegrationTests \
  core.tests.test_generic_app_builder \
  core.tests.test_capability_manifest
