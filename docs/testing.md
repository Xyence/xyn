# Testing

## Full E2E Harness

Run:

```bash
scripts/run_e2e_validation.sh
```

The harness validates:

1. Core API endpoint contracts
2. net-inventory API endpoint contracts
3. Workspace isolation for palette/device listing
4. Persistence after API and DB container restarts
5. Palette command registration/execution (`show devices`)
6. Artifact refresh/self-update smoke path

## Notes

- The harness expects a running seed stack (`./xynctl quickstart`).
- If no successful app deployment exists, the harness creates/submits an app-intent draft and waits for the job chain to complete.
- Output ends with a PASS/FAIL summary suitable for CI/local gating.

## Runtime Artifact Storage (MinIO/S3)

Run:

```bash
scripts/validate_runtime_s3_minio.sh
```

This validation brings up the stack with `compose.minio.yml`, configures runtime artifact storage with `XYN_RUNTIME_ARTIFACT_PROVIDER=s3`, and verifies end-to-end artifact round-trip behavior through MinIO for:

1. Generic artifact API write/read
2. Step log artifact capture
3. Runtime execution artifact write/read
4. Object presence in MinIO under the configured runtime prefix

Success indicators:

- The unittest `core.tests.test_runtime_s3_minio_integration` reports `OK`.
- The script prints MinIO object keys under the runtime prefix.
- Output ends with `[runtime-s3] Validation complete.`

If it fails:

- Inspect `xyn-core` logs: `docker logs xyn-core`
- Inspect MinIO logs: `docker logs xyn-minio`
- Confirm the runtime provider env values passed in `compose.minio.yml`.

## DB-Backed Test Policy

Some tests exercise real Postgres persistence paths and should not silently
degrade in CI.

- Policy control:
  - `XYN_DB_TEST_POLICY=required|optional`
  - `XYN_REQUIRE_DB_TESTS=true` (equivalent to `required`)
  - `CI=true` defaults to `required` when `XYN_DB_TEST_POLICY` is unset
- Behavior:
  - `required`: DB-backed tests fail loudly if Postgres/schema readiness is not met
  - `optional` (local default): DB-backed tests may skip with explicit reason

Current DB-backed diagnostics integration tests use this policy helper:
- [db_requirements.py](/home/jrestivo/src/xyn/core/tests/db_requirements.py)

Recommended CI posture:
- Ensure Postgres is reachable and schema-ready before running DB-backed suites.
- Run with `XYN_DB_TEST_POLICY=required` so regressions are not masked by skips.

Deterministic CI bootstrap path:
- Schema bootstrap script: [bootstrap_db_for_tests.sh](/home/jrestivo/src/xyn/scripts/bootstrap_db_for_tests.sh)
- Required-mode DB suite runner: [run_db_backed_required_tests.sh](/home/jrestivo/src/xyn/scripts/run_db_backed_required_tests.sh)
- CI workflow: [db-backed-tests.yml](/home/jrestivo/src/xyn/.github/workflows/db-backed-tests.yml)

Local equivalent (with Postgres running and `psql` available):
```bash
export DATABASE_URL=postgresql://xyn:xyn_dev_password@localhost:5432/xyn
export XYN_DB_TEST_POLICY=required
./scripts/bootstrap_db_for_tests.sh
./scripts/run_db_backed_required_tests.sh
```
