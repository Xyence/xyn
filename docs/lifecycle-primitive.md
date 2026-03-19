# Lifecycle Primitive (Core Compatibility Layer)

## Canonical boundary

The canonical platform lifecycle/state-machine primitive now lives in `xyn-platform`:
- [`docs/platform-lifecycle-primitive.md`](../../xyn-platform/docs/platform-lifecycle-primitive.md)
- `services/xyn-api/backend/xyn_orchestrator/lifecycle_primitive/`
- `xyn_orchestrator.models.LifecycleTransition`

## Why this exists in `xyn/core`

`xyn/core` still has local object integrations (`Draft`, `Job`) and keeps a thin compatibility adapter in `core/lifecycle/` so those flows continue to work safely.

This layer is integration-only, not canonical platform infrastructure.

## Rule for new work

Do not add new generic lifecycle logic to `xyn/core`.
Add lifecycle definitions/semantics in `xyn-platform`, then consume from core via adapter paths as needed.
