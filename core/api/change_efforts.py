"""Change-effort APIs (MVP) for branch-per-effort control-plane orchestration."""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.access_control import (
    CAP_APP_READ,
    CAP_CAMPAIGNS_MANAGE,
    AccessPrincipal,
    enforce_access_or_403,
    require_capabilities,
)
from core.artifact_provenance import extract_provenance_metadata
from core.database import get_db
from core.models import (
    Artifact,
    ChangeEffort,
    ChangeEffortPromotion,
    Environment,
    ReleaseDeclaration,
    Sibling,
    Workspace,
)

router = APIRouter()

_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_slug(value: str, *, default: str) -> str:
    token = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower())
    token = re.sub(r"-{2,}", "-", token).strip("-")
    return token or default


def _sanitize_artifact_slug(value: str) -> str:
    token = _normalize_slug(str(value or "").replace(".", "-"), default="artifact")
    return token[:80]


def _validate_branch_name(value: str, *, field_name: str) -> str:
    branch = str(value or "").strip()
    if not branch:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    if not _BRANCH_RE.match(branch):
        raise HTTPException(status_code=400, detail=f"{field_name} contains invalid characters")
    if branch.startswith("/") or branch.endswith("/") or ".." in branch or " " in branch:
        raise HTTPException(status_code=400, detail=f"{field_name} is invalid")
    if branch.startswith("xyn/") and field_name == "target_branch":
        raise HTTPException(status_code=400, detail="target_branch cannot be an effort branch namespace")
    return branch


def _validate_commit_sha(value: str, *, field_name: str) -> str:
    token = str(value or "").strip().lower()
    if not token:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    if not _COMMIT_SHA_RE.match(token):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a 7-40 hex git SHA")
    return token


def _validate_image_digest(value: str, *, field_name: str) -> str:
    token = str(value or "").strip().lower()
    if not token:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    if not _IMAGE_DIGEST_RE.match(token):
        raise HTTPException(status_code=400, detail=f"{field_name} must be sha256:<64 hex>")
    return token


def _payload(row: ChangeEffort) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "environment_id": str(row.environment_id) if row.environment_id else None,
        "sibling_id": str(row.sibling_id) if row.sibling_id else None,
        "artifact_slug": str(row.artifact_slug or ""),
        "repo_key": str(row.repo_key or ""),
        "repo_url": str(row.repo_url or ""),
        "repo_subpath": str(row.repo_subpath or ""),
        "base_branch": str(row.base_branch or ""),
        "work_branch": str(row.work_branch or ""),
        "target_branch": str(row.target_branch or ""),
        "worktree_path": str(row.worktree_path or ""),
        "status": str(row.status or ""),
        "owner": str(row.owner or ""),
        "created_by": str(row.created_by or ""),
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _latest_artifact_for_slug(db: Session, *, workspace_id: uuid.UUID, artifact_slug: str) -> Optional[Artifact]:
    slug = str(artifact_slug or "").strip()
    if not slug:
        return None
    direct = (
        db.query(Artifact)
        .filter(Artifact.workspace_id == workspace_id, Artifact.name == slug)
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .first()
    )
    if direct:
        return direct
    rows = (
        db.query(Artifact)
        .filter(Artifact.workspace_id == workspace_id)
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .limit(5000)
        .all()
    )
    for row in rows:
        meta = row.extra_metadata if isinstance(row.extra_metadata, dict) else {}
        if str(meta.get("generated_artifact_slug") or "").strip() == slug:
            return row
    return None


def _resolve_effort_or_404(db: Session, effort_id: uuid.UUID) -> ChangeEffort:
    row = db.query(ChangeEffort).filter(ChangeEffort.id == effort_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="change effort not found")
    return row


class ChangeEffortCreateRequest(BaseModel):
    workspace_id: uuid.UUID
    environment_id: Optional[uuid.UUID] = None
    sibling_id: Optional[uuid.UUID] = None
    artifact_slug: str = Field(min_length=1, max_length=255)
    repo_key: Optional[str] = Field(default=None, max_length=255)
    repo_url: Optional[str] = None
    repo_subpath: Optional[str] = None
    base_branch: str = Field(default="develop", min_length=1, max_length=255)
    target_branch: str = Field(default="develop", min_length=1, max_length=255)
    owner: Optional[str] = Field(default=None, max_length=255)


