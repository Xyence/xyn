from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from core.artifact_registry import (
    ensure_seed_default_registry,
    get_registry_spec_by_slug,
    get_workspace_default_registry_slug,
    list_registry_specs,
    resolve_registry_images,
    set_workspace_default_registry_slug,
    upsert_registry_spec,
    validate_registry_spec,
)
from core.database import SessionLocal

router = APIRouter(prefix="/api/v1", tags=["artifact-registries"])


class ArtifactRegistryPayload(BaseModel):
    slug: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    provider: str = Field(default="oci")
    endpoint: str = Field(..., min_length=1)
    visibility: str = Field(default="public")
    auth: Dict[str, Any] = Field(default_factory=lambda: {"mode": "none", "secret_ref": None})
    defaults: Dict[str, Any] = Field(default_factory=lambda: {"channel": "dev", "naming": {}})
    pin_policy: str = Field(default="tag")
    allow_insecure: bool = False
    notes: str = ""


class WorkspaceDefaultRegistryPayload(BaseModel):
    default_artifact_registry_slug: str = Field(..., min_length=1)


@router.get("/artifact-registries")
def artifact_registries_list() -> Dict[str, Any]:
    db = SessionLocal()
    try:
        ensure_seed_default_registry(db)
        return {"registries": list_registry_specs(db)}
    finally:
        db.close()


@router.post("/artifact-registries")
def artifact_registries_create(payload: ArtifactRegistryPayload) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        spec = validate_registry_spec(payload.model_dump())
        existing = get_registry_spec_by_slug(db, spec["slug"])
        if existing:
            raise HTTPException(status_code=400, detail=f"registry slug already exists: {spec['slug']}")
        created = upsert_registry_spec(db, spec, created_by="api")
        return {"registry": created}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.get("/artifact-registries/resolve")
def artifact_registries_resolve(
    registry_slug: Optional[str] = Query(default=None),
    workspace_slug: str = Query(default="default"),
    channel: Optional[str] = Query(default=None),
    ensure_local: bool = Query(default=False),
) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        result = resolve_registry_images(
            db,
            explicit_registry_slug=registry_slug,
            workspace_slug=workspace_slug,
            channel=channel,
            ensure_local=ensure_local,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        db.close()


@router.get("/artifact-registries/{registry_slug}")
def artifact_registries_detail(registry_slug: str) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        ensure_seed_default_registry(db)
        registry = get_registry_spec_by_slug(db, registry_slug)
        if not registry:
            raise HTTPException(status_code=404, detail="artifact registry not found")
        return {"registry": registry}
    finally:
        db.close()


@router.patch("/artifact-registries/{registry_slug}")
def artifact_registries_update(registry_slug: str, payload: ArtifactRegistryPayload) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        if not get_registry_spec_by_slug(db, registry_slug):
            raise HTTPException(status_code=404, detail="artifact registry not found")
        spec = validate_registry_spec(payload.model_dump())
        if spec["slug"] != registry_slug:
            raise HTTPException(status_code=400, detail="payload slug must match path slug")
        updated = upsert_registry_spec(db, spec, created_by="api")
        return {"registry": updated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()


@router.delete("/artifact-registries/{registry_slug}")
def artifact_registries_delete(registry_slug: str) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        from core.models import Artifact

        rows = db.query(Artifact).filter(Artifact.kind == "artifact-registry").all()
        target = None
        for row in rows:
            meta = row.extra_metadata if isinstance(row.extra_metadata, dict) else {}
            spec = meta.get("spec") if isinstance(meta.get("spec"), dict) else {}
            if str(spec.get("slug") or row.name).strip().lower() == registry_slug.strip().lower():
                target = row
                break
        if target is None:
            raise HTTPException(status_code=404, detail="artifact registry not found")
        db.delete(target)
        db.commit()
        return {"status": "deleted"}
    finally:
        db.close()


@router.post("/artifact-registries/{registry_slug}/test")
def artifact_registries_test(registry_slug: str, channel: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        result = resolve_registry_images(
            db,
            explicit_registry_slug=registry_slug,
            workspace_slug="default",
            channel=channel,
            ensure_local=False,
        )
        return {
            "ok": True,
            "registry_slug": result["registry_slug"],
            "endpoint": result["registry"].get("endpoint"),
            "images": result["images"],
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        db.close()


@router.get("/workspaces/{workspace_slug}/artifact-registry")
def workspace_default_artifact_registry_get(workspace_slug: str) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        ensure_seed_default_registry(db)
        default_slug = get_workspace_default_registry_slug(db, workspace_slug=workspace_slug) or "default-registry"
        result = resolve_registry_images(
            db,
            explicit_registry_slug=default_slug,
            workspace_slug=workspace_slug,
            ensure_local=False,
        )
        return {
            "workspace_slug": workspace_slug,
            "default_artifact_registry_slug": default_slug,
            "registry": result["registry"],
        }
    finally:
        db.close()


@router.patch("/workspaces/{workspace_slug}/artifact-registry")
def workspace_default_artifact_registry_set(workspace_slug: str, payload: WorkspaceDefaultRegistryPayload) -> Dict[str, Any]:
    db = SessionLocal()
    try:
        slug = set_workspace_default_registry_slug(db, workspace_slug=workspace_slug, registry_slug=payload.default_artifact_registry_slug)
        registry = get_registry_spec_by_slug(db, slug)
        return {
            "workspace_slug": workspace_slug,
            "default_artifact_registry_slug": slug,
            "registry": registry,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        db.close()
