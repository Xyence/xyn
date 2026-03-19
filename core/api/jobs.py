"""Job API endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import get_db
from core.lifecycle.service import LifecycleError, transition_model_status
from core.models import Job, JobStatus
from core.workspaces import resolve_workspace_by_context, workspace_context

router = APIRouter()
ALLOWED_JOB_STATUSES = {s.value for s in JobStatus}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class JobPatchRequest(BaseModel):
    status: Optional[str] = None
    output_json: Optional[dict[str, Any]] = None
    logs_text: Optional[str] = None


class JobResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    type: str
    status: str
    input_json: dict[str, Any]
    output_json: Optional[dict[str, Any]] = None
    logs_text: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_orm_model(cls, row: Job) -> "JobResponse":
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            type=row.type,
            status=row.status,
            input_json=row.input_json or {},
            output_json=row.output_json,
            logs_text=row.logs_text,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(
    ctx: dict = Depends(workspace_context),
    limit: int = Query(50, ge=1, le=500),
    status: Optional[str] = Query(default=None),
    job_type: Optional[str] = Query(default=None, alias="type"),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    query = db.query(Job).filter(Job.workspace_id == workspace.id)
    if status:
        norm = status.strip().lower()
        if norm not in ALLOWED_JOB_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid job status: {norm}")
        query = query.filter(Job.status == norm)
    if job_type:
        query = query.filter(Job.type == job_type.strip())
    rows = query.order_by(Job.updated_at.desc()).limit(limit).all()
    return [JobResponse.from_orm_model(row) for row in rows]


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    ctx: dict = Depends(workspace_context),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    row = db.query(Job).filter(Job.id == job_id, Job.workspace_id == workspace.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse.from_orm_model(row)


@router.get("/jobs/{job_id}/logs")
async def get_job_logs(
    job_id: uuid.UUID,
    ctx: dict = Depends(workspace_context),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    row = db.query(Job).filter(Job.id == job_id, Job.workspace_id == workspace.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": str(row.id), "workspace_id": str(row.workspace_id), "logs_text": row.logs_text or ""}


@router.patch("/jobs/{job_id}", response_model=JobResponse)
async def patch_job(
    job_id: uuid.UUID,
    payload: JobPatchRequest,
    ctx: dict = Depends(workspace_context),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    row = db.query(Job).filter(Job.id == job_id, Job.workspace_id == workspace.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    if payload.status is not None:
        status = str(payload.status).strip().lower()
        if status not in ALLOWED_JOB_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid job status: {status}")
        try:
            transition_model_status(
                db,
                model_obj=row,
                lifecycle="job",
                object_type="job",
                next_state=status,
                actor="user",
                reason="Job status updated via patch.",
            )
        except LifecycleError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.output_json is not None:
        row.output_json = payload.output_json
    if payload.logs_text is not None:
        row.logs_text = payload.logs_text
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    return JobResponse.from_orm_model(row)
