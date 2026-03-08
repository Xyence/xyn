# Demo Objective

The demo must reliably show Xyn bootstrapping a fresh instance, landing in a usable workspace, creating a simple network inventory capability through the prompt-driven app builder, deploying it into the Xyn runtime shell, and operating it through the palette and artifact/report surfaces, while visibly using governed artifacts and context packs without critical legacy UI leaks or bootstrap failures.

# Golden Path

1. Launch a fresh Xyn instance with `./xynctl quickstart`
2. Login successfully in dev mode
3. Land in an accessible workspace automatically
4. Open the palette successfully from the default workbench
5. Prompt: build a network inventory app for the current workspace
6. Draft is created and submitted
7. AppSpec is generated
8. Local deployment succeeds
9. Build tracking surface shows deployment status and the installed capability inside Xyn
10. Installed capability is visible and usable from within Xyn
11. Open the sibling Xyn instance successfully
12. Palette commands such as `show devices` and `show devices by status` operate the installed capability and return visible results
13. Context packs and artifacts are visible through runtime APIs
14. No critical legacy UI leaks appear on the demo path

# Do Not Break List

- Context packs must resolve through explicit artifact bindings
- Palette execution must return context metadata (`context_pack_artifact_ids`, `context_pack_slugs`)
- Runtime storage must remain under `.xyn/` and not use repo-root runtime directories
- Artifact inventory must expose context-pack artifacts
- Fresh instance startup must not inherit old repo-root generated artifacts
- Workspace bootstrap must reliably produce an accessible workspace
- The demo golden path defined in `DEMO_READINESS.md` must continue to work

# Active Demo Blockers

DEMO-01  
Title: Workspace bootstrap reliability  
Description: Fresh dev instances must always land in an accessible workspace without stale workspace redirects or manual recovery.  
Impact on Demo: Prevents the demo from starting cleanly.  
Current Status: partially addressed  
Next Action: run repeated clean-start validation and verify Development/default workspace behavior remains deterministic.

DEMO-02  
Title: Installed-capability semantics need to stay consistent  
Description: The demo must keep Xyn as the primary runtime shell and demonstrate that deployment installs a new capability into Xyn, rather than implying a separate arbitrary frontend is the main user surface.  
Impact on Demo: Weakens the platform story if the demo appears to leave Xyn to use the generated capability.  
Current Status: partially addressed  
Next Action: keep the Draft Detail surface and browser rehearsal centered on installed capability visibility, palette operation, and artifacts/reports inside Xyn.

DEMO-03  
Title: Sibling Xyn does not yet install the generated app as an artifact  
Description: The sibling instance is provisioned, but the generated app is not yet consumed through an artifact install flow inside that sibling.  
Impact on Demo: Breaks the architectural claim that everything is an artifact and risks confusion during the demo.  
Current Status: open  
Next Action: implement the smallest publish/import/install step for the generated app before demo freeze.

DEMO-04  
Title: Legacy UI leakage remains possible on parts of the demo path  
Description: Some navigation flows still risk falling into legacy Django-era surfaces or behaviors instead of the intended workbench experience.  
Impact on Demo: Creates visible inconsistency and undermines confidence.  
Current Status: partially addressed  
Next Action: explicitly validate the golden path routes and remove or guard remaining legacy entrypoints relevant to the demo.

DEMO-05  
Title: Demo-path status visibility is still thin  
Description: Draft, build, deployment, and sibling progress are visible, but success/failure signaling is not yet unified enough across activity, notifications, and build tracking.  
Impact on Demo: Makes it harder to explain what the system is doing during the live flow.  
Current Status: partially addressed  
Next Action: tighten the build-status presentation on the draft tracking surface and ensure clear success/failure signals. Notifications already expose app-build completion/failure, and Draft Detail now exposes in-shell capability and generated-artifact paths, but the overall story still depends on several separate surfaces.

# Active Tasks

TASK-01  
Task Name: Lock down fresh-instance bootstrap path  
Why It Matters for Demo: The demo cannot proceed if login lands in an inaccessible or stale workspace.  
Owner: Codex  
Status: in-progress  
Notes: Must include clean-start validation after teardown. Fresh browser validation on 2026-03-08 against a clean `xynctl quickstart` path confirmed that the active workspace loads successfully, the visible capabilities count resolves to `2`, and the artifact list no longer shows the legacy demo artifacts `ems`, `hello-app`, `articles-tour`, `platform-build-tour`, `deploy-subscriber-notes`, or `subscriber-notes-walkthrough`.

TASK-02  
Task Name: Keep deployment semantics honest inside Xyn shell  
Why It Matters for Demo: The demo should show that deployment installs new capability into Xyn rather than sending the user to a disconnected frontend.  
Owner: Codex  
Status: in-progress  
Notes: Build tracking should show the installed capability, available palette actions, and visible report/query outcomes inside Xyn.

TASK-03  
Task Name: Bridge generated app into sibling Xyn install story  
Why It Matters for Demo: The sibling instance must demonstrate artifact-aware installation rather than disconnected provisioning.  
Owner: Codex  
Status: pending  
Notes: Keep scope minimal; do not overreach into the full publish/import system unless required.

