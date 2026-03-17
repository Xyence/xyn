"""Filesystem store for release artifacts (plans, revisions, operations)."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import uuid


def _workspace_root() -> Path:
    env_path = os.getenv("XYN_WORKSPACE_ROOT") or os.getenv("XYN_LOCAL_WORKSPACE_ROOT") or os.getenv("XYNSEED_WORKSPACE")
    if env_path:
        return Path(env_path)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "workspace"


def contracts_root() -> Path:
    env_path = os.getenv("XYNSEED_CONTRACTS_ROOT")
    if env_path:
        return Path(env_path)
    return Path(__file__).resolve().parents[3] / "xyn-contracts"


def artifacts_root() -> Path:
    return _ensure_dir(_workspace_root() / "artifacts")


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _release_root(release_id: str) -> Path:
    return _ensure_dir(_workspace_root() / "releases" / release_id)


def release_revisions_dir(release_id: str) -> Path:
    return _ensure_dir(_release_root(release_id) / "revisions")


def release_plans_dir(release_id: str) -> Path:
    return _ensure_dir(_release_root(release_id) / "plans")


def release_operations_dir(release_id: str) -> Path:
    return _ensure_dir(_release_root(release_id) / "operations")


def list_release_ids() -> list[str]:
    releases_root = _workspace_root() / "releases"
    if not releases_root.exists():
        return []
    return sorted([path.name for path in releases_root.iterdir() if path.is_dir()])


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def latest_revision(release_id: str) -> Optional[int]:
    revisions_dir = release_revisions_dir(release_id)
    candidates = []
    for child in revisions_dir.iterdir():
        if child.is_dir() and child.name.isdigit():
            candidates.append(int(child.name))
    return max(candidates) if candidates else None


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def write_text(path: Path, payload: str) -> None:
    path.write_text(payload)


def _safe_artifact_name(artifact_id: str) -> str:
    if artifact_id != Path(artifact_id).name or ".." in artifact_id:
        raise ValueError("Invalid artifact id")
    return artifact_id


def save_text_artifact(payload: str, suffix: str = ".log") -> str:
    artifact_id = f"{uuid.uuid4()}{suffix}"
    path = artifacts_root() / _safe_artifact_name(artifact_id)
    write_text(path, payload)
    return artifact_id


def load_artifact_text(artifact_id: str) -> Optional[str]:
    path = artifacts_root() / _safe_artifact_name(artifact_id)
    if not path.exists():
        return None
    return path.read_text()


def save_revision_artifacts(
    release_id: str,
    revision: int,
    release_spec: Dict[str, Any],
    runtime_spec: Dict[str, Any],
    compose_yaml: Optional[str]
) -> Dict[str, str]:
    revision_dir = _ensure_dir(release_revisions_dir(release_id) / str(revision))
    release_path = revision_dir / "release.json"
    runtime_path = revision_dir / "runtime.json"
    compose_path = revision_dir / "compose.yaml"

    write_json(release_path, release_spec)
    write_json(runtime_path, runtime_spec)
    if compose_yaml is not None:
        write_text(compose_path, compose_yaml)

    workspace_root = _workspace_root()
    artifacts = {
        "releaseSpecPath": str(release_path.relative_to(workspace_root)),
        "runtimeSpecPath": str(runtime_path.relative_to(workspace_root)),
    }
    if compose_yaml is not None:
        artifacts["composeYamlPath"] = str(compose_path.relative_to(workspace_root))
    return artifacts


def save_plan(
    release_id: str,
    plan_id: str,
    plan: Dict[str, Any]
) -> Path:
    plan_path = release_plans_dir(release_id) / f"{plan_id}.json"
    write_json(plan_path, plan)
    return plan_path


def save_operation(
    release_id: str,
    operation_id: str,
    operation: Dict[str, Any]
) -> Path:
    operation_path = release_operations_dir(release_id) / f"{operation_id}.json"
    write_json(operation_path, operation)
    return operation_path


def load_latest_runtime(release_id: str) -> Optional[Dict[str, Any]]:
    revision = latest_revision(release_id)
    if revision is None:
        return None
    runtime_path = release_revisions_dir(release_id) / str(revision) / "runtime.json"
    if not runtime_path.exists():
        return None
    return read_json(runtime_path)


def load_runtime_revision(release_id: str, revision: int) -> Optional[Dict[str, Any]]:
    runtime_path = release_revisions_dir(release_id) / str(revision) / "runtime.json"
    if not runtime_path.exists():
        return None
    return read_json(runtime_path)


def latest_compose_path(release_id: str) -> Optional[Path]:
    revision = latest_revision(release_id)
    if revision is None:
        return None
    candidate = release_revisions_dir(release_id) / str(revision) / "compose.yaml"
    if candidate.exists():
        return candidate
    return None


def load_plan(release_id: str, plan_id: str) -> Optional[Dict[str, Any]]:
    plan_path = release_plans_dir(release_id) / f"{plan_id}.json"
    if not plan_path.exists():
        return None
    return read_json(plan_path)


def load_operation(release_id: str, operation_id: str) -> Optional[Dict[str, Any]]:
    operation_path = release_operations_dir(release_id) / f"{operation_id}.json"
    if not operation_path.exists():
        return None
    return read_json(operation_path)


def find_operation(operation_id: str) -> Optional[Dict[str, Any]]:
    releases_root = _workspace_root() / "releases"
    if not releases_root.exists():
        return None
    for release_dir in releases_root.iterdir():
        if not release_dir.is_dir():
            continue
        operation_path = release_dir / "operations" / f"{operation_id}.json"
        if operation_path.exists():
            return read_json(operation_path)
    return None


def load_runtime_by_path(relative_path: str) -> Optional[Dict[str, Any]]:
    path = _workspace_root() / relative_path
    if not path.exists():
        return None
    return read_json(path)


def load_compose_path(relative_path: str) -> Path:
    return _workspace_root() / relative_path


def load_artifact_by_path(relative_path: str) -> Optional[str]:
    path = _workspace_root() / relative_path
    if not path.exists():
        return None
    return path.read_text()


def load_release_spec_revision(release_id: str, revision: int) -> Optional[Dict[str, Any]]:
    release_path = release_revisions_dir(release_id) / str(revision) / "release.json"
    if not release_path.exists():
        return None
    return read_json(release_path)


def load_latest_release_spec(release_id: str) -> Optional[Dict[str, Any]]:
    revision = latest_revision(release_id)
    if revision is None:
        return None
    return load_release_spec_revision(release_id, revision)


def latest_artifacts(release_id: str) -> Optional[Dict[str, str]]:
    revision = latest_revision(release_id)
    if revision is None:
        return None
    revision_dir = release_revisions_dir(release_id) / str(revision)
    release_path = revision_dir / "release.json"
    runtime_path = revision_dir / "runtime.json"
    compose_path = revision_dir / "compose.yaml"

    workspace_root = _workspace_root()
    artifacts: Dict[str, str] = {}
    if release_path.exists():
        artifacts["releaseSpecPath"] = str(release_path.relative_to(workspace_root))
    if runtime_path.exists():
        artifacts["runtimeSpecPath"] = str(runtime_path.relative_to(workspace_root))
    if compose_path.exists():
        artifacts["composeYamlPath"] = str(compose_path.relative_to(workspace_root))
    return artifacts


def list_release_operations(release_id: str) -> list[Dict[str, Any]]:
    operations_dir = release_operations_dir(release_id)
    if not operations_dir.exists():
        return []
    operations = []
    for path in operations_dir.glob("*.json"):
        try:
            operations.append(read_json(path))
        except json.JSONDecodeError:
            continue
    return sorted(operations, key=lambda op: op.get("createdAt", ""), reverse=True)


def find_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    releases_root = _workspace_root() / "releases"
    if not releases_root.exists():
        return None
    for release_dir in releases_root.iterdir():
        if not release_dir.is_dir():
            continue
        plan_path = release_dir / "plans" / f"{plan_id}.json"
        if plan_path.exists():
            return read_json(plan_path)
    return None


def _updated_at_iso(path: Path) -> str:
    return datetime.utcfromtimestamp(path.stat().st_mtime).isoformat() + "Z"


def updated_at_for_relative_path(relative_path: str) -> str:
    path = _workspace_root() / relative_path
    if not path.exists():
        return _now_iso()
    return _updated_at_iso(path)


def list_release_summaries() -> list[Dict[str, Any]]:
    summaries = []
    for release_id in list_release_ids():
        runtime = load_latest_runtime(release_id)
        if not runtime:
            continue
        release_meta = runtime.get("metadata", {})
        revision = runtime.get("release", {}).get("revision")
        backend = runtime.get("release", {}).get("backend", {}).get("type")
        revision_dir = release_revisions_dir(release_id) / str(revision)
        runtime_path = revision_dir / "runtime.json"
        updated_at = _updated_at_iso(runtime_path) if runtime_path.exists() else _now_iso()
        summaries.append({
            "releaseId": release_id,
            "name": release_meta.get("name"),
            "namespace": release_meta.get("namespace"),
            "desiredRevision": revision,
            "backend": backend,
            "updatedAt": updated_at
        })
    return sorted(summaries, key=lambda item: item["releaseId"])


__all__ = [
    "artifacts_root",
    "contracts_root",
    "latest_revision",
    "load_latest_runtime",
    "load_runtime_revision",
    "load_plan",
    "load_runtime_by_path",
    "load_artifact_by_path",
    "load_compose_path",
    "load_operation",
    "find_operation",
    "find_plan",
    "list_release_operations",
    "list_release_summaries",
    "updated_at_for_relative_path",
    "load_latest_release_spec",
    "latest_artifacts",
    "load_release_spec_revision",
    "save_text_artifact",
    "load_artifact_text",
    "list_release_ids",
    "latest_compose_path",
    "save_plan",
    "save_revision_artifacts",
    "save_operation",
    "_now_iso",
]
