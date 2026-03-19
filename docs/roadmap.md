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
- Add structured filter support for artifact and list-style prompts. Prompts like `created two days ago`, `created yesterday`, or `with status draft` currently do not flow through a canonical filter model. The proper fix is to add structured filter extraction in intent resolution, carry those filters in the canonical direct/list action envelope, apply them in backend list handlers, and add regression coverage for supported-vs-unsupported filter semantics.

Broader post-Epic-E UX work:
- Consider a future rich-input architecture if true inline token highlighting becomes worth the complexity.
- Migrate additional preserved legacy non-Epic-D prompt paths onto the `PromptInterpretation` contract when those paths are modernized.
- Revisit broader console affordances later for predictive help, suggestions, or advanced editing only after later epic priorities justify it.
- Improve full-stack DB-backed and broader console-suite stability so future UX epics can rely on wider regression coverage.

## Epic F Follow-ons

Epic F is complete enough to close. The items below are explicit follow-on work, not blockers for Epic F completion.

- Keep the run-control phrase list and worker mention aliases aligned with Epic D tests as additional worker types are introduced, so conversation remains the front door to the one orchestration/runtime path instead of drifting into ad hoc command handling.
- Add and maintain a small acceptance matrix mapping conversation action families to their runtime, escalation, and execution-summary coverage points.
- Watch for semantic drift between `ConversationAction`, `PromptInterpretation`, and runtime event summaries so conversation supervision stays machine-readable and auditable.

Broader post-Epic-F work:
- Migrate more preserved legacy prompt paths onto the conversation action seam when those paths are modernized, rather than leaving long-term mixed conversational execution semantics.
- Revisit broader conversation ergonomics only after later execution/review epics define stronger operator workflows.
- Improve wider DB-backed and console integration stability so future conversational supervision work can rely on broader end-to-end regression coverage.

## Epic G Follow-ons

Epic G follow-on workspace behavior is complete enough to ship its current policy seam. The items below are explicit follow-on work, not blockers for the current panel architecture.

- Clean up the remaining `xynConsoleStore.test.tsx` Vitest process-lifecycle issue so workspace and panel policy changes can be covered at the store seam without hanging worker processes.
- Consider an admin-managed preset configuration path for application view/layout presets once the code-defined default preset seam has proven stable in daily use.

## Epic H Follow-ons

Epic H is complete enough to close. The items below are explicit follow-on work, not blockers for the current durable coordination artifact seam.

- Resolve the unrelated migration drift around `0113_alter_videorender_status.py` so targeted Django validation no longer reports unrelated model/migration mismatch noise during coordination-artifact work.
- Tighten activity drawer click-through into work-item panels so conversation/activity references can open the corresponding durable coordination objects more directly.
- Evaluate later whether `DevTask` should remain the long-term durable `WorkItem` substrate or be renamed/reified more explicitly once XCO-level coordination expands.
- Evaluate later whether `RunStep` exposure in the UI should become richer, but only after there is a demonstrated supervisory need.

## Rules Browser Follow-ons

- Add explicit per-workspace/policy-bundle permission checks in the backend rule query path so multi-tenant visibility guarantees do not rely only on metadata and platform-admin gating.

## Geospatial Primitive Follow-ons

- Add CI/runtime smoke coverage that asserts PostGIS remains available/installed after compose/database image changes, so spatial primitives do not silently regress to non-PostGIS runtimes.
- Add a small non-Django consumer example (service/repository usage from FastAPI-side code) to keep the geospatial primitive framework-neutral in practice, not just in type contracts.
- Add optional nearest-neighbor and advanced reprojection helpers after current PostGIS-backed bbox/polygon/distance baseline is stable in operator workflows.

## Lifecycle Primitive Follow-ons

- Integrate additional lifecycle-heavy models (for example pack installations and connector-like objects) onto the shared lifecycle service instead of ad hoc status writes.
- Add optional policy-driven transition hooks for notifications/escalations while preserving deterministic guard checks in the lifecycle service.
- Add lightweight operator UI affordances for object transition history filtering and manual transition actions where safe.
- Continue reducing `xyn/core` lifecycle compatibility code by consuming canonical lifecycle definitions/services from `xyn-platform` where runtime coupling allows.

## Access Control Follow-ons

- Add durable role-assignment persistence and admin management UI for `application_admin`, `campaign_operator`, and `read_only_analyst` instead of header-driven local/development claims.
- Add fine-grained row/partition policy checks (for example jurisdiction-level scope constraints) in service/repository paths once DealFinder data partition semantics are finalized.
- Add standardized authn claim adapters for OIDC/token modes so capability claims are resolved from identity provider or issued platform tokens, not request-header conventions.
- Keep `xyn/core` access-control usage compatibility-only and defer canonical DealFinder-era role/capability evolution to `xyn-platform` (`xyn_orchestrator.app_authorization`).
