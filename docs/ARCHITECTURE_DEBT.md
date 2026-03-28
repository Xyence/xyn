# Purpose

This document records temporary architectural compromises accepted to accelerate demo readiness. These items are intentional, tracked debt, not invisible drift. Any new workaround introduced during demo preparation must be recorded here with its removal path.

# Current Accepted Debt

DEBT-01  
Title: Context-pack runtime bridge is manifest-based, not artifact-import-based  
Description: `xyn-platform` remains the governance authority for context packs. `xyn-core` now consumes an artifact-style synced distribution payload first, but still keeps synchronized manifest fallback for backward compatibility.  
Why It Exists: This is the smallest safe bridge that removes indefinite independent seeding in `xyn-core` without attempting the full publish/import/install architecture.  
Risk: Runtime consumption and governance remain connected by a local sync contract until manifest fallback is fully retired and all environments use artifact publish/import exclusively.  
Planned Resolution: Complete migration to published/synced context-pack artifacts and retire manifest fallback once all supported environments are artifact-backed.

DEBT-02  
Title: Generated artifact lifecycle is still partial  
Description: Sibling Xyn instances now import and install a generated artifact (`app.net-inventory`) before runtime registration, and the generated artifact carries the capability metadata, suggestions, and surfaces needed for sibling UI/capability behavior. The bridge artifact has been retired from normal install/catalog flows, anchored revision prompts now reuse the existing sibling workspace/runtime instead of provisioning a second sibling environment, and validated revised features such as `interfaces` and `interfaces by status` now materialize into sibling runtime commands and reporting. Generated artifact promotion/versioning is still ad hoc and not yet a full publish/promote/install lifecycle.  
Why It Exists: This remains the smallest safe path while the full generated publish/import/install lifecycle is still incomplete.  
Risk: Generated artifact identity, sibling runtime realization, and validated evolution behavior are aligned for the demo path, but versioning, promotion semantics, and broader lifecycle management remain incomplete.  
Planned Resolution: Build the next lifecycle step around explicit generated artifact promotion/distribution semantics, then simplify the remaining generated-app import/install code paths without regressing the current in-place sibling evolution path.

DEBT-03  
Title: Clean-baseline migrations are stronger than dirty-dev migration recovery  
Description: Migration replay is reliable on a clean database, but older drifted developer databases can still fail because earlier local schema evolution predated the current migration discipline.  
Why It Exists: The migration framework stabilized after substantial local schema drift had already accumulated.  
Risk: Developer friction and inconsistent local recovery behavior.  
Planned Resolution: Either add compatibility repair migrations for known dirty states or document/reset tooling more explicitly.

DEBT-04  
Title: Legacy UI surfaces still coexist with the newer workbench path  
Description: Parts of the system still retain legacy Django-era UI behavior and routing while the workbench/prompt-driven UI becomes the intended experience.  
Why It Exists: The migration from legacy surfaces to the current workbench flow is incomplete.  
Risk: Architectural inconsistency and demo-path leakage into legacy pages.  
Planned Resolution: Continue migrating or hard-guarding demo-path entrypoints until the canonical experience is unambiguous.

DEBT-05  
Title: Demo app runtime is still more runtime-first than artifact-first  
Description: The generated network inventory application can be deployed and reached, but the open/install story is still partly expressed as raw runtime URLs instead of installed artifact surfaces.  
Why It Exists: Runtime deploy was implemented before the artifact promotion/install path.  
Risk: Users can reach a service without a fully coherent artifact lifecycle explanation.  
Planned Resolution: Make the deployed app surface derive from installed artifact state and use artifact-managed entry routes.

DEBT-06  
Title: Repo-local legacy directories still exist on disk  
Description: Old repo-root `artifacts/` and `workspace/` directories are no longer mounted as canonical runtime storage, but they may still exist on disk and confuse developers.  
Why It Exists: Automatic destructive cleanup would be risky during active development.  
Risk: Mistaken assumptions about canonical storage or accidental manual reuse of stale files.  
Planned Resolution: Add a clearer cleanup command or migration helper once the runtime storage transition is fully stable.

DEBT-PROTO-01  
Title: Execution-note protocol minimal implementation  
Description: The current execution-note mechanism captures findings, root cause, proposed fix, implementation summary, and validation for non-trivial generation work, but it is not yet a full planning subsystem or universal governance layer.  
Why It Exists: This is the smallest safe change before the demo that adds durable reasoning records without redesigning the artifact system.  
Risk: Coverage is partial and currently focused on the app-builder generation pipeline.  
Planned Resolution: Expand execution-note coverage into a fuller autonomous planning/governance artifact system after the demo.

