from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.context_packs import (
    get_workspace_sync_target,
    list_context_pack_artifacts,
    resolve_bound_context_pack_artifacts,
    set_workspace_context_pack_bindings,
    ensure_runtime_context_pack_artifacts,
)
from core.database import get_db
from core.models import Artifact
from core.workspaces import resolve_workspace_by_context, workspace_context

router = APIRouter()


class ContextPackResponse(BaseModel):
    artifact_id: uuid.UUID
    workspace_id: Optional[uuid.UUID] = None
    slug: str
    title: str
    description: str
    storage_scope: str
    sync_state: str
    capabilities: list[str] = Field(default_factory=list)
    source_authority: str = ""
    bind_by_default: bool = False

    @classmethod
    def from_artifact(cls, row: Artifact) -> "ContextPackResponse":
        metadata = row.extra_metadata if isinstance(row.extra_metadata, dict) else {}
        return cls(
            artifact_id=row.id,
            workspace_id=row.workspace_id,
            slug=str(metadata.get("pack_slug") or row.name),
            title=str(metadata.get("pack_title") or row.name),
            description=str(metadata.get("description") or ""),
            storage_scope=str(getattr(row, "storage_scope", "instance-local") or "instance-local"),
            sync_state=str(getattr(row, "sync_state", "local") or "local"),
            capabilities=[str(item) for item in (metadata.get("capabilities") or []) if str(item).strip()],
            source_authority=str(metadata.get("source_authority") or ""),
            bind_by_default=bool(metadata.get("bind_by_default", False)),
        )


class ContextPackBindingsResponse(BaseModel):
    workspace_id: uuid.UUID
    workspace_slug: str
    bound_context_packs: list[ContextPackResponse]
    artifact_sync_target: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class ContextPackBindingsPatchRequest(BaseModel):
    artifact_ids: list[uuid.UUID] = Field(default_factory=list)
    artifact_sync_target: Optional[str] = None


@router.get("/context-packs", response_model=list[ContextPackResponse])
async def list_context_packs(
    ctx: dict = Depends(workspace_context),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    ensure_runtime_context_pack_artifacts(db)
    return [ContextPackResponse.from_artifact(row) for row in list_context_pack_artifacts(db, workspace_id=workspace.id)]


@router.get("/context-packs/bindings", response_model=ContextPackBindingsResponse)
async def get_context_pack_bindings(
    ctx: dict = Depends(workspace_context),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    packs, warnings = resolve_bound_context_pack_artifacts(db, workspace=workspace)
    return ContextPackBindingsResponse(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        bound_context_packs=[ContextPackResponse.from_artifact(row) for row in packs],
        artifact_sync_target=get_workspace_sync_target(db, workspace_slug=workspace.slug) or None,
        warnings=warnings,
    )


@router.patch("/context-packs/bindings", response_model=ContextPackBindingsResponse)
async def patch_context_pack_bindings(
    payload: ContextPackBindingsPatchRequest,
    ctx: dict = Depends(workspace_context),
    db: Session = Depends(get_db),
):
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=ctx.get("workspace_id"),
        workspace_slug=ctx.get("workspace_slug"),
    )
    if payload.artifact_ids:
        found = (
            db.query(Artifact)
            .filter(
                Artifact.kind == "context-pack",
                Artifact.id.in_(payload.artifact_ids),
                ((Artifact.workspace_id == workspace.id) | (Artifact.workspace_id.is_(None))),
            )
            .count()
        )
        if found != len(payload.artifact_ids):
            raise HTTPException(status_code=400, detail="One or more context-pack artifact_ids are invalid for this workspace")
    set_workspace_context_pack_bindings(
        db,
        workspace=workspace,
        artifact_ids=payload.artifact_ids,
        artifact_sync_target=payload.artifact_sync_target,
    )
    packs, warnings = resolve_bound_context_pack_artifacts(db, workspace=workspace)
    return ContextPackBindingsResponse(
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        bound_context_packs=[ContextPackResponse.from_artifact(row) for row in packs],
        artifact_sync_target=get_workspace_sync_target(db, workspace_slug=workspace.slug) or None,
        warnings=warnings,
    )
