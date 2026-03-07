# Purpose

This document records temporary architectural compromises accepted to accelerate demo readiness. These items are intentional, tracked debt, not invisible drift. Any new workaround introduced during demo preparation must be recorded here with its removal path.

# Current Accepted Debt

DEBT-01  
Title: Context-pack runtime bridge is manifest-based, not artifact-import-based  
Description: `xyn-platform` remains the governance authority for context packs, but `xyn-core` currently consumes a synchronized runtime manifest rather than a published/imported artifact package.  
Why It Exists: This is the smallest safe bridge that removes indefinite independent seeding in `xyn-core` without attempting the full publish/import/install architecture.  
Risk: Runtime consumption and governance remain connected by a sync contract rather than the final artifact promotion pipeline.  
Planned Resolution: Replace the synced manifest bridge with published/synced context-pack artifacts that `xyn-core` imports explicitly.

DEBT-02  
Title: Sibling install flow is not artifact-based yet  
Description: Sibling Xyn instances are provisioned separately from generated app artifact installation, so the reproduction story is not yet fully artifact-native.  
Why It Exists: The publish/import/install pipeline for generated apps has not been implemented yet.  
Risk: Deployment state and installed-artifact state can drift, weakening the core architectural claim.  
Planned Resolution: Promote generated apps to published/synced artifacts and install them into the sibling through an explicit import/install flow.

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

# Temporary Workarounds Protocol

Whenever a temporary workaround is introduced, record:
- Temporary Workaround
- Why It Exists
- When It Must Be Removed

Do not leave transitional behavior undocumented.
