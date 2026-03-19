from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from core import models
from core.lifecycle.definitions import get_lifecycle_definition
from core.lifecycle.interfaces import TransitionRequest, TransitionResult

# Compatibility note:
# The canonical platform lifecycle primitive now lives in xyn-platform
# (`xyn_orchestrator.lifecycle_primitive`). This module remains a thin
# core-integration adapter that wires lifecycle validation to SQLAlchemy-backed
# core objects and persistence.


class LifecycleError(ValueError):
    """Base lifecycle transition validation error."""


class UnknownLifecycleError(LifecycleError):
    """Raised when a lifecycle definition is not registered."""


class InvalidTransitionError(LifecycleError):
    """Raised when a transition is not allowed."""


class MissingStateError(LifecycleError):
    """Raised when transition request omits required state."""


def _normalize_state(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def build_transition_request(
    *,
    lifecycle: str,
    object_type: str,
    object_id: str,
    from_state: Optional[str],
    to_state: Optional[str],
    workspace_id: Optional[uuid.UUID] = None,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    run_id: Optional[uuid.UUID] = None,
) -> TransitionRequest:
    normalized_to_state = _normalize_state(to_state)
    if not normalized_to_state:
        raise MissingStateError("to_state is required")
    return TransitionRequest(
        lifecycle=str(lifecycle or "").strip().lower(),
        object_type=str(object_type or "").strip().lower(),
        object_id=str(object_id or "").strip(),
        from_state=_normalize_state(from_state),
        to_state=normalized_to_state,
        workspace_id=str(workspace_id) if workspace_id else None,
        actor=str(actor or "").strip() or None,
        reason=str(reason or "").strip() or None,
        metadata=metadata or {},
        correlation_id=str(correlation_id or "").strip() or None,
        run_id=str(run_id) if run_id else None,
    )


def validate_transition(request: TransitionRequest) -> None:
    try:
        definition = get_lifecycle_definition(request.lifecycle)
    except KeyError as exc:  # pragma: no cover - direct normalization path
        raise UnknownLifecycleError(str(exc)) from exc

    if not request.object_type:
        raise MissingStateError("object_type is required")
    if not request.object_id:
        raise MissingStateError("object_id is required")

    if not definition.allows(request.from_state, request.to_state):
        from_label = request.from_state or "<none>"
        raise InvalidTransitionError(
            f"Illegal transition for lifecycle '{definition.name}': {from_label} -> {request.to_state}"
        )


def record_transition(db: Session, request: TransitionRequest) -> models.LifecycleTransition:
    row = models.LifecycleTransition(
        workspace_id=uuid.UUID(request.workspace_id) if request.workspace_id else None,
        lifecycle_name=request.lifecycle,
        object_type=request.object_type,
        object_id=request.object_id,
        from_state=request.from_state,
        to_state=request.to_state,
        actor=request.actor,
        reason=request.reason,
        metadata_json=request.metadata or {},
        correlation_id=request.correlation_id,
        run_id=uuid.UUID(request.run_id) if request.run_id else None,
        created_at=request.requested_at,
    )
    db.add(row)
    return row


def apply_transition(
    db: Session,
    *,
    lifecycle: str,
    object_type: str,
    object_id: str,
    from_state: Optional[str],
    to_state: Optional[str],
    workspace_id: Optional[uuid.UUID] = None,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    run_id: Optional[uuid.UUID] = None,
) -> TransitionResult:
    request = build_transition_request(
        lifecycle=lifecycle,
        object_type=object_type,
        object_id=object_id,
        from_state=from_state,
        to_state=to_state,
        workspace_id=workspace_id,
        actor=actor,
        reason=reason,
        metadata=metadata,
        correlation_id=correlation_id,
        run_id=run_id,
    )
    validate_transition(request)
    row = record_transition(db, request)
    return TransitionResult(
        lifecycle=row.lifecycle_name,
        object_type=row.object_type,
        object_id=row.object_id,
        from_state=row.from_state,
        to_state=row.to_state,
        created_at=row.created_at,
    )


def transition_model_status(
    db: Session,
    *,
    model_obj: Any,
    lifecycle: str,
    object_type: str,
    status_attr: str = "status",
    next_state: Optional[str],
    actor: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    correlation_id: Optional[str] = None,
    run_id: Optional[uuid.UUID] = None,
) -> TransitionResult:
    current_state = getattr(model_obj, status_attr, None)
    result = apply_transition(
        db,
        lifecycle=lifecycle,
        object_type=object_type,
        object_id=str(getattr(model_obj, "id")),
        from_state=current_state,
        to_state=next_state,
        workspace_id=getattr(model_obj, "workspace_id", None),
        actor=actor,
        reason=reason,
        metadata=metadata,
        correlation_id=correlation_id,
        run_id=run_id,
    )
    setattr(model_obj, status_attr, result.to_state)
    return result
