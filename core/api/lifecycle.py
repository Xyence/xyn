"""Lifecycle transition visibility endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.lifecycle.definitions import supported_lifecycles
from core.models import LifecycleTransition
from core.workspaces import resolve_workspace_by_context, workspace_context

router = APIRouter()


class LifecycleTransitionResponse(BaseModel):
    id: uuid.UUID
    workspace_id: Optional[uuid.UUID] = None
    lifecycle_name: str
    object_type: str
    object_id: str
    from_state: Optional[str] = None
    to_state: str
    actor: Optional[str] = None
    reason: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    correlation_id: Optional[str] = None
    run_id: Optional[uuid.UUID] = None
    created_at: datetime

    @classmethod
    def from_orm_model(cls, row: LifecycleTransition) -> "LifecycleTransitionResponse":
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            lifecycle_name=row.lifecycle_name,
            object_type=row.object_type,
            object_id=row.object_id,
            from_state=row.from_state,
            to_state=row.to_state,
            actor=row.actor,
            reason=row.reason,
            metadata_json=row.metadata_json or {},
            correlation_id=row.correlation_id,
            run_id=row.run_id,
            created_at=row.created_at,
        )


@router.get("/lifecycle/definitions")
async def list_lifecycle_definitions() -> dict[str, list[str]]:
    return {"lifecycles": list(supported_lifecycles())}


@router.get("/lifecycle/transitions", response_model=list[LifecycleTransitionResponse])
async def list_lifecycle_transitions(
    ctx: dict = Depends(workspace_context),
    object_type: Optional[str] = Query(default=None),
    object_id: Optional[str] = Query(default=None),
    lifecycle_name: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    query = db.query(LifecycleTransition).filter(LifecycleTransition.workspace_id == workspace.id)

    if object_type:
        query = query.filter(LifecycleTransition.object_type == object_type.strip().lower())
    if object_id:
        query = query.filter(LifecycleTransition.object_id == object_id.strip())
    if lifecycle_name:
        normalized = lifecycle_name.strip().lower()
        if normalized not in set(supported_lifecycles()):
            raise HTTPException(status_code=400, detail=f"Unknown lifecycle_name: {normalized}")
        query = query.filter(LifecycleTransition.lifecycle_name == normalized)

    rows = query.order_by(LifecycleTransition.created_at.desc()).limit(limit).all()
    return [LifecycleTransitionResponse.from_orm_model(row) for row in rows]