DEBT-07  
Title: AppSpec -> ArtifactSpec Consolidation  
Description: The current generated-app path is `Prompt -> AppSpec -> Generated Artifact -> Artifact Registry -> WorkspaceArtifactBinding -> WorkspaceAppInstance -> runtime_target`. The generated artifact now carries the canonical identity, capability metadata, suggestions, surfaces, and install semantics, which means AppSpec largely functions as a build intermediate rather than the final system-of-record.  
Why It Exists: The current demo-safe pipeline evolved incrementally. AppSpec remains useful as a durable build-stage representation, but the runtime-facing semantics now live primarily on the generated artifact.  
Risk: Identity and orchestration logic remain split across AppSpec and generated-artifact layers, which keeps `app_jobs.py` more complex than necessary and maintains extra coupling between `xyn-core` generation and Django-side registry import paths.  
Planned Resolution: Evaluate a future artifact-first build model of `Prompt -> ArtifactSpec -> Artifact -> WorkspaceArtifactBinding -> WorkspaceAppInstance -> runtime_target` that preserves the current artifact packaging model, registry install semantics, WorkspaceAppInstance runtime targeting, and compatibility with existing artifacts.  
Status: Future simplification opportunity. Not required for demo readiness.

DEBT-08  
Title: Generalized evolved-feature materialization remains narrow  
Description: The validated evolution path now materializes the `interfaces` entity and `interfaces by status` reporting into generated artifact metadata, sibling runtime behavior, and palette-visible functionality. That materialization logic is still relatively specific to the currently validated scenario rather than a broadly generalized entity/report expansion mechanism.  
Why It Exists: The current implementation prioritized the smallest safe fix needed to make the demo-ready create-and-extend path work end to end without redesigning the app-generation or runtime-materialization system.  
Risk: Future evolved entities or reports beyond the validated interfaces path may require additional targeted materialization work before they become runtime-facing functionality.  
Planned Resolution: Generalize the AppSpec-to-runtime materialization layer so newly added entities, palette commands, and reports derive systematically from generated artifact/application definition state rather than relying on scenario-specific expansion logic.  
Status: Future hardening opportunity. Not required for demo readiness.

DEBT-09  
Title: Generated artifact promotion and version semantics remain minimal  
Description: Generated artifacts now serve as the canonical installed identity in the happy path, but their versioning and promotion semantics remain intentionally lightweight (for example `0.0.1-dev` re-import/update behavior) rather than a fuller lifecycle with stronger revision, release, and promotion rules.  
Why It Exists: Demo readiness required a working artifact-native path before a broader artifact release model was designed.  
Risk: Long-term lifecycle behavior for generated apps may remain ambiguous around revisions, releases, and promotion boundaries if this is not tightened later.  
Planned Resolution: Define stronger generated-artifact version/update/promotion semantics that preserve the current packaging/import/install model while making revision and release behavior explicit.  
Status: Future hardening opportunity. Not required for demo readiness.

DEBT-10  
Title: Dedicated revision-history UI is not yet implemented  
Description: The UI now exposes the originating prompt, application definition, generated artifact identity, and a working `Revise application` entry point, but it does not yet provide a dedicated revision-history timeline or revision list for generated apps.  
Why It Exists: The current demo path only needed a clear definition/revise story, not a broader history-management surface.  
Risk: Post-demo product workflows may need a clearer revision audit/history experience than the current single-definition view.  
Planned Resolution: Add a dedicated revision-history surface when product scope allows, reusing existing stored draft/build/artifact lineage rather than introducing a parallel history model.  
Status: Future UX expansion. Not required for demo readiness.

DEBT-11  
Title: Django test DB regression environment remains fragile  
Description: Browser-driven regression coverage now passes for the demo path, but there is still a known unrelated Django test DB issue that can interfere with some in-container regression execution and make lower-level automated coverage less robust than it should be.  
Why It Exists: Demo-focused validation prioritized browser truth and live-system validation over deeper cleanup of the Django test DB environment.  
Risk: Some automated regression runs may remain noisier or less reliable than desired until the test DB issue is fixed.  
Planned Resolution: Repair the Django test DB setup so containerized regression and lower-level automated test execution can run consistently without unrelated database-trigger failures.  
Status: Future test-hardening opportunity. Not required for demo readiness.

# Temporary Workarounds Protocol

Whenever a temporary workaround is introduced, record:
- Temporary Workaround
- Why It Exists
- When It Must Be Removed

Do not leave transitional behavior undocumented.
