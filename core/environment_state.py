from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.models import Activation, Environment, Sibling


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_slug(value: str, *, default: str) -> str:
    token = str(value or "").strip().lower()
    return token or default


def _merge_metadata(existing: Any, extra: Optional[dict[str, Any]]) -> dict[str, Any]:
    base = dict(existing) if isinstance(existing, dict) else {}
    if not isinstance(extra, dict):
        return base
    base.update({k: v for k, v in extra.items()})
    return base


def ensure_default_environment(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    workspace_slug: str,
) -> Environment:
    env = (
        db.query(Environment)
        .filter(Environment.workspace_id == workspace_id, Environment.slug == "development")
        .first()
    )
    if env:
        return env
    title_seed = str(workspace_slug or "development").strip().replace("-", " ").title() or "Development"
    env = Environment(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        slug="development",
        title=f"{title_seed} Development",
        kind="dev",
        status="active",
        is_ephemeral=False,
        metadata_json={"source": "phase0_write_through"},
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(env)
    db.flush()
    return env


def upsert_sibling_from_provision_output(
    db: Session,
    *,
    environment_id: uuid.UUID,
    workspace_id: uuid.UUID,
    sibling_name: str,
    provision_output: dict[str, Any],
    status: str,
    source_job_id: uuid.UUID | None = None,
    revision_anchor: Optional[dict[str, Any]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Sibling:
    output = provision_output if isinstance(provision_output, dict) else {}
    runtime_target = output.get("runtime_target") if isinstance(output.get("runtime_target"), dict) else {}
    runtime_registration = output.get("runtime_registration") if isinstance(output.get("runtime_registration"), dict) else {}
    runtime_instance = runtime_registration.get("instance") if isinstance(runtime_registration.get("instance"), dict) else {}
    installed_artifact = output.get("installed_artifact") if isinstance(output.get("installed_artifact"), dict) else {}

    workspace_app_instance_id = str(runtime_instance.get("id") or "").strip()
    installed_artifact_slug = str(installed_artifact.get("artifact_slug") or "").strip()
    compose_project = str(output.get("compose_project") or "").strip()
    deployment_id = str(output.get("deployment_id") or "").strip()

    sibling = None
    if workspace_app_instance_id:
        sibling = (
            db.query(Sibling)
            .filter(
                Sibling.workspace_id == workspace_id,
                Sibling.workspace_app_instance_id == workspace_app_instance_id,
            )
            .first()
        )
    if sibling is None and installed_artifact_slug:
        sibling = (
            db.query(Sibling)
            .filter(
                Sibling.workspace_id == workspace_id,
                Sibling.installed_artifact_slug == installed_artifact_slug,
            )
            .order_by(Sibling.updated_at.desc())
            .first()
        )
    if sibling is None and compose_project:
        sibling = (
            db.query(Sibling)
            .filter(
                Sibling.environment_id == environment_id,
                Sibling.compose_project == compose_project,
            )
            .first()
        )

    if sibling is None:
        sibling = Sibling(
            id=uuid.uuid4(),
            environment_id=environment_id,
            workspace_id=workspace_id,
            name=_safe_slug(sibling_name, default="sibling"),
            status=status,
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        db.add(sibling)

    sibling.environment_id = environment_id
    sibling.workspace_id = workspace_id
    sibling.name = _safe_slug(sibling_name or compose_project, default=sibling.name or "sibling")
    sibling.status = str(status or sibling.status or "provisioning")
    sibling.compose_project = compose_project or sibling.compose_project
    sibling.deployment_id = deployment_id or sibling.deployment_id
    sibling.ui_url = str(output.get("ui_url") or sibling.ui_url or "").strip() or sibling.ui_url
    sibling.api_url = str(output.get("api_url") or sibling.api_url or "").strip() or sibling.api_url
    sibling.runtime_target_json = runtime_target if isinstance(runtime_target, dict) else {}
    sibling.runtime_registration_json = runtime_registration if isinstance(runtime_registration, dict) else {}
    sibling.runtime_base_url = str(runtime_target.get("runtime_base_url") or sibling.runtime_base_url or "").strip() or sibling.runtime_base_url
    sibling.runtime_public_url = str(
        runtime_target.get("public_app_url")
        or runtime_target.get("app_url")
        or output.get("ui_url")
        or sibling.runtime_public_url
        or ""
    ).strip() or sibling.runtime_public_url
    sibling.installed_artifact_slug = installed_artifact_slug or sibling.installed_artifact_slug
    sibling.installed_artifact_version = str(
        installed_artifact.get("artifact_version")
        or installed_artifact.get("artifact_version_label")
        or sibling.installed_artifact_version
        or ""
    ).strip() or sibling.installed_artifact_version
    sibling.installed_artifact_revision_id = str(
        installed_artifact.get("artifact_revision_id")
        or sibling.installed_artifact_revision_id
        or ""
    ).strip() or sibling.installed_artifact_revision_id
    sibling.workspace_app_instance_id = workspace_app_instance_id or sibling.workspace_app_instance_id
    sibling.source_job_id = source_job_id or sibling.source_job_id
    sibling.last_seen_at = _utc_now()
    sibling.metadata_json = _merge_metadata(
        sibling.metadata_json,
        {
            **(metadata or {}),
            "revision_anchor": revision_anchor if isinstance(revision_anchor, dict) else {},
        },
    )
    sibling.updated_at = _utc_now()
    db.flush()
    return sibling


def create_or_update_activation(
    db: Session,
    *,
    environment_id: uuid.UUID,
    workspace_id: uuid.UUID,
    artifact_slug: str,
    status: str,
    activation_id: uuid.UUID | None = None,
    sibling_id: uuid.UUID | None = None,
    artifact_revision_id: str = "",
    artifact_version: str = "",
    workspace_app_instance_id: str = "",
    source_job_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
    capability_entry: Optional[dict[str, Any]] = None,
    requested_by: str = "system",
    error_text: str | None = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Activation:
    activation: Optional[Activation] = None
    if activation_id:
        activation = db.query(Activation).filter(Activation.id == activation_id).first()
    if activation is None and idempotency_key:
        activation = (
            db.query(Activation)
            .filter(
                Activation.environment_id == environment_id,
                Activation.idempotency_key == idempotency_key,
            )
            .first()
        )
    if activation is None:
        activation = Activation(
            id=activation_id or uuid.uuid4(),
            environment_id=environment_id,
            workspace_id=workspace_id,
            artifact_slug=str(artifact_slug or "unknown").strip() or "unknown",
            status=str(status or "pending").strip() or "pending",
            requested_by=str(requested_by or "system").strip() or "system",
            requested_at=_utc_now(),
            created_at=_utc_now(),
            updated_at=_utc_now(),
        )
        if idempotency_key:
            activation.idempotency_key = idempotency_key
        db.add(activation)

    activation.environment_id = environment_id
    activation.workspace_id = workspace_id
    activation.artifact_slug = str(artifact_slug or activation.artifact_slug or "unknown").strip() or "unknown"
    activation.status = str(status or activation.status or "pending").strip() or "pending"
    activation.sibling_id = sibling_id or activation.sibling_id
    activation.artifact_revision_id = str(artifact_revision_id or activation.artifact_revision_id or "").strip() or activation.artifact_revision_id
    activation.artifact_version = str(artifact_version or activation.artifact_version or "").strip() or activation.artifact_version
    activation.workspace_app_instance_id = str(
        workspace_app_instance_id or activation.workspace_app_instance_id or ""
    ).strip() or activation.workspace_app_instance_id
    activation.source_job_id = source_job_id or activation.source_job_id
    if capability_entry is not None:
        activation.capability_entry_json = capability_entry if isinstance(capability_entry, dict) else {}
    if error_text is not None:
        activation.error_text = str(error_text)
    if idempotency_key and not activation.idempotency_key:
        activation.idempotency_key = idempotency_key
    activation.metadata_json = _merge_metadata(activation.metadata_json, metadata)
    if activation.status == "smoke_passed":
        activation.activated_at = activation.activated_at or _utc_now()
    if activation.status == "failed":
        activation.failed_at = activation.failed_at or _utc_now()
    activation.updated_at = _utc_now()
    db.flush()
    return activation


def mark_activation_failed(
    db: Session,
    *,
    activation_id: uuid.UUID | str | None,
    error_text: str,
    source_job_id: uuid.UUID | None = None,
) -> Optional[Activation]:
    if not activation_id:
        return None
    try:
        activation_uuid = activation_id if isinstance(activation_id, uuid.UUID) else uuid.UUID(str(activation_id))
    except Exception:
        return None
    activation = db.query(Activation).filter(Activation.id == activation_uuid).first()
    if not activation:
        return None
    activation.status = "failed"
    activation.error_text = str(error_text or "")
    activation.failed_at = _utc_now()
    activation.updated_at = _utc_now()
    if source_job_id:
        activation.source_job_id = source_job_id
    if activation.sibling_id:
        sibling = db.query(Sibling).filter(Sibling.id == activation.sibling_id).first()
        if sibling:
            sibling.status = "failed"
            sibling.updated_at = _utc_now()
            sibling.last_seen_at = _utc_now()
    db.flush()
    return activation
