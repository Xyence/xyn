# Xyn Roadmap

## Phase 1: Network Inventory Foundation

Goal: convert natural-language app requests into structured drafts and queued jobs that can produce deployable specs.

Scope:
- Workspace-scoped drafts (`app_intent`) with review/edit lifecycle.
- Workspace-scoped jobs (`generate_app_spec`, `deploy_app_local`, `smoke_test`) with status + logs.
- Submit flow: draft -> queued job.
- Seed defaults: guaranteed default workspace bootstrap.
- Drafts UI in xyn-ui for create/edit/submit + jobs visibility.
- AppSpec v0 generation and persistence as job output/artifact.
- Shared primitive catalog with reusable `location` primitive.
- Local net-inventory materialization via docker-compose.
- Sibling Xyn provisioning + smoke-test execution against deployed app.

Outcomes:
- API-first workflow for "build an app" intake.
- Persistent traceability from draft through job execution.

## Phase 2 Backlog

- TR-369 / EMS domain modeling and schema pack integration.
- EMS inventory synchronization and reconciliation jobs.
- Rich draft validation and policy checks before submit.
- Artifact binding from jobs to release/runtime specs and rollout workflows.

## Phase 2 Hardening (Current)

- Lightweight endpoint contracts for core API and net-inventory API.
- Contract validator tooling (`scripts/validate_contracts.py`) and end-to-end harness (`scripts/run_e2e_validation.sh`).
- Palette command registry (`palette_commands`) with workspace override + global fallback.
- Workspace isolation tests for palette/device flows.
- Persistence checks across net-inventory service and database restarts.
- Artifact refresh smoke path validation.

## Epic C Follow-ons

Epic C is complete enough to close. The items below are explicit follow-on work, not blockers for Epic C completion.

- Bridge remaining non-codegen task types into Epic C runtime submission. Codegen `DevTask` execution now uses typed Epic C runs; release-plan, deploy-oriented, and any other remaining legacy execution-backed task types should migrate to worker-appropriate Epic C submission paths without forcing them through `codex_local` or reintroducing dual execution truth.
- Resolve the Django test DB issue blocking automated runtime-bridge regression coverage. Investigate and fix `django.db.utils.OperationalError: cannot CREATE INDEX "xyn_orchestrator_modelconfig" because it has pending trigger events` so `xyn_orchestrator` DevTask-to-runtime bridge tests can run reliably in automation.
- Clarify authoritative execution reference semantics in docs and code comments. Document that `runtime_run_id` and `runtime_workspace_id` are authoritative for bridged Epic C-backed `DevTask`s, while legacy `result_run` is compatibility-only for non-bridged paths.
- Preserve and document `DevTask` idempotency and retry semantics. Active bridged runs should continue returning the existing Epic C run on repeated submission attempts, while retry after a terminal state should create a new Epic C run and update the authoritative runtime reference.
- Review the broader `DevTask` UX after runtime migration settles. Once more task types are Epic C-backed, revisit task list/detail/operator affordances so queued, running, blocked, retry, artifact, and runtime-reference states are clearer without changing them in the current Epic C close-out.

## Epic E Follow-ons

Epic E is complete enough to close. The items below are explicit follow-on work, not blockers for Epic E completion.

- Clean up the broader console test harness so the remaining React `act(...)` warning noise is reduced without changing Epic E behavior.
- Add and maintain a small acceptance matrix note mapping the three supported Epic E prompt families to their affordance coverage and backend enforcement points.
- Watch for resolve/apply semantic drift over time by keeping `PromptInterpretation`, `execution_mode`, and clarification semantics centralized.

Broader post-Epic-E UX work:
- Consider a future rich-input architecture if true inline token highlighting becomes worth the complexity.
- Migrate additional preserved legacy non-Epic-D prompt paths onto the `PromptInterpretation` contract when those paths are modernized.
- Revisit broader console affordances later for predictive help, suggestions, or advanced editing only after later epic priorities justify it.
- Improve full-stack DB-backed and broader console-suite stability so future UX epics can rely on wider regression coverage.
