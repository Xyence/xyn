# Runtime Execution

This document describes the v1 runtime execution contract for Epic C.

## Status

Epic C is complete enough to close. The runtime execution contract, worker loop, `codex_local` execution path, runtime visibility, and the initial `DevTask` bridge for code generation are in place.

The remaining work for Epic C is follow-on migration and cleanup, not a blocker for closing the epic. See [`docs/roadmap.md`](./roadmap.md) for the explicit residual backlog items.

## Contract locations

- Run payload schema: [`core/runtime_contract.py`](../core/runtime_contract.py)
- Worker contract: [`core/runtime_contract.py`](../core/runtime_contract.py)
- Runtime dispatch/lifecycle: [`core/runtime_execution.py`](../core/runtime_execution.py)
- Worker registry: [`core/runtime_workers.py`](../core/runtime_workers.py)
- Runtime event publication: [`core/runtime_events.py`](../core/runtime_events.py)

## Lifecycle states

Allowed `Run.status` values:

- `queued`
- `running`
- `completed`
- `failed`
- `blocked`

`RunStep.status` values follow the runtime step flow and are emitted through the same event ledger.

## Runtime event types

Runtime execution emits machine-readable events through the existing `Event` table:

- `run.started`
- `run.step.started`
- `run.step.completed`
- `run.step.failed`
- `run.completed`
- `run.failed`
- `run.blocked`
- `run.artifact.created`
- `run.heartbeat`

Each runtime event includes run context when available:

- `run_id`
- `work_item_id`
- `worker_type`
- `workspace_id`
- `repo`
- `branch`

## Streaming delivery

The `Event` ledger remains the authoritative runtime event source. Streaming is a delivery layer on top of the ledger.

### Transport

Runtime visibility uses Server-Sent Events (SSE).

### Endpoints

- `xyn-core`: `GET /api/v1/events/stream`
- `xyn-api`: `GET /xyn/api/ai/activity/stream`

`xyn-api` is the workspace-scoped stream exposed to the UI. It proxies `xyn-core` ledger events and applies the authoritative runtime activity envelope mapping.

### Stream envelope

The UI-facing stream envelope includes:

- `event_id`
- `event_type`
- `created_at`
- `workspace_id`
- `run_id`
- `work_item_id`
- `worker_type`
- `status`
- `title`
- `message`
- `payload`

### Resume and reconnect

- Clients connect from `now` by default.
- Clients may resume with `last_event_id` or `since`.
- The browser stream uses reconnect with the last seen event id.
- Initial panel/activity fetch remains the canonical history/bootstrap path.

### Fallback

If streaming is unavailable, UI runtime surfaces fall back to slower periodic refresh. Polling is degraded-mode behavior, not the primary freshness path.

## Artifact types

Allowed runtime artifact types:

- `patch`
- `log`
- `report`
- `code`
- `summary`

The current local URI convention is deterministic:

- `artifact://runs/{run_id}/patch.diff`
- `artifact://runs/{run_id}/build_logs.txt`
- `artifact://runs/{run_id}/final_summary.md`
- `artifact://runs/{run_id}/test_report.json`

## Submission and worker loop

- Typed runtime submission endpoint: `POST /api/v1/runtime/runs`
- Payload validation contract: [`core/runtime_contract.py`](../core/runtime_contract.py)
- Background runtime loop: [`core/runtime_loop.py`](../core/runtime_loop.py)
- The runtime loop registers `codex_local`, heartbeats the worker, dispatches queued runs, executes assigned runs, and monitors stale running runs.
- Coordination systems should pass only execution-ready payloads plus a stable `work_item_id`; planning state remains outside Epic C models.

## codex_local availability

- Production adapter: [`core/codex_executor.py`](../core/codex_executor.py)
- `xyn-core` installs the Codex CLI during container build.
- Availability is checked explicitly with `codex --help` before worker registration and before direct `CliCodexExecutor` use.
- If Codex is unavailable:
  - `codex_local` registers as `offline`
  - the dispatcher will not assign queued runs to it
  - the runtime loop stays alive and logs the offline condition
  - `/api/v1/ops/workers` includes a `health_reason`

Manual validation:

- `docker exec xyn-core which codex`
- `docker exec xyn-core codex --help`
- `docker exec xyn-core python -m unittest core.tests.test_codex_local_worker core.tests.test_runtime_loop`

## target.repo semantics

- Canonical `target.repo` values are logical repo keys resolved through [`core/repo_resolver.py`](../core/repo_resolver.py)
- Default runtime repo map:
  - `xyn -> /workspace/xyn`
  - `xyn-platform -> /workspace/xyn-platform`
- The map can be overridden with `XYN_RUNTIME_REPO_MAP`
- The xyn-platform mount source can be overridden with `XYN_PLATFORM_HOST_SRC_PATH` (host path -> `/workspace/xyn-platform`)
- Startup validation mode is controlled by `XYN_RUNTIME_REPO_MAP_VALIDATION`:
  - `warn` (default): log warnings when configured targets are missing
  - `fail`: abort startup when configured targets are missing or map JSON is invalid
  - `off`: disable startup validation
- Absolute paths are allowed as explicit local overrides for tests/dev execution
- Repo resolution validates:
  - path exists
  - path is a directory
  - path is a git repository
  - path is readable/executable by runtime user

Runtime deployment requirement for source review:

- Ensure your deployment mounts the source checkout/mirror paths referenced by `XYN_RUNTIME_REPO_MAP`.
- Default expected mounts inside `xyn-core`:
  - `/workspace/xyn`
  - `/workspace/xyn-platform`

Failure semantics:

- blocked:
  - missing repo key
  - ambiguous repo map entry
  - branch mismatch
  - unsafe dirty repository state
- failed:
  - repo key not configured
  - repo path missing/unmounted
  - repo path is not a git repository

## Failure reasons

Documented failure reasons for v1:

- `worker_crashed`
- `worker_unresponsive`
- `repo_unreachable`
- `tests_failed`
- `contract_violation`
- `unexpected_error`
- `timeout_exceeded`

Blocked runs should populate `escalation_reason` instead of overloading `failure_reason` when a human decision is required.
