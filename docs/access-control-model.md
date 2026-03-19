# Platform Access Control Model (Compatibility Notes)

Canonical DealFinder-era role/capability definitions now live in `xyn-platform`:

- [platform-access-control-model.md](/home/jrestivo/src/xyn-platform/docs/platform-access-control-model.md)
- `services/xyn-api/backend/xyn_orchestrator/app_authorization.py`

## Scope

This file documents how `xyn/core` compatibility surfaces align with the canonical model.

- Role checks should map to capabilities.
- Endpoint/service enforcement should check capabilities, not role-name conditionals.
- Scope is workspace-first, with optional application scope.

## Canonical Roles

- `application_admin`
- `campaign_operator`
- `read_only_analyst`

## Capability Families

For `xyn/core` compatibility APIs, capability slugs are mirrored in [core/access_control.py](/home/jrestivo/src/xyn/core/access_control.py).

Admin/data-source capabilities include:

- `app.jurisdictions.manage`
- `app.sources.manage`
- `app.mappings.inspect`
- `app.refreshes.run`
- `app.ingest_runs.read`
- `app.failures.read`
- `app.artifacts.read`
- `app.provenance.read`
- `app.datasets.publish`
- `app.sources.diagnostics.read`

Campaign/operator capabilities include:

- `app.campaigns.manage`
- `app.watches.manage`
- `app.subscribers.manage`
- `app.notification_targets.manage`
- `app.matches.review`
- `app.signals.review`
- `app.campaign_history.read`
- `app.notifications.read`

Read-only analyst baseline includes:

- `app.read`
- read-oriented run/failure/campaign/signal/match/notification capabilities

## Reusable Enforcement Pattern

1. Resolve principal from request headers (`X-Roles`, `X-Capabilities`, optional scope headers).
2. Resolve effective capabilities from role mappings.
3. Call `assert_access(...)` with required capabilities and scope.
4. Return `403` on missing capability or out-of-scope access.

For FastAPI routes, use `require_capabilities(...)` where practical.

## Visibility Endpoints

- `GET /api/v1/access/roles` (role -> capability catalog)
- `GET /api/v1/access/me` (effective principal view)

## Capability-to-Endpoint Mapping (Current Backend)

The current `xyn` repo does not yet expose dedicated DealFinder primitives like
campaign/watch/source-connector endpoints. Enforcement is applied to the nearest
platform surfaces in this repo:

- Source/admin-like surfaces (`application_admin`):
  - `/api/v1/artifact-registries*`
  - `/api/v1/workspaces/{workspace_slug}/artifact-registry*`
  - `/api/v1/artifacts/refresh`
  - `/api/v1/releases*`
  Required capability: `app.sources.manage` or `app.datasets.publish`/`app.refreshes.run` depending on route.
- Run/ingest/failure read surfaces (`application_admin`, `campaign_operator`, `read_only_analyst`):
  - `/api/v1/runs*` reads, `/api/v1/jobs*` reads, `/api/v1/events*` reads
  - `/api/v1/ops/*`, `/api/v1/packs*` reads
  Required capability: `app.ingest_runs.read` and/or `app.failures.read`.
- Campaign/operator mutation-like surfaces (`application_admin`, `campaign_operator`):
  - `/api/v1/drafts*` writes
  - `/api/v1/locations*` writes
  - `/api/v1/palette/commands*` writes
  - `/api/v1/domain/sites|customers` writes
  Required capability: `app.campaigns.manage`.
- Read-only analyst read surfaces:
  - `/api/v1/drafts`/`/api/v1/locations`/`/api/v1/primitives`/lifecycle reads
  Required capability: `app.read`.

## Notes

- `xyn/core` is not the canonical home for DealFinder-era platform/app authorization.
- This layer is compatibility/runtime-only for core-local surfaces.
- Fine-grained row-level policy and dynamic role assignment UX are follow-on work.
