from __future__ import annotations

import json
import os
import re
import time
import shutil
import subprocess
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.artifact_registry import resolve_registry_images
from core.context_packs import default_instance_deployments_root, default_instance_workspace_root
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
    root = Path(os.getenv("XYN_LOCAL_DEPLOYMENTS_ROOT", default_instance_deployments_root())).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workspace_root() -> Path:
    return Path(os.getenv("XYN_LOCAL_WORKSPACE_ROOT", default_instance_workspace_root())).resolve()


def _state_path(deploy_dir: Path) -> Path:
    return deploy_dir / "deployment_state.json"


def _sanitize_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return slug or "local"


def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_remote_image_ref(value: str) -> bool:
    ref = str(value or "").strip()
    return "/" in ref


def _image_tag(value: str) -> str:
    ref = str(value or "").strip()
    if "@" in ref:
        return ""
    _, _, tag = ref.rpartition(":")
    return tag if tag and "/" not in tag else ""


def _should_refresh_remote_image(value: str, *, force: bool) -> bool:
    ref = str(value or "").strip()
    if not ref or not _is_remote_image_ref(ref):
        return False
    return force or _image_tag(ref) == DEFAULT_IMAGE_TAG


def _docker_image_exists(image_ref: str) -> bool:
    code, _, _ = _run(["docker", "image", "inspect", str(image_ref or "").strip()])
    return code == 0


def _running_container_image_ref(container_name: str) -> str:
    name = str(container_name or "").strip()
    if not name:
        return ""
    code, stdout, _ = _run(["docker", "inspect", "--format", "{{.Image}}", name])
    if code == 0 and str(stdout or "").strip():
        return str(stdout or "").strip()
    code, stdout, _ = _run(["docker", "inspect", "--format", "{{.Config.Image}}", name])
    if code == 0 and str(stdout or "").strip():
        return str(stdout or "").strip()
    return ""


def _tls_enabled() -> bool:
    if _as_bool(os.getenv("XYN_TRAEFIK_ENABLE_TLS", "false")):
        return True
    return bool(str(os.getenv("XYN_TRAEFIK_ACME_EMAIL", "")).strip())


def _resolved_hosts(project: str, *, ui_host_override: Optional[str] = None, api_host_override: Optional[str] = None) -> tuple[str, str]:
    if ui_host_override and api_host_override:
        return ui_host_override.strip(), api_host_override.strip()
    ui_override = str(os.getenv("XYN_LOCAL_UI_HOST", "")).strip()
    api_override = str(os.getenv("XYN_LOCAL_API_HOST", "")).strip()
    if ui_override and api_override:
        return ui_override, api_override
    base_domain = str(os.getenv("XYN_BASE_DOMAIN", "")).strip()
    if base_domain and base_domain not in {"localhost", "127.0.0.1"}:
        ui_host = ui_override or f"{project}.{base_domain}"
        api_host = api_override or f"api.{project}.{base_domain}"
        return ui_host, api_host
    ui_host = ui_override or "localhost"
    api_host = api_override or "localhost"
    return ui_host, api_host


