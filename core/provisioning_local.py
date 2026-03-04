from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.models import Artifact

router = APIRouter(prefix="/api/v1/provision", tags=["provision"])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _deployments_root() -> Path:
    root = Path(os.getenv("XYN_LOCAL_DEPLOYMENTS_ROOT", ".xyn/deployments")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workspace_root() -> Path:
    return Path(os.getenv("XYN_LOCAL_WORKSPACE_ROOT", ".xyn/workspace")).resolve()


def _sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return slug or "local"


def _find_free_port(start: int = 42000, end: int = 42999) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("No free port available in allocation range")


def _compose_yaml(project: str, ui_port: int, api_port: int) -> str:
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
    image: xyn-api-backend
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
    image: xyn-ui
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
    job: Optional[LocalDevChangeInput] = None


@router.post("/local-instance")
def provision_local_instance(request: ProvisionLocalRequest) -> Dict[str, Any]:
    deployment_id = str(uuid.uuid4())
    project_suffix = _sanitize_slug(request.name or deployment_id[:8])[:20]
    project = f"xyn-{project_suffix}"
    ui_port = _find_free_port()
    api_port = _find_free_port(start=ui_port + 1)
    deploy_dir = _deployments_root() / project
    deploy_dir.mkdir(parents=True, exist_ok=True)
    compose_path = deploy_dir / "compose.yaml"
    compose_yaml = _compose_yaml(project=project, ui_port=ui_port, api_port=api_port)
    compose_path.write_text(compose_yaml, encoding="utf-8")

    cmd = [*_compose_cmd(), "-p", project, "-f", str(compose_path), "up", "-d", "--build"]
    code, stdout, stderr = _run(cmd, cwd=deploy_dir)
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
    return response
