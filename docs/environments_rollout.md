# Environments/Siblings/Activations Rollout Notes

This repository now includes Phase 0-3 environment model work.

## Required migration state

- Migration `014_environments_siblings_activations.sql` must be applied before enabling Phase 1+ APIs.
- Tables required:
  - `environments`
  - `siblings`
  - `activations`

## Feature status

- Ready for controlled rollout:
  - Phase 0 write-through state
  - Phase 1 environment/sibling read-control APIs
  - Phase 2 artifact activation API (`activate-artifact`)
- Experimental / rollout with caution:
  - Phase 3 external DB tenancy allocator (`XYN_DB_MODE=external`)

## DB mode controls

### Local default (unchanged behavior)

- `XYN_DB_MODE=local`
- Uses compose-managed Postgres in provisioned sibling stacks.
- DB allocator is a no-op.

### External shared Postgres mode

- `XYN_DB_MODE=external`
- `XYN_DB_TENANCY_MODE=shared_rds_db_per_sibling`
- `DATABASE_URL` should point sibling runtime to external DB host pattern (compose resolves from injected env).
- `XYN_DB_BOOTSTRAP_DATABASE_URL` must be set on the control plane.

`XYN_DB_BOOTSTRAP_DATABASE_URL` is used only for allocation operations (create role/database/grants). It must not be provided to runtime sibling containers.

## Security expectations

- Admin/bootstrap DB credentials are never persisted to:
  - `siblings` rows
  - `activations` rows
  - job output payloads
  - API provision outputs
- Persist only non-secret allocation metadata (mode, tenancy_mode, db name/user, host/port).
- Runtime receives only scoped per-sibling `DATABASE_URL`.

## Failure modes

- External mode with missing bootstrap URL fails safe and clearly.
- Allocation errors fail sibling provisioning path and activation follows existing failed transition path.
- Local mode remains unaffected when external settings are absent.

## Known caveats

- Current external tenancy implementation supports only:
  - `shared_rds_db_per_sibling`
- No automatic teardown/garbage-collection of external tenant DBs yet.
- Promotion, solution activation, and drift reconciliation are intentionally out of scope for this pass.
