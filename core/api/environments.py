"""Environment and sibling read/control APIs (Phase 1)."""
from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.access_control import (
    CAP_APP_READ,
    CAP_CAMPAIGNS_MANAGE,
    AccessPrincipal,
    enforce_access_or_403,
    require_capabilities,
)
from core.database import get_db
from core.environment_state import create_or_update_activation, upsert_sibling_from_provision_output
from core.db_tenancy import allocate_database
from core.lifecycle.service import LifecycleError, apply_transition
from core.models import Activation, Environment, Job, JobStatus, Sibling, Workspace
from core.sibling_runtime_control import (
    build_compact_sibling_payload,
    restart_project,
    stop_project,
)

router = APIRouter()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_slug(value: str, *, default: str) -> str:
    raw = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower())
    slug = re.sub(r"-{2,}", "-", raw).strip("-")
    return slug or default


def _provision_local_instance(
    *,
    name: str,
    force: bool,
    workspace_slug: str,
    database_url: str = "",
) -> dict[str, Any]:
    # Import lazily so this API surface remains importable in lean test/runtime
    # contexts where local provisioning routes are not loaded.
    from core.provisioning_local import ProvisionLocalRequest, provision_local_instance

    return provision_local_instance(
        ProvisionLocalRequest(
            name=name,
            force=force,
            workspace_slug=workspace_slug,
            database_url=str(database_url or "").strip() or None,
        )
    )


def _environment_payload(row: Environment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "workspace_id": str(row.workspace_id),
        "slug": row.slug,
        "title": row.title,
        "kind": row.kind,
        "status": row.status,
        "is_ephemeral": bool(row.is_ephemeral),
        "ttl_expires_at": row.ttl_expires_at.isoformat() if row.ttl_expires_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


class EnvironmentListResponse(BaseModel):
    environments: list[dict[str, Any]]


class EnvironmentResponse(BaseModel):
    id: str
    workspace_id: str
    slug: str
    title: str
    kind: str
    status: str
    is_ephemeral: bool
    ttl_expires_at: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


class EnvironmentCreateRequest(BaseModel):
    workspace_id: uuid.UUID
    slug: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=32)
    is_ephemeral: bool = False
    ttl_hours: Optional[int] = Field(default=None, ge=1)


class SiblingListResponse(BaseModel):
    siblings: list[dict[str, Any]]


class SpawnSiblingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    workspace_slug: Optional[str] = None
    force: bool = False


class SpawnSiblingResponse(BaseModel):
    operation: str
    sibling: dict[str, Any]
    provision_output: dict[str, Any]


class SiblingControlRequest(BaseModel):
    remove_volumes: bool = False


class SiblingControlResponse(BaseModel):
    operation: str
    sibling_id: str
    status: str


class ActivateArtifactRequest(BaseModel):
    artifact_slug: str = Field(min_length=1, max_length=255)
    revision_id: Optional[str] = Field(default=None, max_length=255)
    workspace_app_instance_id: Optional[str] = Field(default=None, max_length=255)
    idempotency_key: Optional[str] = Field(default=None, max_length=255)


class ActivateArtifactResponse(BaseModel):
    activation_id: str
    environment_id: str
    status: str
    job_id: str
    job_type: str


def _job_log_prefix() -> str:
    return f"[{_utc_now().isoformat()}] Queued by environments API."


def _activation_response(db: Session, activation: Activation) -> ActivateArtifactResponse:
    job_type = "deploy_app_local"
    job_id = str(activation.source_job_id or "")
    if activation.source_job_id:
        source_job = db.query(Job).filter(Job.id == activation.source_job_id).first()
        if source_job:
            job_type = str(source_job.type or job_type)
            job_id = str(source_job.id)
    return ActivateArtifactResponse(
        activation_id=str(activation.id),
        environment_id=str(activation.environment_id),
        status=str(activation.status or "pending"),
        job_id=job_id,
        job_type=job_type,
    )