TASK-04  
Task Name: Stabilize prompt-driven build tracking UX  
Why It Matters for Demo: The operator must be able to understand whether the build succeeded and where to go next.  
Owner: Codex  
Status: in-progress  
Notes: Track build should surface status, installed capability visibility inside Xyn, sibling target, and failures clearly. The current Draft Detail view now exposes an installed-capability card, execution trace, sibling CTA, palette-oriented usage guidance for the generated network inventory capability, and a direct in-shell "View generated artifacts" path for demo step 8. Local reprovisioning now explicitly pulls and recreates remote `:dev` images so browser-facing UI changes are not silently masked by stale containers, and repeated demo builds now reuse a stable `xyn-app-net-inventory` compose project to avoid Docker network exhaustion during rehearsals. Fresh browser validation on 2026-03-08 confirmed visible success for `show devices`, `show locations`, `create device`, and `show devices by status`, and the sibling Xyn capability indicator also resolved to `2` instead of `0`.

TASK-05  
Task Name: Validate context-pack authority bridge live  
Why It Matters for Demo: Context packs must appear governed and runtime-visible without split-brain confusion.  
Owner: Codex  
Status: complete  
Notes: Runtime inventory now reports `source_authority=xyn-platform` and uses the synced manifest.

TASK-06  
Task Name: Guard demo path against legacy UI leaks  
Why It Matters for Demo: Demo users should not see inconsistent old/new surfaces in the core path.  
Owner: Codex  
Status: pending  
Notes: Focus on workbench, platform settings, drafts, build tracking, and app open flow.

TASK-07  
Task Name: Prepare repeatable golden-path validation script/checklist  
Why It Matters for Demo: Fast regression detection is required during the demo-prep window.  
Owner: Codex  
Status: complete  
Notes: Added a browser-driven Playwright smoke test in `xyn-platform/apps/xyn-ui/e2e/demo-golden-path.spec.ts` and a one-command runner in `scripts/run_demo_rehearsal.sh`. The rehearsal captures step-labeled screenshots, browser logs, and Playwright report output under `.xyn/demo-rehearsal/<timestamp>/`. Validated locally on 2026-03-07 against the visible UI path with `./scripts/run_demo_rehearsal.sh`; latest confirmed evidence bundle: `.xyn/demo-rehearsal/20260307-115802/`. The smoke covers login, build prompt submission, draft creation, build tracking, execution trace visibility, deployed app open action, palette command submission, and artifact visibility for generated `app_spec` artifacts.

TASK-08  
Task Name: Expose execution-trace proof point on build tracking surface  
Why It Matters for Demo: The demo needs a visible proof that non-trivial generation work records findings, proposed fixes, validation, and transitional notes durably.  
Owner: Codex  
Status: complete  
Notes: Implemented as a compact Execution Trace card on Draft Detail backed by same-origin `xyn-api` execution-note proxy endpoints. The card only renders notes that are explicitly linked to the current build chain and otherwise shows an empty linked-state message instead of guessing. Validated on 2026-03-07 with an authenticated draft submission against the `default` workspace: the proxy returned execution-note `54b31828-00fd-4311-97d7-c7d86ed0c4db` matched by `related_artifact_ids` for generated AppSpec `68995707-552b-4f5b-bb4a-ffa3bbe34eb4`.

DEMO-PROTO-01  
Title: Findings-First Implementation Protocol  
Description: Xyn now records execution/design notes for non-trivial generation and artifact modification tasks.  
Why It Matters: reduces architectural drift and provides shared reasoning context between automated development steps.  
Owner: Codex  
Status: implemented  
Notes: Initial implementation is runtime-only and currently hooks into the app-builder generation pipeline through `execution-note` artifacts. Validated on 2026-03-07: migrations no-op cleanly, `xynctl quickstart --force` booted successfully, context-pack APIs and artifact inventory responded, palette metadata remained intact, app generation succeeded, and execution-note artifact `c105190a-92f4-49fc-b81d-43723004e18b` completed with validation details. The Draft Detail surface now renders a minimal Execution Trace card from stored execution-note records through a same-origin proxy when an explicit build-chain match exists.

# Validation Checklist

- fresh instance boots cleanly
- migrations apply on a clean database
- login succeeds in dev mode
- accessible workspace loads automatically
- no stale workspace redirect loop
- context packs visible via `/api/v1/context-packs`
- context-pack bindings visible via `/api/v1/context-packs/bindings`
- artifact inventory includes context-pack artifacts with correct authority metadata
- palette execution returns explicit context-pack metadata
- build-app prompt creates and submits a draft
- AppSpec generation succeeds
- local deployment succeeds
- deployed app opens via the intended user-facing entry route
- sibling Xyn instance provisions successfully
- sibling install story is coherent for the demo path
- `show devices` returns structured results
- no critical legacy UI leaks on the golden path
- browser-driven golden-path smoke passes against the visible UI, not just API calls
