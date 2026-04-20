# Planner Contract (AppSpec Semantic Planning)

This document defines the current planner boundary used by AppSpec generation.

## Responsibilities

The planner path is a thin orchestration flow:

1. Normalize the planning request payload (`raw_prompt` and related inputs).
2. Assemble planning context (current app/app-summary plus prompt-derived context).
3. Invoke the planning agent (`codex`) for semantic planning output.
4. Validate the structured response against the semantic planning schema.
5. Persist the resulting plan-derived artifacts and diagnostics.
6. Hand off to downstream execution/deploy stages.

## What Was Removed

- Heuristic semantic fallback planning when the planning agent is unavailable.
- Automatic deterministic repair of malformed planning-agent output.
- Silent substitution of agent output with synthesized fallback plan content.

## Structured Response Schema

Semantic planning output is validated as:

- `entities: list[str]`
- `entity_contracts: list[object]`
- `requested_visuals: list[str]`

Additional properties are rejected.

## Failure Behavior

Planner failures are explicit:

- Agent unavailable -> `SemanticPlanningAgentUnavailableError`
- Agent invocation failure -> `SemanticPlanningError`
- Schema/type validation failure -> `SemanticPlanningResponseValidationError`

The planner does not synthesize replacement plan content on these failures.

## Planner vs Execution Boundary

- Planner: request normalization, context assembly, planning-agent invocation, response validation, persistence metadata.
- Execution: deployment/runtime jobs and operational handoff that consume persisted artifacts.
