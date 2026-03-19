from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass(frozen=True)
class TransitionRequest:
    lifecycle: str
    object_type: str
    object_id: str
    from_state: Optional[str]
    to_state: str
    workspace_id: Optional[str] = None
    actor: Optional[str] = None
    reason: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    run_id: Optional[str] = None
    requested_at: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class TransitionResult:
    lifecycle: str
    object_type: str
    object_id: str
    from_state: Optional[str]
    to_state: str
    created_at: datetime
