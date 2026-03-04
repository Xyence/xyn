from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.artifact_registry import resolve_registry_images
from core.database import SessionLocal
from core.models import Artifact

router = APIRouter(prefix="/api/v1/provision", tags=["provision"])

DEFAULT_ARTIFACT_REGISTRY = "public.ecr.aws/i0h0h0n4/xyn/artifacts"
DEFAULT_UI_IMAGE_NAME = "xyn-ui"
DEFAULT_API_IMAGE_NAME = "xyn-api"
DEFAULT_IMAGE_TAG = "dev"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _deployments_root() -> Path:
    root = Path(os.getenv("XYN_LOCAL_DEPLOYMENTS_ROOT", ".xyn/deployments")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workspace_root() -> Path:
    return Path(os.getenv("XYN_LOCAL_WORKSPACE_ROOT", ".xyn/workspace")).resolve()


def _state_path(deploy_dir: Path) -> Path:
    return deploy_dir / "deployment_state.json"


def _sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return slug or "local"


def _used_host_ports() -> set[int]:
    code, stdout, _ = _run(["docker", "ps", "--format", "{{.Ports}}"])
    if code != 0:
        return set()
    used: set[int] = set()
    for line in (stdout or "").splitlines():
        for match in re.findall(r":(\d+)->", line):
            try:
                used.add(int(match))
            except ValueError:
                continue
    return used


def _find_free_port(start: int = 42000, end: int = 42999, used_ports: Optional[set[int]] = None) -> int:
    used = used_ports or set()
    for port in range(start, end + 1):
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free port available in allocation range")


def _compose_yaml(project: str, ui_port: int, api_port: int, *, ui_image: str, api_image: str) -> str:
    return f"""services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: xyn
      POSTGRES_USER: xyn
      POSTGRES_PASSWORD: xyn_dev_password
    volumes:
      - db_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    restart: unless-stopped

  backend:
    image: {api_image}
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql://xyn:xyn_dev_password@db:5432/xyn
      REDIS_URL: redis://redis:6379/0
      XYN_ENV: local
      XYN_AI_PROVIDER: ${{XYN_AI_PROVIDER:-openai}}
      XYN_AI_MODEL: ${{XYN_AI_MODEL:-gpt-5-mini}}
      XYN_OPENAI_API_KEY: ${{XYN_OPENAI_API_KEY:-}}
      XYN_GEMINI_API_KEY: ${{XYN_GEMINI_API_KEY:-}}
      XYN_ANTHROPIC_API_KEY: ${{XYN_ANTHROPIC_API_KEY:-}}
    ports:
      - "{api_port}:8000"
    depends_on:
      - db
      - redis

  ui:
    image: {ui_image}
    restart: unless-stopped
    environment:
      VITE_API_BASE_URL: http://localhost:{api_port}
    ports:
      - "{ui_port}:80"
    depends_on:
      - backend

volumes:
  db_data:
"""


def _run(cmd: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _compose_cmd() -> list[str]:
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]


def _write_text_artifact(db: Session, *, name: str, kind: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    artifact_id = str(uuid.uuid4())
    filename = f"{artifact_id}.txt"
    path = root / filename
    path.write_text(content, encoding="utf-8")
    row = Artifact(
        id=uuid.UUID(artifact_id),
        name=name,
        kind=kind,
        content_type="text/plain",
        byte_length=len(content.encode("utf-8")),
        created_by="xyn-seed",
        storage_path=str(path),
        extra_metadata=metadata or {},
        created_at=_utc_now(),
    )
    db.add(row)
    db.commit()
    return artifact_id


def _write_json_artifact(db: Session, *, name: str, kind: str, payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> str:
    text = json.dumps(payload, indent=2, sort_keys=True)
    root = _workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    artifact_id = str(uuid.uuid4())
    filename = f"{artifact_id}.json"
    path = root / filename
    path.write_text(text, encoding="utf-8")
    row = Artifact(
        id=uuid.UUID(artifact_id),
        name=name,
        kind=kind,
        content_type="application/json",
        byte_length=len(text.encode("utf-8")),
        created_by="xyn-seed",
        storage_path=str(path),
        extra_metadata=metadata or {},
        created_at=_utc_now(),
    )
    db.add(row)
    db.commit()
    return artifact_id


@dataclass
class DevChangeResult:
    job_artifact_id: str
    job_run_artifact_id: str
    patch_artifact_id: str
    logs_artifact_id: str
    branch_name: str
    commit_sha: str
    status: str


def _run_dev_change_job(db: Session, *, prompt: str, repo_ref: str, deployment_id: str) -> DevChangeResult:
    repo_path = Path(repo_ref).expanduser().resolve()
    if not repo_path.exists() or not (repo_path / ".git").exists():
        raise RuntimeError(f"repo_ref is not a git repository: {repo_ref}")
    branch = f"xyn/{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{_sanitize_slug(prompt)[:32]}"

    prompt_text = prompt.strip()
    ai_provider = os.getenv("XYN_AI_PROVIDER", "openai")
    ai_model = os.getenv("XYN_AI_MODEL", "gpt-5-mini")
    note_text = (
        "dev.change job scaffold\n"
        f"provider={ai_provider}\nmodel={ai_model}\n"
        f"prompt={prompt_text}\n"
        "This demo runner records patch/branch outputs and never mutates the running instance.\n"
    )

    run_log: list[str] = []
    code, out, err = _run(["git", "checkout", "-B", branch], cwd=repo_path)
    run_log.append(f"$ git checkout -B {branch}\n{out}\n{err}")
    if code != 0:
        raise RuntimeError(f"git checkout failed: {err or out}")

    changes_dir = repo_path / ".xyn" / "jobs"
    changes_dir.mkdir(parents=True, exist_ok=True)
    change_file = changes_dir / f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{_sanitize_slug(prompt_text)[:24]}.md"
    change_file.write_text(note_text, encoding="utf-8")

    _run(["git", "add", str(change_file)], cwd=repo_path)
    code, out, err = _run(["git", "commit", "-m", f"xyn job: {prompt_text[:72]}"], cwd=repo_path)
    run_log.append(f"$ git commit -m ...\n{out}\n{err}")
    if code != 0:
        raise RuntimeError(f"git commit failed: {err or out}")

    _, commit_sha, _ = _run(["git", "rev-parse", "HEAD"], cwd=repo_path)
    _, patch_text, _ = _run(["git", "show", "--no-color", "--stat=200,120,5", "HEAD"], cwd=repo_path)
    logs_text = "\n\n".join(run_log)

    job_artifact_id = _write_json_artifact(
        db,
        name=f"job.dev-change.{branch}",
        kind="job",
        payload={
            "schema_version": "xyn.job.v1",
            "kind": "dev.change",
            "prompt": prompt_text,
            "repo_ref": str(repo_path),
            "deployment_id": deployment_id,
        },
        metadata={"surface": {"label": "Job", "path": f"/jobs/{branch}"}},
    )
    patch_artifact_id = _write_text_artifact(
        db,
        name=f"patch.{branch}",
        kind="patch",
        content=patch_text,
        metadata={"branch": branch, "commit_sha": commit_sha},
    )
    logs_artifact_id = _write_text_artifact(
        db,
        name=f"job.logs.{branch}",
        kind="log",
        content=logs_text,
        metadata={"branch": branch, "commit_sha": commit_sha},
    )
    job_run_artifact_id = _write_json_artifact(
        db,
        name=f"job-run.dev-change.{branch}",
        kind="job_run",
        payload={
            "schema_version": "xyn.job_run.v1",
            "status": "succeeded",
            "job_kind": "dev.change",
            "branch_name": branch,
            "commit_sha": commit_sha,
            "patch_artifact_id": patch_artifact_id,
            "logs_artifact_id": logs_artifact_id,
            "deployment_id": deployment_id,
            "completed_at": _utc_now().isoformat(),
        },
        metadata={"surface": {"label": "Job Run", "path": f"/job-runs/{branch}"}},
    )
    return DevChangeResult(
        job_artifact_id=job_artifact_id,
        job_run_artifact_id=job_run_artifact_id,
        patch_artifact_id=patch_artifact_id,
        logs_artifact_id=logs_artifact_id,
        branch_name=branch,
        commit_sha=commit_sha,
        status="succeeded",
    )


class LocalDevChangeInput(BaseModel):
    prompt: str = Field(..., min_length=3)
    repo_ref: str = Field(..., min_length=1)


class ProvisionLocalRequest(BaseModel):
    name: Optional[str] = None
    force: bool = False
    job: Optional[LocalDevChangeInput] = None
    ui_image: Optional[str] = None
    api_image: Optional[str] = None
    registry_slug: Optional[str] = None
    channel: Optional[str] = None
    workspace_slug: Optional[str] = None


def _load_state(deploy_dir: Path) -> Optional[Dict[str, Any]]:
    path = _state_path(deploy_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_state(deploy_dir: Path, state: Dict[str, Any]) -> None:
    _state_path(deploy_dir).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _ensure_required_artifacts() -> list[dict[str, str]]:
    roots = [Path("/home/ubuntu/src")]
    env_roots = str(os.getenv("XYN_KERNEL_MANIFEST_ROOTS") or "").strip()
    if env_roots:
        roots = [Path(item.strip()) for item in env_roots.split(",") if item.strip()]
    expected = [
        ("xyn-api", "xyn-api/artifact.manifest.json"),
        ("xyn-ui", "xyn-ui/artifact.manifest.json"),
    ]
    ensured: list[dict[str, str]] = []
    for slug, rel in expected:
        found = None
        for root in roots:
            candidate = (root / rel).resolve()
            if candidate.exists():
                found = candidate
                break
        if not found:
            continue
        try:
            manifest = json.loads(found.read_text(encoding="utf-8"))
            artifact_meta = manifest.get("artifact") if isinstance(manifest, dict) else {}
            version = str((artifact_meta or {}).get("version") or "unknown")
        except Exception:
            version = "unknown"
        ensured.append({"slug": slug, "version": version, "manifest_path": str(found)})
    return ensured


def _artifact_image_defaults() -> dict[str, str]:
    registry = str(os.getenv("XYN_ARTIFACT_REGISTRY", "")).strip() or DEFAULT_ARTIFACT_REGISTRY
    ui_image = str(os.getenv("XYN_UI_IMAGE", "")).strip() or f"{registry}/{DEFAULT_UI_IMAGE_NAME}:{DEFAULT_IMAGE_TAG}"
    api_image = str(os.getenv("XYN_API_IMAGE", "")).strip() or f"{registry}/{DEFAULT_API_IMAGE_NAME}:{DEFAULT_IMAGE_TAG}"
    return {
        "registry": registry,
        "ui_image": ui_image,
        "api_image": api_image,
    }


def _context_has_dockerfile(path_text: str) -> bool:
    candidate = Path(path_text).expanduser().resolve()
    return candidate.is_dir() and (candidate / "Dockerfile").exists()


def _resolve_images_for_provision(request: ProvisionLocalRequest) -> dict[str, Any]:
    defaults = _artifact_image_defaults()
    operations: list[str] = []

    requested_ui = str(request.ui_image or "").strip()
    requested_api = str(request.api_image or "").strip()
    if requested_ui and requested_api:
        return {
            "mode": "explicit",
            "registry": defaults["registry"],
            "ui_image": requested_ui,
            "api_image": requested_api,
            "registry_slug": None,
            "registry_source": "explicit",
            "channel": str(request.channel or DEFAULT_IMAGE_TAG).strip() or DEFAULT_IMAGE_TAG,
            "operations": operations,
        }

    local_ui_context = str(os.getenv("XYN_LOCAL_UI_CONTEXT", "")).strip()
    local_api_context = str(os.getenv("XYN_LOCAL_API_CONTEXT", "")).strip()
    local_contexts_ready = bool(local_ui_context and local_api_context)
    if local_contexts_ready and _context_has_dockerfile(local_ui_context) and _context_has_dockerfile(local_api_context):
        code, _, stderr = _run(["docker", "build", "-t", DEFAULT_API_IMAGE_NAME, local_api_context])
        if code != 0:
            raise RuntimeError(f"Failed to build local API context: {stderr or local_api_context}")
        operations.append(f"Built local image {DEFAULT_API_IMAGE_NAME} from {local_api_context}")

        code, _, stderr = _run(["docker", "build", "-t", DEFAULT_UI_IMAGE_NAME, local_ui_context])
        if code != 0:
            raise RuntimeError(f"Failed to build local UI context: {stderr or local_ui_context}")
        operations.append(f"Built local image {DEFAULT_UI_IMAGE_NAME} from {local_ui_context}")

        return {
            "mode": "local_build",
            "registry": defaults["registry"],
            "ui_image": DEFAULT_UI_IMAGE_NAME,
            "api_image": DEFAULT_API_IMAGE_NAME,
            "registry_slug": None,
            "registry_source": "local_build",
            "channel": str(request.channel or DEFAULT_IMAGE_TAG).strip() or DEFAULT_IMAGE_TAG,
            "operations": operations,
        }
    db = SessionLocal()
    try:
        resolved = resolve_registry_images(
            db,
            explicit_registry_slug=str(request.registry_slug or "").strip() or None,
            workspace_slug=str(request.workspace_slug or "default").strip() or "default",
            channel=str(request.channel or "").strip() or None,
            ensure_local=True,
        )
    finally:
        db.close()

    operations.extend(list(resolved.get("operations") or []))
    images = resolved.get("images") if isinstance(resolved.get("images"), dict) else {}
    return {
        "mode": "artifact_registry",
        "registry": str((resolved.get("registry") or {}).get("endpoint") or defaults["registry"]),
        "ui_image": str(images.get("ui_image") or defaults["ui_image"]),
        "api_image": str(images.get("api_image") or defaults["api_image"]),
        "registry_slug": str(resolved.get("registry_slug") or ""),
        "registry_source": str(resolved.get("registry_source") or ""),
        "channel": str(images.get("channel") or DEFAULT_IMAGE_TAG),
        "registry_spec": resolved.get("registry") if isinstance(resolved.get("registry"), dict) else {},
        "operations": operations,
    }


@router.post("/local-instance")
def provision_local_instance(request: ProvisionLocalRequest) -> Dict[str, Any]:
    project_suffix = _sanitize_slug(request.name or "local")[:20]
    project = f"xyn-{project_suffix}"
    deploy_dir = _deployments_root() / project
    deploy_dir.mkdir(parents=True, exist_ok=True)
    existing = _load_state(deploy_dir)
    if existing and not request.force:
        return {
            "deployment_id": existing.get("deployment_id") or "",
            "deployment_artifact_id": existing.get("deployment_artifact_id") or "",
            "status": "reused",
            "compose_project": project,
            "compose_path": existing.get("compose_path") or str(deploy_dir / "compose.yaml"),
            "ui_url": existing.get("ui_url") or "",
            "api_url": existing.get("api_url") or "",
            "surfaces": {"deployment": {"label": "Deployment", "path": existing.get("ui_url") or ""}},
            "ensured_artifacts": existing.get("ensured_artifacts") or [],
            "artifact_resolution": existing.get("artifact_resolution") or {},
        }
    deployment_id = str(uuid.uuid4())
    compose_path = deploy_dir / "compose.yaml"
    ensured_artifacts = _ensure_required_artifacts()
    try:
        artifact_resolution = _resolve_images_for_provision(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail={"message": str(exc)}) from exc

    up_cmd = [*_compose_cmd(), "-p", project, "-f", str(compose_path), "up", "-d"]
    down_cmd = [*_compose_cmd(), "-p", project, "-f", str(compose_path), "down", "--remove-orphans"]
    code = 1
    stdout = ""
    stderr = ""
    ui_port = 0
    api_port = 0
    for attempt in range(5):
        used_ports = _used_host_ports()
        ui_port = _find_free_port(used_ports=used_ports)
        used_ports.add(ui_port)
        api_port = _find_free_port(start=ui_port + 1, used_ports=used_ports)
        compose_yaml = _compose_yaml(
            project=project,
            ui_port=ui_port,
            api_port=api_port,
            ui_image=artifact_resolution["ui_image"],
            api_image=artifact_resolution["api_image"],
        )
        compose_path.write_text(compose_yaml, encoding="utf-8")
        code, stdout, stderr = _run(up_cmd, cwd=deploy_dir)
        if code == 0:
            break
        if "port is already allocated" not in (stderr or "").lower():
            break
        _run(down_cmd, cwd=deploy_dir)
        if attempt == 4:
            break
    status = "succeeded" if code == 0 else "failed"

    ui_url = f"http://localhost:{ui_port}"
    api_url = f"http://localhost:{api_port}"
    deployment_payload: Dict[str, Any] = {
        "schema_version": "xyn.deployment.v1",
        "deployment_id": deployment_id,
        "status": status,
        "compose_project": project,
        "compose_path": str(compose_path),
        "ui_url": ui_url,
        "api_url": api_url,
        "ensured_artifacts": ensured_artifacts,
        "artifact_resolution": artifact_resolution,
        "created_at": _utc_now().isoformat(),
    }
    deployment_metadata = {
        "surface": {
            "label": "Deployment",
            "path": ui_url,
        }
    }
    db = SessionLocal()
    try:
        stdout_artifact_id = _write_text_artifact(
            db,
            name=f"deployment.stdout.{project}",
            kind="log",
            content=stdout or "",
            metadata={"deployment_id": deployment_id, "stream": "stdout"},
        )
        stderr_artifact_id = _write_text_artifact(
            db,
            name=f"deployment.stderr.{project}",
            kind="log",
            content=stderr or "",
            metadata={"deployment_id": deployment_id, "stream": "stderr"},
        )
        deployment_payload["stdout_artifact_id"] = stdout_artifact_id
        deployment_payload["stderr_artifact_id"] = stderr_artifact_id

        job_result: Optional[DevChangeResult] = None
        if status == "succeeded" and request.job:
            try:
                job_result = _run_dev_change_job(
                    db,
                    prompt=request.job.prompt,
                    repo_ref=request.job.repo_ref,
                    deployment_id=deployment_id,
                )
            except Exception as exc:
                deployment_payload["job_error"] = str(exc)
                deployment_payload["job_status"] = "failed"
        deployment_artifact_id = _write_json_artifact(
            db,
            name=f"deployment.{project}",
            kind="deployment",
            payload=deployment_payload,
            metadata=deployment_metadata,
        )
    finally:
        db.close()

    if code != 0:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Local instance provisioning failed",
                "deployment_id": deployment_id,
                "compose_project": project,
                "stderr": stderr or stdout,
            },
        )

    response: Dict[str, Any] = {
        "deployment_id": deployment_id,
        "deployment_artifact_id": deployment_artifact_id,
        "status": status,
        "compose_project": project,
        "compose_path": str(compose_path),
        "ui_url": ui_url,
        "api_url": api_url,
        "ensured_artifacts": ensured_artifacts,
        "artifact_resolution": artifact_resolution,
        "surfaces": {
            "deployment": {"label": "Deployment", "path": ui_url},
        },
    }
    if request.job:
        response["job"] = {
            "status": job_result.status if job_result else deployment_payload.get("job_status", "failed"),
            "job_artifact_id": job_result.job_artifact_id if job_result else None,
            "job_run_artifact_id": job_result.job_run_artifact_id if job_result else None,
            "patch_artifact_id": job_result.patch_artifact_id if job_result else None,
            "logs_artifact_id": job_result.logs_artifact_id if job_result else None,
            "branch_name": job_result.branch_name if job_result else None,
            "commit_sha": job_result.commit_sha if job_result else None,
        }
    _save_state(
        deploy_dir,
        {
            "deployment_id": deployment_id,
            "deployment_artifact_id": deployment_artifact_id,
            "compose_project": project,
            "compose_path": str(compose_path),
            "ui_url": ui_url,
            "api_url": api_url,
            "ensured_artifacts": ensured_artifacts,
            "artifact_resolution": artifact_resolution,
            "updated_at": _utc_now().isoformat(),
        },
    )
    return response
