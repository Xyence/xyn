from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.context_pack_manifest import load_authoritative_context_pack_definitions
from core.models import Artifact, Workspace, WorkspaceSetting


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_instance_artifact_root() -> str:
    return str(os.getenv("XYN_ARTIFACT_ROOT") or os.getenv("ARTIFACT_STORE_PATH", ".xyn/artifacts")).strip() or ".xyn/artifacts"


def default_instance_workspace_root() -> str:
    return (
        str(
            os.getenv("XYN_WORKSPACE_ROOT")
            or os.getenv("XYN_LOCAL_WORKSPACE_ROOT")
            or os.getenv("XYNSEED_WORKSPACE", ".xyn/workspace")
        ).strip()
        or ".xyn/workspace"
    )


def default_instance_deployments_root() -> str:
    return str(os.getenv("XYN_LOCAL_DEPLOYMENTS_ROOT", ".xyn/deployments")).strip() or ".xyn/deployments"


def ensure_runtime_context_pack_artifacts(db: Session) -> list[Artifact]:
    definitions, source_info = load_authoritative_context_pack_definitions()
    allowed_slugs = {str(row["slug"]).strip().lower() for row in definitions}
    rows: list[Artifact] = []
    existing_rows = (
        db.query(Artifact)
        .filter(Artifact.kind == "context-pack", Artifact.storage_scope == "core-packaged")
        .order_by(Artifact.created_at.asc())
        .all()
    )
    stale_rows = [row for row in existing_rows if str(row.name or "").strip().lower() not in allowed_slugs]
    for stale in stale_rows:
        db.delete(stale)
    db.flush()
    for pack in definitions:
        slug = str(pack["slug"]).strip().lower()
        existing = (
            db.query(Artifact)
            .filter(Artifact.kind == "context-pack", Artifact.name == slug, Artifact.storage_scope == "core-packaged")
            .order_by(Artifact.created_at.asc())
            .first()
        )
        metadata = {
            "pack_slug": slug,
            "pack_title": pack["title"],
            "pack_scope": pack["scope"],
            "pack_purpose": pack["purpose"],
            "pack_version": pack["version"],
            "capabilities": list(pack.get("capabilities") or []),
            "description": pack["description"],
            "bind_by_default": bool(pack.get("bind_by_default", False)),
            "content_format": pack.get("content_format") or "markdown",
            "content": pack["content"],
            "applies_to_json": pack.get("applies_to_json") if isinstance(pack.get("applies_to_json"), dict) else {},
            "source_authority": source_info["source_system"],
            "source_manifest_version": source_info["manifest_version"],
            "source_seed_pack_slug": source_info["source_seed_pack_slug"],
            "source_seed_pack_version": source_info["source_seed_pack_version"],
            "source_manifest_path": source_info["manifest_path"],
            "source_fallback_used": bool(source_info["fallback_used"]),
        }
        if existing is None:
            existing = Artifact(
                id=uuid.uuid4(),
                workspace_id=None,
                name=slug,
                kind="context-pack",
                storage_scope="core-packaged",
                sync_state="packaged",
                content_type="application/json",
                byte_length=None,
                sha256=None,
                created_by="seed-bootstrap",
                extra_metadata=metadata,
                storage_path=None,
                created_at=_utc_now(),
            )
            db.add(existing)
            db.flush()
        else:
            existing.storage_scope = "core-packaged"
            existing.sync_state = "packaged"
            existing.extra_metadata = metadata
        rows.append(existing)
    db.commit()
    return rows


def list_context_pack_artifacts(db: Session, *, workspace_id: Optional[uuid.UUID] = None) -> list[Artifact]:
    ensure_runtime_context_pack_artifacts(db)
    query = db.query(Artifact).filter(Artifact.kind == "context-pack")
    if workspace_id:
        query = query.filter((Artifact.workspace_id == workspace_id) | (Artifact.workspace_id.is_(None)))
    return query.order_by(Artifact.storage_scope.asc(), Artifact.created_at.asc()).all()


def get_workspace_sync_target(db: Session, *, workspace_slug: str) -> str:
    value = ""
    row = db.query(WorkspaceSetting).filter(WorkspaceSetting.workspace_slug == workspace_slug).first()
    if row and row.artifact_sync_target:
        value = str(row.artifact_sync_target).strip()
    if not value:
        value = str(os.getenv("XYN_ARTIFACT_SYNC_TARGET", "")).strip()
    return value


def resolve_bound_context_pack_artifacts(
    db: Session,
    *,
    workspace: Workspace,
) -> tuple[list[Artifact], list[str]]:
    ensure_runtime_context_pack_artifacts(db)
    settings = db.query(WorkspaceSetting).filter(WorkspaceSetting.workspace_slug == workspace.slug).first()
    explicit_ids = settings.default_context_pack_artifact_ids_json if settings and isinstance(settings.default_context_pack_artifact_ids_json, list) else []
    warnings: list[str] = []
    rows: list[Artifact] = []
    if explicit_ids:
        rows = (
            db.query(Artifact)
            .filter(
                Artifact.kind == "context-pack",
                Artifact.id.in_([uuid.UUID(str(row)) for row in explicit_ids if str(row).strip()]),
            )
            .order_by(Artifact.created_at.asc())
            .all()
        )
    if not rows:
        rows = (
            db.query(Artifact)
            .filter(
                Artifact.kind == "context-pack",
                Artifact.workspace_id.is_(None),
                Artifact.storage_scope == "core-packaged",
            )
            .order_by(Artifact.created_at.asc())
            .all()
        )
        rows = [row for row in rows if bool(((row.extra_metadata or {}).get("bind_by_default")))]
        if rows:
            warnings.append("Using synchronized default context-pack bindings from the authoritative runtime manifest.")
    if any(bool(((row.extra_metadata or {}).get("source_fallback_used"))) for row in rows):
        warnings.append("Authoritative xyn-platform context-pack manifest not found; using xyn-core fallback definitions.")
    sync_target = get_workspace_sync_target(db, workspace_slug=workspace.slug)
    if not sync_target:
        warnings.append("No artifact sync target configured. Cross-instance portability requires published artifacts or explicit import bundles.")
    return rows, warnings


def set_workspace_context_pack_bindings(
    db: Session,
    *,
    workspace: Workspace,
    artifact_ids: list[uuid.UUID],
    artifact_sync_target: Optional[str] = None,
) -> WorkspaceSetting:
    row = db.query(WorkspaceSetting).filter(WorkspaceSetting.workspace_slug == workspace.slug).first()
    if row is None:
        row = WorkspaceSetting(
            workspace_slug=workspace.slug,
            default_artifact_registry_slug="default-registry",
            default_context_pack_artifact_ids_json=[],
        )
        db.add(row)
        db.flush()

    allowed = (
        db.query(Artifact)
        .filter(
            Artifact.kind == "context-pack",
            Artifact.id.in_(artifact_ids),
            ((Artifact.workspace_id == workspace.id) | (Artifact.workspace_id.is_(None))),
        )
        .all()
    )
    allowed_ids = [str(item.id) for item in allowed]
    row.default_context_pack_artifact_ids_json = allowed_ids
    if artifact_sync_target is not None:
        row.artifact_sync_target = str(artifact_sync_target).strip() or None
    row.updated_at = _utc_now()
    db.commit()
    db.refresh(row)
    return row
