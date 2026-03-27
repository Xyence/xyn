from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from core.models import Artifact


PersistJsonArtifactFn = Callable[..., str]


def _base_metadata(*, workspace_id: uuid.UUID, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    return {"workspace_id": str(workspace_id), **(metadata or {})}


def persist_generated_json_artifact(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    kind: str,
    payload: dict[str, Any],
    metadata: Optional[dict[str, Any]] = None,
    workspace_root_factory: Callable[[], Path],
    now_fn: Callable[[], Any],
) -> str:
    specs_root = workspace_root_factory() / "app_specs"
    specs_root.mkdir(parents=True, exist_ok=True)
    artifact_id = uuid.uuid4()
    path = specs_root / f"{artifact_id}.json"
    text = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    row = Artifact(
        id=artifact_id,
        workspace_id=workspace_id,
        name=name,
        kind=kind,
        storage_scope="instance-local",
        sync_state="local",
        content_type="application/json",
        byte_length=len(text.encode("utf-8")),
        created_by="app-job-worker",
        storage_path=str(path),
        extra_metadata=_base_metadata(workspace_id=workspace_id, metadata=metadata),
        created_at=now_fn(),
    )
    db.add(row)
    db.flush()
    return str(row.id)


def persist_appspec_artifact(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    app_spec: dict[str, Any],
    job_id: str,
    inference_diagnostics: Optional[dict[str, Any]],
    persist_fn: PersistJsonArtifactFn,
) -> str:
    app_slug = str(app_spec.get("app_slug") or "")
    return persist_fn(
        db,
        workspace_id=workspace_id,
        name=f"appspec.{app_slug}",
        kind="app_spec",
        payload=app_spec,
        metadata={"job_id": job_id, "inference_diagnostics": inference_diagnostics},
    )


def persist_policy_artifact(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    app_slug: str,
    policy_bundle: dict[str, Any],
    job_id: str,
    app_spec_artifact_id: str,
    policy_slug_fn: Callable[[str], str],
    persist_fn: PersistJsonArtifactFn,
) -> str:
    return persist_fn(
        db,
        workspace_id=workspace_id,
        name=policy_slug_fn(str(app_slug or "generated-app")),
        kind="policy_bundle",
        payload=policy_bundle,
        metadata={"job_id": job_id, "app_spec_artifact_id": app_spec_artifact_id},
    )


def link_generated_artifact_memberships(*, _db: Session, **_kwargs: Any) -> list[str]:
    """Compatibility seam for repositories that implement app-level memberships.

    This repository currently has no generated application membership table in `core`.
    Keep this no-op helper to preserve a stable extraction boundary for future parity.
    """

    return []