def _ensure_remote_workspace_via_container(*, api_container_name: str, workspace_slug: str, workspace_title: str) -> Optional[Dict[str, Any]]:
    container = str(api_container_name or "").strip()
    slug = str(workspace_slug or "").strip().lower()
    title = str(workspace_title or workspace_slug or "Workspace").strip() or "Workspace"
    if not container or not slug:
        return None
    script = (
        "import json\n"
        "from xyn_orchestrator.models import RoleBinding, Workspace, WorkspaceMembership\n"
        "from xyn_orchestrator.xyn_api import _ensure_default_workspace_artifact_bindings, _ensure_local_identity\n"
        f"slug = {json.dumps(slug)}\n"
        f"title = {json.dumps(title)}\n"
        "workspace = Workspace.objects.filter(slug=slug).first()\n"
        "created = False\n"
        "if workspace is None:\n"
        "    workspace = Workspace.objects.create(slug=slug, name=title, org_name=title)\n"
        "    _ensure_default_workspace_artifact_bindings(workspace)\n"
        "    created = True\n"
        "identity = _ensure_local_identity('admin@local')\n"
        "WorkspaceMembership.objects.get_or_create(\n"
        "    workspace=workspace,\n"
        "    user_identity=identity,\n"
        "    defaults={'role': 'admin', 'termination_authority': True},\n"
        ")\n"
        "RoleBinding.objects.get_or_create(\n"
        "    user_identity=identity,\n"
        "    scope_kind='platform',\n"
        "    scope_id=None,\n"
        "    role='platform_admin',\n"
        ")\n"
        "print(json.dumps({'status': 'created' if created else 'existing', 'workspace_id': str(workspace.id), 'workspace_slug': slug}))\n"
    )
    code, stdout, stderr = _run(["docker", "exec", container, "python", "manage.py", "shell", "-c", script])
    if code != 0:
        raise RuntimeError(f"Failed to ensure workspace '{slug}' in provisioned instance via container: {stderr or stdout}")
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _ensure_remote_workspace(*, api_url: str, workspace_slug: str, workspace_title: str, api_container_name: str = "") -> Dict[str, Any]:
    base_url = str(api_url or "").strip().rstrip("/")
    slug = str(workspace_slug or "").strip().lower()
    title = str(workspace_title or workspace_slug or "Workspace").strip() or "Workspace"
    if not base_url or not slug:
        return {"status": "skipped", "reason": "missing_workspace_context"}
    container_result = _ensure_remote_workspace_via_container(
        api_container_name=api_container_name,
        workspace_slug=slug,
        workspace_title=title,
    )
    if container_result:
        return container_result

    cookie_header = ""
    opener = urllib.request.build_opener()

    def _request(*, method: str, path: str, data: Optional[bytes] = None, content_type: str = "") -> tuple[int, Dict[str, Any], Dict[str, str], str]:
        nonlocal cookie_header
        request = urllib.request.Request(f"{base_url}{path}", data=data, method=method.upper())
        if cookie_header:
            request.add_header("Cookie", cookie_header)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with opener.open(request, timeout=10) as response:
                status = getattr(response, "status", 200)
                body_bytes = response.read()
                headers = {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            body_bytes = exc.read()
            headers = {key.lower(): value for key, value in exc.headers.items()}
        set_cookie = headers.get("set-cookie") or ""
        if set_cookie:
            cookie_header = set_cookie.split(";", 1)[0]
        text = body_bytes.decode("utf-8", errors="replace") if body_bytes else ""
        payload: Dict[str, Any] = {}
        if text:
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    payload = decoded
            except json.JSONDecodeError:
                payload = {}
        return status, payload, headers, text

    deadline = time.time() + 90
    last_error = ""
    while time.time() < deadline:
        try:
            login_body = urllib.parse.urlencode({"appId": "xyn-ui", "returnTo": "/app"}).encode("utf-8")
            login_status, _, _, _ = _request(
                method="POST",
                path="/auth/dev-login",
                data=login_body,
                content_type="application/x-www-form-urlencoded",
            )
            if login_status not in {200, 302, 303}:
                last_error = f"dev-login failed ({login_status})"
                time.sleep(2)
                continue
            list_status, listing_payload, _, _ = _request(method="GET", path="/xyn/api/workspaces")
            if list_status != 200:
                last_error = f"workspace list failed ({list_status})"
                time.sleep(2)
                continue
            rows = listing_payload.get("workspaces") if isinstance(listing_payload, dict) else listing_payload
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("slug") or "").strip().lower() == slug:
                    return {
                        "status": "existing",
                        "workspace_id": str(row.get("id") or ""),
                        "workspace_slug": slug,
                    }
            create_status, create_payload, _, create_text = _request(
                method="POST",
                path="/xyn/api/workspaces",
                data=json.dumps({"name": title, "slug": slug, "org_name": title}).encode("utf-8"),
                content_type="application/json",
            )
            if create_status in {200, 201}:
                workspace = create_payload.get("workspace") if isinstance(create_payload, dict) else {}
                return {
                    "status": "created",
                    "workspace_id": str(workspace.get("id") or ""),
                    "workspace_slug": slug,
                }
            if create_status == 400 and "already exists" in str(create_text or "").lower():
                continue
            last_error = f"workspace create failed ({create_status})"
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = exc.__class__.__name__
        time.sleep(2)
    raise RuntimeError(f"Failed to ensure workspace '{slug}' in provisioned instance: {last_error or 'timeout'}")


