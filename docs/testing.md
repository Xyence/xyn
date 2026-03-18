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
