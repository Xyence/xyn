# Lifecycle Primitive

## What It Is

A lightweight platform primitive for lifecycle/state transitions across platform-owned objects.

It provides:
- reusable lifecycle definitions
- legal transition enforcement
- durable transition history
- actor/reason/metadata capture for auditability

## What It Is Not

- Not a BPM engine.
- Not a workflow/orchestration replacement.
- Not a run-history replacement.

Use orchestration/runtime for execution pipelines; use lifecycle for object state integrity.

## Core Concepts

- Lifecycle definition: named state model (`draft`, `job`) with initial state and legal transitions.
- Transition request: from/to state + actor/reason/metadata + optional run/correlation linkage.
- Transition history: durable row per successful transition in `lifecycle_transitions`.

## Current Definitions (v1)

- `draft`: `draft -> ready/submitted/archived`, `ready -> draft/submitted/archived`, `submitted -> archived`
- `job`: `queued -> running/failed`, `running -> succeeded/failed`, `failed -> queued`

## How To Use

- Define/extend lifecycles in [`core/lifecycle/definitions.py`](../core/lifecycle/definitions.py).
- Enforce a transition through service helpers in [`core/lifecycle/service.py`](../core/lifecycle/service.py):
  - `apply_transition(...)` for generic object references.
  - `transition_model_status(...)` to validate + update model status field + record history.

## API Visibility

- `GET /api/v1/lifecycle/definitions`
- `GET /api/v1/lifecycle/transitions?workspace_slug=...&object_type=...&object_id=...`

History response includes prior/new state, actor, reason, metadata, and optional correlation/run linkage.

## Integration Boundaries

- Orchestration/run history: execution-level status and step artifacts.
- Lifecycle primitive: object-level status transition policy and audit log.
- Source connectors and future review objects should reuse this primitive for status integrity.

## Relationship To Future Work

This v1 adds the reusable substrate and initial integrations (`Draft`, `Job`).
Future objects should adopt it incrementally rather than reimplementing ad hoc transition guards.