def _compose_yaml(project: str, *, ui_image: str, api_image: str, ui_host: str, api_host: str) -> str:
    traefik_network = str(os.getenv("XYN_TRAEFIK_NETWORK", "xyn_traefik")).strip() or "xyn_traefik"
    resolver = str(os.getenv("XYN_TRAEFIK_CERT_RESOLVER", "letsencrypt")).strip() or "letsencrypt"
    tls = _tls_enabled()
    ui_scheme = "https" if tls else "http"
    if ui_host == api_host:
        api_rule = f"Host(`{ui_host}`) && (PathPrefix(`/xyn/api`) || PathPrefix(`/api`) || PathPrefix(`/auth`))"
    else:
        api_rule = (
            f"Host(`{api_host}`) || "
            f"(Host(`{ui_host}`) && (PathPrefix(`/xyn/api`) || PathPrefix(`/api`) || PathPrefix(`/auth`)))"
        )
    ui_rule = f"Host(`{ui_host}`)"
    return f"""services:
  postgres:
    image: postgres:16-alpine
    container_name: {project}-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: xyn
      POSTGRES_USER: xyn
      POSTGRES_PASSWORD: xyn_dev_password
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U xyn -d xyn"]
      interval: 5s
      timeout: 5s
      retries: 20

  redis:
    image: redis:7-alpine
    container_name: {project}-redis
    restart: unless-stopped

  backend:
    image: {api_image}
    container_name: {project}-api
    restart: unless-stopped
    networks:
      - default
      - traefik
    environment:
      DATABASE_URL: postgresql://xyn:xyn_dev_password@postgres:5432/xyn
      REDIS_URL: redis://redis:6379/0
      POSTGRES_DB: xyn
      POSTGRES_USER: xyn
      POSTGRES_PASSWORD: xyn_dev_password
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      XYN_ENV: local
      XYN_AUTH_MODE: ${{XYN_AUTH_MODE:-dev}}
      XYN_INTENT_ENGINE_V1: ${{XYN_INTENT_ENGINE_V1:-1}}
      XYN_PUBLIC_BASE_URL: ${{XYN_PUBLIC_BASE_URL:-{ui_scheme}://{ui_host}}}
      XYN_TRUST_PROXY: ${{XYN_TRUST_PROXY:-true}}
      XYN_TRUSTED_PROXY_CIDRS: ${{XYN_TRUSTED_PROXY_CIDRS:-127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}}
      XYN_DEBUG_AUTH: ${{XYN_DEBUG_AUTH:-false}}
      XYN_OIDC_ISSUER: ${{XYN_OIDC_ISSUER:-}}
      XYN_OIDC_CLIENT_ID: ${{XYN_OIDC_CLIENT_ID:-}}
      OIDC_ISSUER: ${{XYN_OIDC_ISSUER:-}}
      OIDC_CLIENT_ID: ${{XYN_OIDC_CLIENT_ID:-}}
      XYN_UI_BEARER_TOKEN: ${{XYN_UI_BEARER_TOKEN:-}}
      XYN_AI_PROVIDER: ${{XYN_AI_PROVIDER:-}}
      XYN_AI_MODEL: ${{XYN_AI_MODEL:-}}
      XYN_AI_PLANNING_PROVIDER: ${{XYN_AI_PLANNING_PROVIDER:-}}
      XYN_AI_PLANNING_MODEL: ${{XYN_AI_PLANNING_MODEL:-}}
      XYN_AI_PLANNING_API_KEY: ${{XYN_AI_PLANNING_API_KEY:-}}
      XYN_AI_CODING_PROVIDER: ${{XYN_AI_CODING_PROVIDER:-}}
      XYN_AI_CODING_MODEL: ${{XYN_AI_CODING_MODEL:-}}
      XYN_AI_CODING_API_KEY: ${{XYN_AI_CODING_API_KEY:-}}
      XYN_OPENAI_API_KEY: ${{XYN_OPENAI_API_KEY:-}}
      XYN_GEMINI_API_KEY: ${{XYN_GEMINI_API_KEY:-}}
      XYN_ANTHROPIC_API_KEY: ${{XYN_ANTHROPIC_API_KEY:-}}
      XYN_CREDENTIALS_ENCRYPTION_KEY: ${{XYN_CREDENTIALS_ENCRYPTION_KEY:-}}
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network={traefik_network}"
      - "traefik.http.routers.{project}-api-http.rule={api_rule}"
      - "traefik.http.routers.{project}-api-http.entrypoints=web"
      - "traefik.http.routers.{project}-api-http.priority=200"
      - "traefik.http.routers.{project}-api-http.service={project}-api-svc"
      - "traefik.http.services.{project}-api-svc.loadbalancer.server.port=8000"
      - "traefik.http.routers.{project}-api-https.rule={api_rule}"
      - "traefik.http.routers.{project}-api-https.entrypoints=websecure"
      - "traefik.http.routers.{project}-api-https.priority=200"
      - "traefik.http.routers.{project}-api-https.service={project}-api-svc"
      - "traefik.http.routers.{project}-api-https.tls={str(tls).lower()}"
      - "traefik.http.routers.{project}-api-https.tls.certresolver={resolver}"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started
      migrate:
        condition: service_completed_successfully

  migrate:
    image: {api_image}
    container_name: {project}-migrate
    restart: "no"
    networks:
      - default
    environment:
      DATABASE_URL: postgresql://xyn:xyn_dev_password@postgres:5432/xyn
      REDIS_URL: redis://redis:6379/0
      POSTGRES_DB: xyn
      POSTGRES_USER: xyn
      POSTGRES_PASSWORD: xyn_dev_password
      POSTGRES_HOST: postgres
      POSTGRES_PORT: 5432
      XYN_ENV: local
      XYN_AUTH_MODE: ${{XYN_AUTH_MODE:-dev}}
      XYN_INTENT_ENGINE_V1: ${{XYN_INTENT_ENGINE_V1:-1}}
      XYN_PUBLIC_BASE_URL: ${{XYN_PUBLIC_BASE_URL:-{ui_scheme}://{ui_host}}}
      XYN_TRUST_PROXY: ${{XYN_TRUST_PROXY:-true}}
      XYN_TRUSTED_PROXY_CIDRS: ${{XYN_TRUSTED_PROXY_CIDRS:-127.0.0.1/32,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}}
      XYN_DEBUG_AUTH: ${{XYN_DEBUG_AUTH:-false}}
      XYN_OIDC_ISSUER: ${{XYN_OIDC_ISSUER:-}}
      XYN_OIDC_CLIENT_ID: ${{XYN_OIDC_CLIENT_ID:-}}
      OIDC_ISSUER: ${{XYN_OIDC_ISSUER:-}}
      OIDC_CLIENT_ID: ${{XYN_OIDC_CLIENT_ID:-}}
      XYN_UI_BEARER_TOKEN: ${{XYN_UI_BEARER_TOKEN:-}}
      XYN_AI_PROVIDER: ${{XYN_AI_PROVIDER:-}}
      XYN_AI_MODEL: ${{XYN_AI_MODEL:-}}
      XYN_AI_PLANNING_PROVIDER: ${{XYN_AI_PLANNING_PROVIDER:-}}
      XYN_AI_PLANNING_MODEL: ${{XYN_AI_PLANNING_MODEL:-}}
      XYN_AI_PLANNING_API_KEY: ${{XYN_AI_PLANNING_API_KEY:-}}
      XYN_AI_CODING_PROVIDER: ${{XYN_AI_CODING_PROVIDER:-}}
      XYN_AI_CODING_MODEL: ${{XYN_AI_CODING_MODEL:-}}
      XYN_AI_CODING_API_KEY: ${{XYN_AI_CODING_API_KEY:-}}
      XYN_OPENAI_API_KEY: ${{XYN_OPENAI_API_KEY:-}}
      XYN_GEMINI_API_KEY: ${{XYN_GEMINI_API_KEY:-}}
      XYN_ANTHROPIC_API_KEY: ${{XYN_ANTHROPIC_API_KEY:-}}
      XYN_CREDENTIALS_ENCRYPTION_KEY: ${{XYN_CREDENTIALS_ENCRYPTION_KEY:-}}
    command:
      - /bin/sh
      - -lc
      - |
        set -e
        python - <<'PY'
        from pathlib import Path

        p = Path("/app/xyn_orchestrator/migrations/0068_providercredential_agentdefinition_and_more.py")
        if p.exists():
            marker = "atomic = False"
            target = "class Migration(migrations.Migration):\\n\\n    dependencies = ["
            source = p.read_text()
            if marker not in source and target in source:
                p.write_text(source.replace(target, "class Migration(migrations.Migration):\\n    atomic = False\\n\\n    dependencies = ["))
        PY
        exec python manage.py migrate --noinput
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_started

  ui:
    image: {ui_image}
    container_name: {project}-ui
    restart: unless-stopped
    networks:
      - default
      - traefik
    environment:
      VITE_API_BASE_URL: {ui_scheme}://{ui_host}/xyn/api
    labels:
      - "traefik.enable=true"
      - "traefik.docker.network={traefik_network}"
      - "traefik.http.routers.{project}-ui-http.rule={ui_rule}"
      - "traefik.http.routers.{project}-ui-http.entrypoints=web"
      - "traefik.http.routers.{project}-ui-http.priority=10"
      - "traefik.http.routers.{project}-ui-http.service={project}-ui-svc"
      - "traefik.http.services.{project}-ui-svc.loadbalancer.server.port=80"
      - "traefik.http.routers.{project}-ui-https.rule={ui_rule}"
      - "traefik.http.routers.{project}-ui-https.entrypoints=websecure"
      - "traefik.http.routers.{project}-ui-https.priority=10"
      - "traefik.http.routers.{project}-ui-https.service={project}-ui-svc"
      - "traefik.http.routers.{project}-ui-https.tls={str(tls).lower()}"
      - "traefik.http.routers.{project}-ui-https.tls.certresolver={resolver}"
    depends_on:
      - backend

volumes:
  postgres_data:

networks:
  traefik:
    external: true
    name: {traefik_network}
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
        storage_scope="instance-local",
        sync_state="local",
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
        storage_scope="instance-local",
        sync_state="local",
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
    ui_host: Optional[str] = None
    api_host: Optional[str] = None
    prefer_local_images: bool = False


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


def _candidate_contexts(explicit: str, candidates: list[Path]) -> list[str]:
    values: list[str] = []
    if explicit:
        values.append(str(explicit).strip())
    values.extend(str(candidate) for candidate in candidates)
    unique: list[str] = []
    for value in values:
        token = str(value or "").strip()
        if token and token not in unique:
            unique.append(token)
    return unique


def _build_local_image(tag: str, contexts: list[str]) -> tuple[str, list[str]]:
    attempts: list[str] = []
    for context in contexts:
        code, _, stderr = _run(["docker", "build", "-t", tag, context])
        if code == 0:
            attempts.append(f"Built local image {tag} from {context}")
            return context, attempts
        attempts.append(f"Failed to build local image {tag} from {context}: {stderr or 'unknown error'}")
    return "", attempts


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

    prefer_local_images = bool(request.prefer_local_images) or _as_bool(os.getenv("XYN_PROVISION_PREFER_LOCAL_IMAGES", "false"))
    if prefer_local_images:
        local_api_image_ref = _running_container_image_ref(str(os.getenv("XYN_PLATFORM_API_CONTAINER", "xyn-local-api")).strip() or "xyn-local-api")
        local_ui_image_ref = _running_container_image_ref(str(os.getenv("XYN_PLATFORM_UI_CONTAINER", "xyn-local-ui")).strip() or "xyn-local-ui")
        if local_api_image_ref and local_ui_image_ref and _docker_image_exists(local_api_image_ref) and _docker_image_exists(local_ui_image_ref):
            operations.append(f"Using running local API image {local_api_image_ref}")
            operations.append(f"Using running local UI image {local_ui_image_ref}")
            return {
                "mode": "running_local_images",
                "registry": defaults["registry"],
                "ui_image": local_ui_image_ref,
                "api_image": local_api_image_ref,
                "registry_slug": None,
                "registry_source": "running_local_images",
                "channel": str(request.channel or DEFAULT_IMAGE_TAG).strip() or DEFAULT_IMAGE_TAG,
                "operations": operations,
            }
        src_root = str(os.getenv("XYN_HOST_SRC_ROOT", "/home/ubuntu/src")).strip() or "/home/ubuntu/src"
        src_root_path = Path(src_root).expanduser().resolve()
        local_ui_contexts = _candidate_contexts(
            str(os.getenv("XYN_LOCAL_UI_CONTEXT", "")).strip(),
            [
                src_root_path / "xyn-platform" / "apps" / "xyn-ui",
                src_root_path / "xyn-ui",
            ],
        )
        local_api_contexts = _candidate_contexts(
            str(os.getenv("XYN_LOCAL_API_CONTEXT", "")).strip(),
            [
                src_root_path / "xyn-platform" / "services" / "xyn-api",
                src_root_path / "xyn-api",
            ],
        )
        built_api_context, api_attempts = _build_local_image(DEFAULT_API_IMAGE_NAME, local_api_contexts)
        built_ui_context, ui_attempts = _build_local_image(DEFAULT_UI_IMAGE_NAME, local_ui_contexts)
        if built_api_context and built_ui_context:
            operations.extend(api_attempts[-1:])
            operations.extend(ui_attempts[-1:])
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
        if _docker_image_exists(DEFAULT_UI_IMAGE_NAME) and _docker_image_exists(DEFAULT_API_IMAGE_NAME):
            operations.extend(api_attempts)
            operations.extend(ui_attempts)
            operations.append(f"Using prebuilt local image {DEFAULT_API_IMAGE_NAME}")
            operations.append(f"Using prebuilt local image {DEFAULT_UI_IMAGE_NAME}")
            return {
                "mode": "prebuilt_local_images",
                "registry": defaults["registry"],
                "ui_image": DEFAULT_UI_IMAGE_NAME,
                "api_image": DEFAULT_API_IMAGE_NAME,
                "registry_slug": None,
                "registry_source": "prebuilt_local_images",
                "channel": str(request.channel or DEFAULT_IMAGE_TAG).strip() or DEFAULT_IMAGE_TAG,
                "operations": operations,
            }
        operations.append("Local image preference enabled, but no local xyn-api/xyn-ui build sources were available. Falling back to artifact registry.")
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

    remote_refresh_services: list[str] = []
    if _should_refresh_remote_image(artifact_resolution["api_image"], force=request.force):
        remote_refresh_services.extend(["backend", "migrate"])
    if _should_refresh_remote_image(artifact_resolution["ui_image"], force=request.force):
        remote_refresh_services.append("ui")
    up_cmd = [*_compose_cmd(), "-p", project, "-f", str(compose_path), "up", "-d"]
    down_cmd = [*_compose_cmd(), "-p", project, "-f", str(compose_path), "down", "--remove-orphans", "--volumes"]
    pull_cmd = [*_compose_cmd(), "-p", project, "-f", str(compose_path), "pull", *remote_refresh_services] if remote_refresh_services else []
    ui_host, api_host = _resolved_hosts(
        project,
        ui_host_override=request.ui_host,
        api_host_override=request.api_host,
    )
    tls = _tls_enabled()
    scheme = "https" if tls else "http"
    compose_yaml = _compose_yaml(
        project=project,
        ui_image=artifact_resolution["ui_image"],
        api_image=artifact_resolution["api_image"],
        ui_host=ui_host,
        api_host=api_host,
    )
    compose_path.write_text(compose_yaml, encoding="utf-8")
    if request.force:
        _run(down_cmd, cwd=deploy_dir)
    pull_stdout = ""
    pull_stderr = ""
    if pull_cmd:
        pull_code, pull_stdout, pull_stderr = _run(pull_cmd, cwd=deploy_dir)
        if pull_code != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Failed to pull latest artifact images for local instance",
                    "deployment_id": deployment_id,
                    "compose_project": project,
                    "stderr": pull_stderr or pull_stdout,
                },
            )
        up_cmd.append("--force-recreate")
    code, stdout, stderr = _run(up_cmd, cwd=deploy_dir)
    status = "succeeded" if code == 0 else "failed"

    ui_url = f"{scheme}://{ui_host}"
    api_url = f"{scheme}://{api_host}"
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
    if pull_cmd:
        deployment_payload["pull_stdout"] = pull_stdout
        deployment_payload["pull_stderr"] = pull_stderr
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
    workspace_slug = str(request.workspace_slug or "").strip().lower()
    if workspace_slug:
        response["workspace"] = _ensure_remote_workspace(
            api_url=api_url,
            workspace_slug=workspace_slug,
            workspace_title=workspace_slug.replace("-", " ").title(),
            api_container_name=f"{project}-api",
        )
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
