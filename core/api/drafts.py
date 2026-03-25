"""Draft API endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.access_control import (
    CAP_APP_READ,
    CAP_CAMPAIGNS_MANAGE,
    AccessPrincipal,
    enforce_access_or_403,
    require_capabilities,
)
from core.lifecycle.service import LifecycleError, apply_transition, transition_model_status
from core.models import Draft, DraftStatus, Job, JobStatus
from core.workspaces import ensure_default_workspace, resolve_workspace_by_context, workspace_context

router = APIRouter()

ALLOWED_DRAFT_STATUSES = {s.value for s in DraftStatus}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DraftCreateRequest(BaseModel):
    type: str = Field(default="app_intent")
    title: str
    content_json: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default=DraftStatus.DRAFT.value)
    created_by: str = Field(default="user")


class DraftPatchRequest(BaseModel):
    title: Optional[str] = None
    content_json: Optional[dict[str, Any]] = None
    status: Optional[str] = None


class DraftResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    type: str
    title: str
    content_json: dict[str, Any]
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_model(cls, row: Draft) -> "DraftResponse":
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            type=row.type,
            title=row.title,
            content_json=row.content_json or {},
            status=row.status,
            created_by=row.created_by,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class DraftSubmitResponse(BaseModel):
    draft: DraftResponse
    job_id: uuid.UUID
    job_status: str


@router.post("/drafts", response_model=DraftResponse, status_code=201)
async def create_draft(
    payload: DraftCreateRequest,
    ctx: dict = Depends(workspace_context),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    ensure_default_workspace(db)
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)
    status = str(payload.status or DraftStatus.DRAFT.value).strip().lower()
    if status not in ALLOWED_DRAFT_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid draft status: {status}")
    row = Draft(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        type=str(payload.type or "app_intent").strip() or "app_intent",
        title=str(payload.title or "").strip(),
        content_json=payload.content_json or {},
        status=DraftStatus.DRAFT.value,
        created_by=str(payload.created_by or "user").strip() or "user",
    )
    if not row.title:
        raise HTTPException(status_code=400, detail="title is required")
    try:
        # Enter the lifecycle through its declared initial state first; callers
        # may request a subsequent valid state like "ready".
        apply_transition(
            db,
            lifecycle="draft",
            object_type="draft",
            object_id=str(row.id),
            from_state=None,
            to_state=DraftStatus.DRAFT.value,
            workspace_id=workspace.id,
            actor=row.created_by,
            reason="Draft created.",
        )
        row.status = DraftStatus.DRAFT.value
        if status != row.status:
            transition_model_status(
                db,
                model_obj=row,
                lifecycle="draft",
                object_type="draft",
                next_state=status,
                actor=row.created_by,
                reason="Draft created with initial status override.",
            )
    except LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(row)
    db.commit()
    db.refresh(row)
    return DraftResponse.from_orm_model(row)


@router.get("/drafts", response_model=list[DraftResponse])
async def list_drafts(
    ctx: dict = Depends(workspace_context),
    limit: int = Query(50, ge=1, le=500),
    status: Optional[str] = Query(default=None),
    draft_type: Optional[str] = Query(default=None, alias="type"),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_APP_READ)),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    enforce_access_or_403(principal, required_capabilities=[CAP_APP_READ], workspace_id=workspace.id)
    query = db.query(Draft).filter(Draft.workspace_id == workspace.id)
    if status:
        norm = status.strip().lower()
        if norm not in ALLOWED_DRAFT_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid draft status: {norm}")
        query = query.filter(Draft.status == norm)
    if draft_type:
        query = query.filter(Draft.type == draft_type.strip())
    rows = query.order_by(Draft.updated_at.desc()).limit(limit).all()
    return [DraftResponse.from_orm_model(row) for row in rows]


@router.get("/drafts/{draft_id}", response_model=DraftResponse)
async def get_draft(
    draft_id: uuid.UUID,
    ctx: dict = Depends(workspace_context),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_APP_READ)),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    enforce_access_or_403(principal, required_capabilities=[CAP_APP_READ], workspace_id=workspace.id)
    row = db.query(Draft).filter(Draft.id == draft_id, Draft.workspace_id == workspace.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Draft not found")
    return DraftResponse.from_orm_model(row)


@router.patch("/drafts/{draft_id}", response_model=DraftResponse)
async def patch_draft(
    draft_id: uuid.UUID,
    payload: DraftPatchRequest,
    ctx: dict = Depends(workspace_context),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)
    row = db.query(Draft).filter(Draft.id == draft_id, Draft.workspace_id == workspace.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Draft not found")
    if payload.title is not None:
        title = str(payload.title).strip()
        if not title:
            raise HTTPException(status_code=400, detail="title cannot be empty")
        row.title = title
    if payload.content_json is not None:
        row.content_json = payload.content_json
    if payload.status is not None:
        status = str(payload.status).strip().lower()
        if status not in ALLOWED_DRAFT_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid draft status: {status}")
        try:
            transition_model_status(
                db,
                model_obj=row,
                lifecycle="draft",
                object_type="draft",
                next_state=status,
                actor="user",
                reason="Draft status updated via patch.",
            )
        except LifecycleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    return DraftResponse.from_orm_model(row)


@router.post("/drafts/{draft_id}/submit", response_model=DraftSubmitResponse)
async def submit_draft(
    draft_id: uuid.UUID,
    ctx: dict = Depends(workspace_context),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)
    row = db.query(Draft).filter(Draft.id == draft_id, Draft.workspace_id == workspace.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        transition_model_status(
            db,
            model_obj=row,
            lifecycle="draft",
            object_type="draft",
            next_state=DraftStatus.SUBMITTED.value,
            actor="user",
            reason="Draft submitted.",
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row.updated_at = _utc_now()
    job = Job(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        type="generate_app_spec",
        status=JobStatus.QUEUED.value,
        input_json={
            "draft_id": str(row.id),
            "draft_type": row.type,
            "title": row.title,
            "content_json": row.content_json or {},
        },
        output_json={},
        logs_text="Queued from draft submit.",
    )
    try:
        apply_transition(
            db,
            lifecycle="job",
            object_type="job",
            object_id=str(job.id),
            from_state=None,
            to_state=job.status,
            workspace_id=workspace.id,
            actor="system",
            reason="Job created from draft submission.",
            metadata={"draft_id": str(row.id)},
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(job)
    db.commit()
    db.refresh(row)
    db.refresh(job)
    return DraftSubmitResponse(
        draft=DraftResponse.from_orm_model(row),
        job_id=job.id,
        job_status=job.status,
    )