class ChangeEffortResponse(BaseModel):
    change_effort: dict[str, Any]


class ResolveSourceResponse(BaseModel):
    change_effort: dict[str, Any]
    source: dict[str, Any]


class AllocateBranchRequest(BaseModel):
    base_branch: Optional[str] = Field(default=None, max_length=255)
    target_branch: Optional[str] = Field(default=None, max_length=255)
    owner: Optional[str] = Field(default=None, max_length=255)


class AllocateWorktreeRequest(BaseModel):
    root_path: Optional[str] = None
    owner: Optional[str] = Field(default=None, max_length=255)


class PromoteEffortRequest(BaseModel):
    to_branch: str = Field(default="develop", min_length=1, max_length=255)
    strategy: str = Field(default="merge_commit", min_length=1, max_length=64)
    approval_metadata: dict[str, Any] = Field(default_factory=dict)
    merge_commit_sha: Optional[str] = Field(default=None, max_length=64)


class PromoteEffortResponse(BaseModel):
    change_effort: dict[str, Any]
    promotion: dict[str, Any]


class DeclareReleaseRequest(BaseModel):
    workspace_id: uuid.UUID
    artifact_slug: str = Field(min_length=1, max_length=255)
    target_commit_sha: str = Field(min_length=7, max_length=64)
    artifact_revision_map: dict[str, str] = Field(default_factory=dict)
    image_digest_map: dict[str, str] = Field(default_factory=dict)
    environment_id: Optional[uuid.UUID] = None
    effort_id: Optional[uuid.UUID] = None
    pipeline_provider: str = Field(default="github_actions", min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeclareReleaseResponse(BaseModel):
    release: dict[str, Any]


def _promotion_payload(row: ChangeEffortPromotion) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "effort_id": str(row.effort_id),
        "workspace_id": str(row.workspace_id),
        "artifact_slug": str(row.artifact_slug or ""),
        "from_branch": str(row.from_branch or ""),
        "to_branch": str(row.to_branch or ""),
        "strategy": str(row.strategy or ""),
        "status": str(row.status or ""),
        "preflight": row.preflight_json if isinstance(row.preflight_json, dict) else {},
        "approval": row.approval_json if isinstance(row.approval_json, dict) else {},
        "result": row.result_json if isinstance(row.result_json, dict) else {},
        "merge_commit_sha": str(row.merge_commit_sha or ""),
        "requested_by": str(row.requested_by or ""),
        "approved_by": str(row.approved_by or ""),
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _release_payload(row: ReleaseDeclaration) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "environment_id": str(row.environment_id) if row.environment_id else None,
        "effort_id": str(row.effort_id) if row.effort_id else None,
        "artifact_slug": str(row.artifact_slug or ""),
        "target_commit_sha": str(row.target_commit_sha or ""),
        "artifact_revision_map": row.artifact_revision_map_json if isinstance(row.artifact_revision_map_json, dict) else {},
        "image_digest_map": row.image_digest_map_json if isinstance(row.image_digest_map_json, dict) else {},
        "pipeline_provider": str(row.pipeline_provider or ""),
        "status": str(row.status or ""),
        "declared_by": str(row.declared_by or ""),
        "declared_at": row.declared_at.isoformat() if row.declared_at else None,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/change-efforts", response_model=ChangeEffortResponse, status_code=201)
async def create_change_effort(
    payload: ChangeEffortCreateRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    workspace = db.query(Workspace).filter(Workspace.id == payload.workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="workspace not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)

    environment_id = payload.environment_id
    if environment_id:
        environment = db.query(Environment).filter(Environment.id == environment_id).first()
        if not environment or environment.workspace_id != workspace.id:
            raise HTTPException(status_code=400, detail="environment_id is invalid for workspace")
    sibling_id = payload.sibling_id
    if sibling_id:
        sibling = db.query(Sibling).filter(Sibling.id == sibling_id).first()
        if not sibling or sibling.workspace_id != workspace.id:
            raise HTTPException(status_code=400, detail="sibling_id is invalid for workspace")

    base_branch = _validate_branch_name(payload.base_branch, field_name="base_branch")
    target_branch = _validate_branch_name(payload.target_branch, field_name="target_branch")
    actor = str(getattr(principal, "subject_id", "") or "").strip() or "system"
    owner = str(payload.owner or actor).strip() or actor

    row = ChangeEffort(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        environment_id=environment_id,
        sibling_id=sibling_id,
        artifact_slug=str(payload.artifact_slug).strip(),
        repo_key=str(payload.repo_key or "").strip() or None,
        repo_url=str(payload.repo_url or "").strip() or None,
        repo_subpath=str(payload.repo_subpath or "").strip() or None,
        base_branch=base_branch,
        target_branch=target_branch,
        status="created",
        owner=owner,
        created_by=actor,
        metadata_json={},
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ChangeEffortResponse(change_effort=_payload(row))


@router.get("/change-efforts/{effort_id}", response_model=ChangeEffortResponse)
async def get_change_effort(
    effort_id: uuid.UUID,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_APP_READ)),
    db: Session = Depends(get_db),
):
    row = _resolve_effort_or_404(db, effort_id)
    enforce_access_or_403(principal, required_capabilities=[CAP_APP_READ], workspace_id=row.workspace_id)
    return ChangeEffortResponse(change_effort=_payload(row))


@router.post("/change-efforts/{effort_id}/resolve-source", response_model=ResolveSourceResponse)
async def resolve_change_effort_source(
    effort_id: uuid.UUID,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    row = _resolve_effort_or_404(db, effort_id)
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=row.workspace_id)

    artifact = _latest_artifact_for_slug(db, workspace_id=row.workspace_id, artifact_slug=row.artifact_slug)
    if not artifact:
        raise HTTPException(status_code=404, detail=f"artifact not found for slug={row.artifact_slug}")
    metadata = artifact.extra_metadata if isinstance(artifact.extra_metadata, dict) else {}
    provenance = extract_provenance_metadata(metadata)
    source = provenance.get("source") if isinstance(provenance.get("source"), dict) else {}
    if str(source.get("kind") or "").strip().lower() != "git":
        raise HTTPException(status_code=409, detail="artifact provenance is missing source.kind=git")

    repo_key = str(source.get("repo_key") or "").strip()
    repo_url = str(source.get("repo_url") or "").strip()
    repo_subpath = str(source.get("monorepo_subpath") or "").strip()
    if not repo_key and not repo_url:
        raise HTTPException(status_code=409, detail="artifact provenance missing repo_key/repo_url")

    row.repo_key = repo_key or row.repo_key
    row.repo_url = repo_url or row.repo_url
    row.repo_subpath = repo_subpath or row.repo_subpath
    if str(row.base_branch or "").strip() in {"", "develop"}:
        branch_hint = str(source.get("branch_hint") or "").strip()
        if branch_hint:
            row.base_branch = _validate_branch_name(branch_hint, field_name="base_branch")
    if row.status == "created":
        row.status = "source_resolved"
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    return ResolveSourceResponse(change_effort=_payload(row), source=source)


@router.post("/change-efforts/{effort_id}/allocate-branch", response_model=ChangeEffortResponse)
async def allocate_change_effort_branch(
    effort_id: uuid.UUID,
    payload: AllocateBranchRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    row = _resolve_effort_or_404(db, effort_id)
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=row.workspace_id)

    if payload.owner and str(payload.owner).strip() and str(row.owner or "").strip() and str(payload.owner).strip() != str(row.owner or "").strip():
        raise HTTPException(status_code=409, detail="change effort owner mismatch")

    if payload.base_branch:
        row.base_branch = _validate_branch_name(payload.base_branch, field_name="base_branch")
    else:
        row.base_branch = _validate_branch_name(str(row.base_branch or "develop"), field_name="base_branch")
    if payload.target_branch:
        row.target_branch = _validate_branch_name(payload.target_branch, field_name="target_branch")
    else:
        row.target_branch = _validate_branch_name(str(row.target_branch or "develop"), field_name="target_branch")

    deterministic = f"xyn/{_sanitize_artifact_slug(row.artifact_slug)}/{str(row.id).replace('-', '')[:12]}"
    if row.work_branch and row.work_branch != deterministic:
        raise HTTPException(status_code=409, detail="existing work_branch does not match deterministic naming")
    conflict = (
        db.query(ChangeEffort)
        .filter(
            ChangeEffort.workspace_id == row.workspace_id,
            ChangeEffort.work_branch == deterministic,
            ChangeEffort.id != row.id,
        )
        .first()
    )
    if conflict:
        raise HTTPException(status_code=409, detail="deterministic work_branch already allocated")

    row.work_branch = deterministic
    if row.status in {"created", "source_resolved"}:
        row.status = "branch_allocated"
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    return ChangeEffortResponse(change_effort=_payload(row))


@router.post("/change-efforts/{effort_id}/allocate-worktree", response_model=ChangeEffortResponse)
async def allocate_change_effort_worktree(
    effort_id: uuid.UUID,
    payload: AllocateWorktreeRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    row = _resolve_effort_or_404(db, effort_id)
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=row.workspace_id)
    if payload.owner and str(payload.owner).strip() and str(row.owner or "").strip() and str(payload.owner).strip() != str(row.owner or "").strip():
        raise HTTPException(status_code=409, detail="change effort owner mismatch")
    if not str(row.work_branch or "").strip():
        raise HTTPException(status_code=400, detail="work_branch is not allocated; call allocate-branch first")

    workspace = db.query(Workspace).filter(Workspace.id == row.workspace_id).first()
    workspace_token = str((workspace.slug if workspace else row.workspace_id) or "").strip() or str(row.workspace_id)
    root_raw = str(payload.root_path or "").strip() or str(
        Path(str(os.getenv("XYN_CHANGE_EFFORT_WORKTREE_ROOT", "/workspace/.xyn/change-efforts"))).expanduser()
    )
    root = Path(root_raw).expanduser().resolve()
    candidate = (root / workspace_token / str(row.id)).resolve()
    try:
        candidate.relative_to(root)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="resolved worktree path escapes root_path") from exc

    if row.worktree_path:
        current = Path(str(row.worktree_path)).expanduser().resolve()
        if current != candidate:
            raise HTTPException(status_code=409, detail="existing worktree_path is already allocated")
        current.mkdir(parents=True, exist_ok=True)
        return ChangeEffortResponse(change_effort=_payload(row))

    conflict = (
        db.query(ChangeEffort)
        .filter(ChangeEffort.worktree_path == str(candidate), ChangeEffort.id != row.id)
        .first()
    )
    if conflict:
        raise HTTPException(status_code=409, detail="worktree_path already allocated to another effort")

    candidate.mkdir(parents=True, exist_ok=True)
    row.worktree_path = str(candidate)
    if row.status in {"created", "source_resolved", "branch_allocated"}:
        row.status = "worktree_allocated"
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    return ChangeEffortResponse(change_effort=_payload(row))


@router.post("/change-efforts/{effort_id}/promote", response_model=PromoteEffortResponse)
async def promote_change_effort(
    effort_id: uuid.UUID,
    payload: PromoteEffortRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    row = _resolve_effort_or_404(db, effort_id)
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=row.workspace_id)

    to_branch = _validate_branch_name(payload.to_branch, field_name="to_branch")
    if to_branch != "develop":
        raise HTTPException(status_code=400, detail="only promotion to develop is supported in this phase")
    if not str(row.work_branch or "").strip():
        raise HTTPException(status_code=409, detail="effort has no work_branch allocated")
    if str(row.status or "") not in {"branch_allocated", "worktree_allocated", "ready_for_promotion", "promotion_requested"}:
        raise HTTPException(status_code=409, detail=f"effort status {row.status} is not promotable")
    if str(row.target_branch or "").strip() not in {"", "develop"}:
        raise HTTPException(status_code=409, detail=f"effort target_branch {row.target_branch} is not promotable in this phase")

    actor = str(getattr(principal, "subject_id", "") or "").strip() or "system"
    strategy = str(payload.strategy or "").strip().lower() or "merge_commit"
    if strategy not in {"merge_commit", "squash", "rebase"}:
        raise HTTPException(status_code=400, detail="strategy must be merge_commit|squash|rebase")

    preflight = {
        "branch_exists": bool(str(row.work_branch or "").strip()),
        "effort_status_promotable": True,
        "target_branch_valid": True,
        "conflict_check": {
            "status": "not_implemented",
            "result": "unknown",
            "note": "Merge conflict detection hook not implemented in this phase.",
        },
    }
    existing = (
        db.query(ChangeEffortPromotion)
        .filter(
            ChangeEffortPromotion.effort_id == row.id,
            ChangeEffortPromotion.from_branch == row.work_branch,
            ChangeEffortPromotion.to_branch == to_branch,
            ChangeEffortPromotion.strategy == strategy,
            ChangeEffortPromotion.status.in_(["requested", "preflight_passed", "merge_pending"]),
        )
        .order_by(ChangeEffortPromotion.created_at.desc())
        .first()
    )
    if existing:
        return PromoteEffortResponse(change_effort=_payload(row), promotion=_promotion_payload(existing))

    promotion_status = "preflight_passed"
    merge_commit_sha = ""
    completed_at = None
    result_json: dict[str, Any] = {"merge_execution": "pending_external"}
    if payload.merge_commit_sha:
        merge_commit_sha = _validate_commit_sha(payload.merge_commit_sha, field_name="merge_commit_sha")
        promotion_status = "completed"
        completed_at = _utc_now()
        result_json = {"merge_execution": "recorded_external", "merge_commit_sha": merge_commit_sha}

    promotion = ChangeEffortPromotion(
        id=uuid.uuid4(),
        effort_id=row.id,
        workspace_id=row.workspace_id,
        artifact_slug=row.artifact_slug,
        from_branch=str(row.work_branch or ""),
        to_branch=to_branch,
        strategy=strategy,
        status=promotion_status,
        preflight_json=preflight,
        approval_json=payload.approval_metadata if isinstance(payload.approval_metadata, dict) else {},
        result_json=result_json,
        merge_commit_sha=merge_commit_sha or None,
        requested_by=actor,
        approved_by=actor if payload.approval_metadata else None,
        requested_at=_utc_now(),
        completed_at=completed_at,
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(promotion)
    row.status = "promoted_to_develop" if promotion_status == "completed" else "promotion_requested"
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    db.refresh(promotion)
    return PromoteEffortResponse(change_effort=_payload(row), promotion=_promotion_payload(promotion))


@router.post("/releases/declare", response_model=DeclareReleaseResponse)
async def declare_release(
    payload: DeclareReleaseRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    workspace = db.query(Workspace).filter(Workspace.id == payload.workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="workspace not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)
    actor = str(getattr(principal, "subject_id", "") or "").strip() or "system"

    target_commit_sha = _validate_commit_sha(payload.target_commit_sha, field_name="target_commit_sha")
    if not isinstance(payload.artifact_revision_map, dict) or not payload.artifact_revision_map:
        raise HTTPException(status_code=400, detail="artifact_revision_map must contain at least one artifact entry")
    if not isinstance(payload.image_digest_map, dict) or not payload.image_digest_map:
        raise HTTPException(status_code=400, detail="image_digest_map must contain at least one artifact entry")
    normalized_revisions: dict[str, str] = {}
    for key, value in payload.artifact_revision_map.items():
        artifact_key = str(key or "").strip()
        revision_id = str(value or "").strip()
        if not artifact_key or not revision_id:
            raise HTTPException(status_code=400, detail="artifact_revision_map keys and values must be non-empty")
        normalized_revisions[artifact_key] = revision_id
    normalized_digests: dict[str, str] = {}
    for key, value in payload.image_digest_map.items():
        artifact_key = str(key or "").strip()
        digest = _validate_image_digest(str(value or ""), field_name=f"image_digest_map[{artifact_key}]")
        normalized_digests[artifact_key] = digest

    artifact_slug = str(payload.artifact_slug or "").strip()
    if artifact_slug not in normalized_revisions:
        raise HTTPException(status_code=400, detail="artifact_slug must be present in artifact_revision_map")

    for slug in normalized_revisions.keys():
        artifact = _latest_artifact_for_slug(db, workspace_id=workspace.id, artifact_slug=slug)
        if not artifact:
            raise HTTPException(status_code=409, detail=f"artifact not found for slug={slug}")
        provenance = extract_provenance_metadata(artifact.extra_metadata if isinstance(artifact.extra_metadata, dict) else {})
        source = provenance.get("source") if isinstance(provenance.get("source"), dict) else {}
        build = provenance.get("build") if isinstance(provenance.get("build"), dict) else {}
        if not source and not build:
            raise HTTPException(status_code=409, detail=f"artifact provenance missing for slug={slug}")

    environment_id = payload.environment_id
    if environment_id:
        env = db.query(Environment).filter(Environment.id == environment_id).first()
        if not env or env.workspace_id != workspace.id:
            raise HTTPException(status_code=400, detail="environment_id is invalid for workspace")

    effort_id = payload.effort_id
    if effort_id:
        effort = db.query(ChangeEffort).filter(ChangeEffort.id == effort_id).first()
        if not effort or effort.workspace_id != workspace.id:
            raise HTTPException(status_code=400, detail="effort_id is invalid for workspace")

    row = ReleaseDeclaration(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        environment_id=environment_id,
        effort_id=effort_id,
        artifact_slug=artifact_slug,
        target_commit_sha=target_commit_sha,
        artifact_revision_map_json=normalized_revisions,
        image_digest_map_json=normalized_digests,
        pipeline_provider=str(payload.pipeline_provider or "github_actions").strip() or "github_actions",
        status="declared",
        declared_by=actor,
        declared_at=_utc_now(),
        metadata_json=payload.metadata if isinstance(payload.metadata, dict) else {},
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return DeclareReleaseResponse(release=_release_payload(row))


@router.get("/provenance/{artifact_slug}")
async def get_artifact_provenance(
    artifact_slug: str,
    workspace_id: uuid.UUID,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_APP_READ)),
    db: Session = Depends(get_db),
):
    workspace = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="workspace not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_APP_READ], workspace_id=workspace.id)

    slug = str(artifact_slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="artifact_slug is required")

    efforts = (
        db.query(ChangeEffort)
        .filter(ChangeEffort.workspace_id == workspace.id, ChangeEffort.artifact_slug == slug)
        .order_by(ChangeEffort.created_at.desc())
        .limit(100)
        .all()
    )
    promotions = (
        db.query(ChangeEffortPromotion)
        .filter(ChangeEffortPromotion.workspace_id == workspace.id, ChangeEffortPromotion.artifact_slug == slug)
        .order_by(ChangeEffortPromotion.created_at.desc())
        .limit(200)
        .all()
    )
    release_rows = (
        db.query(ReleaseDeclaration)
        .filter(ReleaseDeclaration.workspace_id == workspace.id)
        .order_by(ReleaseDeclaration.created_at.desc())
        .limit(200)
        .all()
    )
    releases = []
    for row in release_rows:
        rev_map = row.artifact_revision_map_json if isinstance(row.artifact_revision_map_json, dict) else {}
        digest_map = row.image_digest_map_json if isinstance(row.image_digest_map_json, dict) else {}
        if row.artifact_slug == slug or slug in rev_map or slug in digest_map:
            releases.append(_release_payload(row))

    artifact = _latest_artifact_for_slug(db, workspace_id=workspace.id, artifact_slug=slug)
    artifact_provenance = {}
    if artifact:
        artifact_provenance = extract_provenance_metadata(artifact.extra_metadata if isinstance(artifact.extra_metadata, dict) else {})

    return {
        "artifact_slug": slug,
        "workspace_id": str(workspace.id),
        "artifact_provenance": artifact_provenance,
        "efforts": [_payload(row) for row in efforts],
        "promotions": [_promotion_payload(row) for row in promotions],
        "releases": releases,
    }
