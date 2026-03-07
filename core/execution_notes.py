from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from core.context_packs import default_instance_workspace_root
from core.models import Artifact


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _notes_root() -> Path:
    root = Path(default_instance_workspace_root()).resolve() / "execution_notes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _serialize_payload(payload: dict[str, Any]) -> tuple[str, int]:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return text, len(text.encode("utf-8"))


def create_execution_note(
    db: Session,
    *,
    workspace_id: uuid.UUID | None,
    prompt_or_request: str,
    findings: list[str],
    root_cause: str,
    proposed_fix: str,
    implementation_summary: str,
    validation_summary: list[str] | None = None,
    debt_recorded: list[str] | None = None,
    related_artifact_ids: list[str] | None = None,
    status: str = "in_progress",
    created_by: str = "app-job-worker",
    extra_metadata: dict[str, Any] | None = None,
) -> Artifact:
    artifact_id = uuid.uuid4()
    payload = {
        "id": str(artifact_id),
        "timestamp": _utc_now().isoformat(),
        "workspace_id": str(workspace_id) if workspace_id else None,
        "related_artifact_ids": list(related_artifact_ids or []),
        "prompt_or_request": prompt_or_request,
        "findings": list(findings or []),
        "root_cause": root_cause,
        "proposed_fix": proposed_fix,
        "implementation_summary": implementation_summary,
        "validation_summary": list(validation_summary or []),
        "debt_recorded": list(debt_recorded or []),
        "status": status,
        "protocol_phases": [
            "Findings",
            "Root Cause / Current State",
            "Proposed Fix",
            "Implementation",
            "Validation",
            "Recorded Debt or Transitional Behavior",
        ],
    }
    text, byte_length = _serialize_payload(payload)
    path = _notes_root() / f"{artifact_id}.json"
    path.write_text(text, encoding="utf-8")
    row = Artifact(
        id=artifact_id,
        workspace_id=workspace_id,
        name=f"execution-note.{artifact_id}",
        kind="execution-note",
        storage_scope="instance-local",
        sync_state="local",
        content_type="application/json",
        byte_length=byte_length,
        sha256=None,
        created_by=created_by,
        storage_path=str(path),
        extra_metadata={
            "workspace_id": str(workspace_id) if workspace_id else None,
            "prompt_or_request": prompt_or_request,
            "related_artifact_ids": list(related_artifact_ids or []),
            "status": status,
            **(extra_metadata or {}),
        },
        created_at=_utc_now(),
    )
    db.add(row)
    db.flush()
    return row


def update_execution_note(
    db: Session,
    *,
    artifact_id: uuid.UUID,
    findings: list[str] | None = None,
    root_cause: str | None = None,
    proposed_fix: str | None = None,
    implementation_summary: str | None = None,
    validation_summary: list[str] | None = None,
    debt_recorded: list[str] | None = None,
    related_artifact_ids: list[str] | None = None,
    status: str | None = None,
    append_validation: list[str] | None = None,
    append_findings: list[str] | None = None,
    extra_metadata_updates: dict[str, Any] | None = None,
) -> Artifact | None:
    row = db.query(Artifact).filter(Artifact.id == artifact_id, Artifact.kind == "execution-note").first()
    if row is None or not row.storage_path:
        return None
    path = Path(row.storage_path)
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.setdefault("id", str(row.id))
    current.setdefault("timestamp", _utc_now().isoformat())
    current.setdefault("workspace_id", str(row.workspace_id) if row.workspace_id else None)
    current.setdefault("related_artifact_ids", [])
    current.setdefault("findings", [])
    current.setdefault("validation_summary", [])
    current.setdefault("debt_recorded", [])
    current.setdefault("protocol_phases", [
        "Findings",
        "Root Cause / Current State",
        "Proposed Fix",
        "Implementation",
        "Validation",
        "Recorded Debt or Transitional Behavior",
    ])
    if findings is not None:
        current["findings"] = list(findings)
    if append_findings:
        current["findings"] = list(current.get("findings") or []) + list(append_findings)
    if root_cause is not None:
        current["root_cause"] = root_cause
    if proposed_fix is not None:
        current["proposed_fix"] = proposed_fix
    if implementation_summary is not None:
        current["implementation_summary"] = implementation_summary
    if validation_summary is not None:
        current["validation_summary"] = list(validation_summary)
    if append_validation:
        current["validation_summary"] = list(current.get("validation_summary") or []) + list(append_validation)
    if debt_recorded is not None:
        current["debt_recorded"] = list(debt_recorded)
    if related_artifact_ids is not None:
        current["related_artifact_ids"] = list(related_artifact_ids)
    if status is not None:
        current["status"] = status
    current["updated_at"] = _utc_now().isoformat()
    text, byte_length = _serialize_payload(current)
    path.write_text(text, encoding="utf-8")
    row.byte_length = byte_length
    metadata = dict(row.extra_metadata) if isinstance(row.extra_metadata, dict) else {}
    metadata["related_artifact_ids"] = list(current.get("related_artifact_ids") or [])
    metadata["status"] = str(current.get("status") or metadata.get("status") or "in_progress")
    if extra_metadata_updates:
        metadata.update(extra_metadata_updates)
    row.extra_metadata = dict(metadata)
    db.flush()
    return row
