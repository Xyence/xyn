## Context-Pack Bridge

Authoritative source
- `xyn-platform` remains the source of truth for context-pack identity and governance.
- The authoritative input currently comes from:
  - `services/xyn-api/backend/seeds/xyn-core-context-packs.v1.2.0.json`
  - `services/xyn-api/backend/xyn_orchestrator/models.py`
  - `services/xyn-api/backend/xyn_orchestrator/artifact_links.py`

Runtime consumption
- `xynctl` exports a runtime manifest from `xyn-platform` into:
  - `.xyn/sync/context-packs.manifest.json`
- `xyn-core` consumes that synced manifest at startup and upserts `Artifact(kind="context-pack")` rows.
- Runtime APIs remain:
  - `/api/v1/context-packs`
  - `/api/v1/context-packs/bindings`

Binding model
- Runtime binding remains explicit and artifact-based.
- Workspace bindings live in `workspace_settings.default_context_pack_artifact_ids_json`.
- If a workspace has no explicit binding row yet, `xyn-core` uses only manifest entries marked `bind_by_default=true`.

Why this is transitional
- `xyn-core` is still consuming a synchronized manifest, not a published/imported artifact package.
- `xyn-platform` still owns governance; `xyn-core` owns runtime binding and consumption.

Next required step
- Replace manifest sync with a promoted artifact publish/import path so sibling Xyn instances consume published/synced context-pack artifacts instead of a local runtime manifest.