def _build_generate_match_payload(
    *,
    job: Job,
) -> dict[str, Any]:
    output = job.output_json if isinstance(job.output_json, dict) else {}
    app_spec = output.get("app_spec") if isinstance(output.get("app_spec"), dict) else {}
    policy_bundle = output.get("policy_bundle") if isinstance(output.get("policy_bundle"), dict) else {}
    generated_artifact = output.get("generated_artifact") if isinstance(output.get("generated_artifact"), dict) else {}
    package_path = str(generated_artifact.get("artifact_package_path") or "").strip()
    return {
        "source_job_id": str(job.id),
        "app_spec": app_spec,
        "policy_bundle": policy_bundle,
        "generated_artifact": generated_artifact,
        "app_spec_artifact_id": str(output.get("app_spec_artifact_id") or "").strip(),
        "policy_bundle_artifact_id": str(output.get("policy_bundle_artifact_id") or "").strip(),
        "policy_source": str(output.get("policy_source") or "").strip(),
        "policy_artifact_ref": output.get("policy_artifact_ref") if isinstance(output.get("policy_artifact_ref"), dict) else {},
        "policy_compatibility": str(output.get("policy_compatibility") or "").strip(),
        "policy_compatibility_reason": str(output.get("policy_compatibility_reason") or "").strip(),
        "execution_note_artifact_id": str(output.get("execution_note_artifact_id") or "").strip(),
        "artifact_package_path": package_path,
    }


def _find_generated_build_payload(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    artifact_slug: str,
    revision_id: str,
) -> dict[str, Any]:
    rows = (
        db.query(Job)
        .filter(
            Job.workspace_id == workspace_id,
            Job.type == "generate_app_spec",
            Job.status == JobStatus.SUCCEEDED.value,
        )
        .order_by(Job.updated_at.desc())
        .all()
    )
    expected_slug = str(artifact_slug or "").strip()
    expected_revision = str(revision_id or "").strip()
    for row in rows:
        payload = _build_generate_match_payload(job=row)
        generated_artifact = payload.get("generated_artifact") if isinstance(payload.get("generated_artifact"), dict) else {}
        candidate_slug = str(generated_artifact.get("artifact_slug") or "").strip()
        if candidate_slug != expected_slug:
            continue
        candidate_revision = str(
            generated_artifact.get("revision_id")
            or generated_artifact.get("artifact_revision_id")
            or ""
        ).strip()
        if expected_revision and candidate_revision != expected_revision:
            continue
        package_path = str(payload.get("artifact_package_path") or "").strip()
        if not package_path:
            continue
        if not Path(package_path).expanduser().exists():
            continue
        return payload
    raise HTTPException(
        status_code=404,
        detail=(
            f"No successful generate_app_spec output found for artifact_slug={expected_slug}"
            + (f" revision_id={expected_revision}" if expected_revision else "")
        ),
    )


@router.get("/environments", response_model=EnvironmentListResponse)
async def list_environments(
    workspace_id: Optional[uuid.UUID] = Query(default=None),
    workspace_slug: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_APP_READ)),
    db: Session = Depends(get_db),
):
    query = db.query(Environment)
    if workspace_id:
        query = query.filter(Environment.workspace_id == workspace_id)
    if workspace_slug:
        normalized_workspace_slug = str(workspace_slug or "").strip().lower()
        query = query.join(Workspace, Workspace.id == Environment.workspace_id).filter(Workspace.slug == normalized_workspace_slug)
    if kind:
        query = query.filter(Environment.kind == str(kind).strip().lower())
    if status:
        query = query.filter(Environment.status == str(status).strip().lower())
    rows = query.order_by(Environment.created_at.desc()).all()
    return EnvironmentListResponse(environments=[_environment_payload(row) for row in rows])


