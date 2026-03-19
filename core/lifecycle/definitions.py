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


LIFECYCLE_DEFINITIONS: dict[str, LifecycleDefinition] = {
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


def get_lifecycle_definition(name: str) -> LifecycleDefinition:
    key = str(name or "").strip().lower()
    if not key or key not in LIFECYCLE_DEFINITIONS:
        raise KeyError(f"Unknown lifecycle definition: {name}")
    return LIFECYCLE_DEFINITIONS[key]


def supported_lifecycles() -> Iterable[str]:
    return tuple(sorted(LIFECYCLE_DEFINITIONS.keys()))
