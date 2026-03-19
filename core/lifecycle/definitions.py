from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class LifecycleDefinition:
    name: str
    states: tuple[str, ...]
    initial_state: str
    transitions: dict[str, tuple[str, ...]]
    terminal_states: frozenset[str]

    def allows(self, from_state: Optional[str], to_state: str) -> bool:
        if to_state not in self.states:
            return False
        if from_state is None:
            return to_state == self.initial_state
        return to_state in self.transitions.get(from_state, ())


def _compat_fallback_definitions() -> dict[str, LifecycleDefinition]:
    return {
        "draft": LifecycleDefinition(
            name="draft",
            states=("draft", "ready", "submitted", "archived"),
            initial_state="draft",
            transitions={
                "draft": ("ready", "submitted", "archived"),
                "ready": ("draft", "submitted", "archived"),
                "submitted": ("archived",),
                "archived": (),
            },
            terminal_states=frozenset({"archived"}),
        ),
        "job": LifecycleDefinition(
            name="job",
            states=("queued", "running", "succeeded", "failed"),
            initial_state="queued",
            transitions={
                "queued": ("running", "failed"),
                "running": ("succeeded", "failed"),
                "succeeded": (),
                "failed": ("queued",),
            },
            terminal_states=frozenset({"succeeded"}),
        ),
    }


# Compatibility note:
# Canonical lifecycle definitions now live in xyn-platform
# (`xyn_orchestrator.lifecycle_primitive.definitions`). Core keeps this fallback
# only to preserve local/runtime compatibility where xyn-platform modules are not
# importable in-process.
try:  # pragma: no cover - depends on cross-repo import wiring
    from xyn_orchestrator.lifecycle_primitive.definitions import LIFECYCLE_DEFINITIONS as _CANONICAL  # type: ignore

    LIFECYCLE_DEFINITIONS: dict[str, LifecycleDefinition] = {
        key: LifecycleDefinition(
            name=value.name,
            states=tuple(value.states),
            initial_state=value.initial_state,
            transitions={k: tuple(v) for k, v in dict(value.transitions).items()},
            terminal_states=frozenset(value.terminal_states),
        )
        for key, value in dict(_CANONICAL).items()
    }
except Exception:  # pragma: no cover - deterministic fallback for core-only runtime
    LIFECYCLE_DEFINITIONS = _compat_fallback_definitions()


def get_lifecycle_definition(name: str) -> LifecycleDefinition:
    key = str(name or "").strip().lower()
    if not key or key not in LIFECYCLE_DEFINITIONS:
        raise KeyError(f"Unknown lifecycle definition: {name}")
    return LIFECYCLE_DEFINITIONS[key]


def supported_lifecycles() -> Iterable[str]:
    return tuple(sorted(LIFECYCLE_DEFINITIONS.keys()))