@router.post("/environments", response_model=EnvironmentResponse, status_code=201)
async def create_environment(
    payload: EnvironmentCreateRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    workspace = db.query(Workspace).filter(Workspace.id == payload.workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="workspace not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)

    slug = _normalize_slug(payload.slug, default="environment")
    title = str(payload.title or "").strip()
    kind = str(payload.kind or "").strip().lower()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if not kind:
        raise HTTPException(status_code=400, detail="kind is required")

    ttl_expires_at = None
    if payload.is_ephemeral and payload.ttl_hours:
        ttl_expires_at = _utc_now() + timedelta(hours=int(payload.ttl_hours))

    row = Environment(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        slug=slug,
        title=title,
        kind=kind,
        status="active",
        is_ephemeral=bool(payload.is_ephemeral),
        ttl_expires_at=ttl_expires_at,
        metadata_json={},
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="environment slug already exists for workspace") from exc
    db.refresh(row)
    return _environment_payload(row)


@router.get("/environments/{env_id}/siblings", response_model=SiblingListResponse)
async def list_environment_siblings(
    env_id: uuid.UUID,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_APP_READ)),
    db: Session = Depends(get_db),
):
    environment = db.query(Environment).filter(Environment.id == env_id).first()
    if not environment:
        raise HTTPException(status_code=404, detail="environment not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_APP_READ], workspace_id=environment.workspace_id)
    rows = (
        db.query(Sibling)
        .filter(Sibling.environment_id == env_id)
        .order_by(Sibling.created_at.desc())
        .all()
    )
    return SiblingListResponse(siblings=[build_compact_sibling_payload(row) for row in rows])


@router.post("/environments/{env_id}/siblings/spawn", response_model=SpawnSiblingResponse)
async def spawn_environment_sibling(
    env_id: uuid.UUID,
    payload: SpawnSiblingRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    environment = db.query(Environment).filter(Environment.id == env_id).first()
    if not environment:
        raise HTTPException(status_code=404, detail="environment not found")
    workspace = db.query(Workspace).filter(Workspace.id == environment.workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="workspace not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)

    sibling_name = _normalize_slug(payload.name, default="sibling")
    existing = (
        db.query(Sibling)
        .filter(
            Sibling.environment_id == environment.id,
            Sibling.name == sibling_name,
            Sibling.status.in_(["provisioning", "ready", "active"]),
        )
        .order_by(Sibling.updated_at.desc())
        .first()
    )
    if existing and not payload.force:
        return SpawnSiblingResponse(
            operation="spawn_sibling",
            sibling=build_compact_sibling_payload(existing),
            provision_output={
                "status": "existing",
                "message": "active sibling already exists in environment",
            },
        )

    requested_workspace_slug = str(payload.workspace_slug or workspace.slug or "").strip().lower() or workspace.slug
    try:
        db_allocation = allocate_database(
            environment_id=environment.id,
            sibling_id=uuid.uuid5(uuid.NAMESPACE_URL, f"{environment.id}:{sibling_name}"),
            workspace_id=workspace.id,
            sibling_name=sibling_name,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    db_allocation_public = db_allocation.to_public_dict() if hasattr(db_allocation, "to_public_dict") else {}
    provision_output = _provision_local_instance(
        name=sibling_name,
        force=bool(payload.force),
        workspace_slug=requested_workspace_slug,
        database_url=str(getattr(db_allocation, "database_url", "") or ""),
    )
    if isinstance(db_allocation_public, dict) and db_allocation_public:
        provision_output["database_allocation"] = db_allocation_public
    sibling_status = "ready" if str(provision_output.get("status") or "").strip().lower() in {"succeeded", "reused"} else "provisioning"
    sibling = upsert_sibling_from_provision_output(
        db,
        environment_id=environment.id,
        workspace_id=workspace.id,
        sibling_name=sibling_name,
        provision_output=provision_output,
        status=sibling_status,
        metadata={
            "spawned_by": "phase1_api",
            "database_allocation": db_allocation_public if isinstance(db_allocation_public, dict) else {},
        },
    )
    db.commit()
    db.refresh(sibling)

    return SpawnSiblingResponse(
        operation="spawn_sibling",
        sibling=build_compact_sibling_payload(sibling),
        provision_output=provision_output,
    )


@router.post("/environments/{env_id}/activate-artifact", response_model=ActivateArtifactResponse)
async def activate_environment_artifact(
    env_id: uuid.UUID,
    payload: ActivateArtifactRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    environment = db.query(Environment).filter(Environment.id == env_id).first()
    if not environment:
        raise HTTPException(status_code=404, detail="environment not found")
    workspace = db.query(Workspace).filter(Workspace.id == environment.workspace_id).first()
    if not workspace:
        raise HTTPException(status_code=404, detail="workspace not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=workspace.id)

    artifact_slug = str(payload.artifact_slug or "").strip()
    if not artifact_slug:
        raise HTTPException(status_code=400, detail="artifact_slug is required")
    revision_id = str(payload.revision_id or "").strip()
    workspace_app_instance_id = str(payload.workspace_app_instance_id or "").strip()
    idempotency_key = str(payload.idempotency_key or "").strip() or None

    if idempotency_key:
        existing = (
            db.query(Activation)
            .filter(
                Activation.environment_id == environment.id,
                Activation.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing:
            return _activation_response(db, existing)

    requested_by = str(getattr(principal, "subject_id", "") or "").strip() or "system"
    try:
        activation = create_or_update_activation(
            db,
            environment_id=environment.id,
            workspace_id=workspace.id,
            artifact_slug=artifact_slug,
            artifact_revision_id=revision_id,
            workspace_app_instance_id=workspace_app_instance_id,
            status="pending",
            idempotency_key=idempotency_key,
            requested_by=requested_by,
        )
    except IntegrityError as exc:
        db.rollback()
        if idempotency_key:
            existing = (
                db.query(Activation)
                .filter(
                    Activation.environment_id == environment.id,
                    Activation.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing:
                return _activation_response(db, existing)
        raise HTTPException(status_code=409, detail="activation idempotency conflict") from exc

    db.commit()
    db.refresh(activation)

    try:
        build_payload = _find_generated_build_payload(
            db,
            workspace_id=workspace.id,
            artifact_slug=artifact_slug,
            revision_id=revision_id,
        )
    except HTTPException as exc:
        create_or_update_activation(
            db,
            environment_id=environment.id,
            workspace_id=workspace.id,
            activation_id=activation.id,
            artifact_slug=artifact_slug,
            artifact_revision_id=revision_id,
            workspace_app_instance_id=workspace_app_instance_id,
            status="failed",
            requested_by=requested_by,
            error_text=str(exc.detail),
        )
        db.commit()
        raise
    app_spec = copy.deepcopy(
        build_payload.get("app_spec") if isinstance(build_payload.get("app_spec"), dict) else {}
    )
    policy_bundle = copy.deepcopy(
        build_payload.get("policy_bundle") if isinstance(build_payload.get("policy_bundle"), dict) else {}
    )
    generated_artifact = copy.deepcopy(
        build_payload.get("generated_artifact") if isinstance(build_payload.get("generated_artifact"), dict) else {}
    )
    if not app_spec or not policy_bundle or not generated_artifact:
        create_or_update_activation(
            db,
            environment_id=environment.id,
            workspace_id=workspace.id,
            activation_id=activation.id,
            artifact_slug=artifact_slug,
            artifact_revision_id=revision_id,
            workspace_app_instance_id=workspace_app_instance_id,
            status="failed",
            requested_by=requested_by,
            error_text="Historical generate_app_spec payload missing app_spec/policy_bundle/generated_artifact.",
        )
        db.commit()
        raise HTTPException(status_code=404, detail="matching generated build payload is incomplete")

    if workspace_app_instance_id:
        revision_anchor = app_spec.get("revision_anchor") if isinstance(app_spec.get("revision_anchor"), dict) else {}
        revision_anchor = {
            **revision_anchor,
            "workspace_id": str(workspace.id),
            "workspace_slug": str(workspace.slug or ""),
            "artifact_slug": artifact_slug,
            "workspace_app_instance_id": workspace_app_instance_id,
        }
        app_spec["revision_anchor"] = revision_anchor

    deploy_input: dict[str, Any] = {
        "app_spec": app_spec,
        "policy_bundle": policy_bundle,
        "generated_artifact": generated_artifact,
        "app_spec_artifact_id": str(build_payload.get("app_spec_artifact_id") or "").strip(),
        "policy_bundle_artifact_id": str(build_payload.get("policy_bundle_artifact_id") or "").strip(),
        "policy_source": str(build_payload.get("policy_source") or "reconstructed").strip() or "reconstructed",
        "policy_artifact_ref": build_payload.get("policy_artifact_ref") if isinstance(build_payload.get("policy_artifact_ref"), dict) else {},
        "policy_compatibility": str(build_payload.get("policy_compatibility") or "unknown").strip() or "unknown",
        "policy_compatibility_reason": str(build_payload.get("policy_compatibility_reason") or "").strip(),
        "execution_note_artifact_id": str(build_payload.get("execution_note_artifact_id") or "").strip(),
        "source_job_id": str(build_payload.get("source_job_id") or "").strip(),
        "environment_id": str(environment.id),
        "activation_id": str(activation.id),
    }

    job = Job(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        type="deploy_app_local",
        status=JobStatus.QUEUED.value,
        input_json=deploy_input,
        output_json={},
        logs_text=_job_log_prefix(),
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
            actor=requested_by,
            reason="Job created from environment artifact activation.",
            metadata={
                "activation_id": str(activation.id),
                "environment_id": str(environment.id),
                "artifact_slug": artifact_slug,
                "artifact_revision_id": revision_id,
            },
        )
    except LifecycleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(job)
    create_or_update_activation(
        db,
        environment_id=environment.id,
        workspace_id=workspace.id,
        activation_id=activation.id,
        artifact_slug=artifact_slug,
        artifact_revision_id=revision_id,
        workspace_app_instance_id=workspace_app_instance_id,
        status="pending",
        source_job_id=job.id,
        requested_by=requested_by,
    )
    db.commit()
    db.refresh(activation)
    return _activation_response(db, activation)


@router.post("/siblings/{sibling_id}/restart", response_model=SiblingControlResponse)
async def restart_sibling(
    sibling_id: uuid.UUID,
    payload: SiblingControlRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    row = db.query(Sibling).filter(Sibling.id == sibling_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="sibling not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=row.workspace_id)
    compose_project = str(row.compose_project or "").strip()
    if not compose_project:
        raise HTTPException(status_code=400, detail="sibling compose_project is not set")
    ok, message = restart_project(compose_project)
    if not ok:
        raise HTTPException(status_code=409, detail=message or "sibling restart failed")

    row.status = "active"
    row.last_seen_at = _utc_now()
    row.updated_at = _utc_now()
    db.commit()

    return SiblingControlResponse(
        operation="restart",
        sibling_id=str(row.id),
        status=row.status,
    )


@router.post("/siblings/{sibling_id}/stop", response_model=SiblingControlResponse)
async def stop_sibling(
    sibling_id: uuid.UUID,
    payload: SiblingControlRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_CAMPAIGNS_MANAGE)),
    db: Session = Depends(get_db),
):
    row = db.query(Sibling).filter(Sibling.id == sibling_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="sibling not found")
    enforce_access_or_403(principal, required_capabilities=[CAP_CAMPAIGNS_MANAGE], workspace_id=row.workspace_id)
    compose_project = str(row.compose_project or "").strip()
    if not compose_project:
        raise HTTPException(status_code=400, detail="sibling compose_project is not set")
    ok, message = stop_project(compose_project, remove_volumes=bool(payload.remove_volumes))
    if not ok:
        raise HTTPException(status_code=409, detail=message or "sibling stop failed")

    row.status = "stopped"
    row.last_seen_at = _utc_now()
    row.updated_at = _utc_now()
    db.commit()

    return SiblingControlResponse(
        operation="stop",
        sibling_id=str(row.id),
        status=row.status,
    )
