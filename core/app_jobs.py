"""Phase-2 app-intent pipeline worker.

Executes queued jobs:
- generate_app_spec
- deploy_app_local
- provision_sibling_xyn
- smoke_test
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
import uuid
import base64
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from jsonschema import ValidationError, validate
from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.context_packs import default_instance_workspace_root
from core.capability_manifest import build_manifest_suggestions, build_resolved_capability_manifest
from core.execution_notes import create_execution_note, update_execution_note
from core.models import Artifact, Job, JobStatus, Workspace
from core.palette_engine import execute_palette_prompt
from core.primitives import get_primitive_catalog
from core.provisioning_local import ProvisionLocalRequest, provision_local_instance

POLL_SECONDS = float(os.getenv("XYN_APP_JOB_POLL_SECONDS", "2.0"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_JOB_HTTP_TIMEOUT", "10"))
APP_DEPLOY_HEALTH_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_DEPLOY_HEALTH_TIMEOUT_SECONDS", "180"))
COMMAND_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_JOB_COMMAND_TIMEOUT_SECONDS", "240"))
APPSPEC_SCHEMA_PATH = Path(__file__).resolve().parent / "contracts" / "appspec_v0.schema.json"
NET_INVENTORY_IMAGE = str(
    os.getenv("XYN_NET_INVENTORY_IMAGE", "public.ecr.aws/i0h0h0n4/xyn/artifacts/net-inventory-api:dev")
).strip()
GENERATED_ARTIFACT_VERSION = "0.0.1-dev"
ROOT_PLATFORM_API_CONTAINER = str(os.getenv("XYN_PLATFORM_API_CONTAINER", "xyn-local-api")).strip() or "xyn-local-api"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _safe_slug(value: str, *, default: str = "app") -> str:
    raw = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in str(value or "").lower())
    collapsed = "-".join(part for part in raw.split("-") if part)
    return collapsed or default


def _workspace_root() -> Path:
    root = Path(os.getenv("XYN_LOCAL_WORKSPACE_ROOT", os.getenv("XYNSEED_WORKSPACE", default_instance_workspace_root()))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _deployments_root() -> Path:
    root = _workspace_root() / "app_deployments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _generated_artifacts_root() -> Path:
    root = _workspace_root() / "artifacts" / "generated"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _docker_image_exists(image_ref: str) -> bool:
    code, _, _ = _run(["docker", "image", "inspect", str(image_ref or "").strip()])
    return code == 0


def _effective_net_inventory_image() -> str:
    explicit = str(os.getenv("XYN_NET_INVENTORY_IMAGE", "") or "").strip()
    if explicit:
        return explicit
    if _docker_image_exists("net-inventory-api:local"):
        return "net-inventory-api:local"
    return NET_INVENTORY_IMAGE


def _generated_artifact_slug(app_slug: str) -> str:
    return f"app.{_safe_slug(app_slug, default='generated-app')}"


def _run(cmd: list[str], *, cwd: Optional[Path] = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
        return 124, stdout, stderr or f"command timed out after {COMMAND_TIMEOUT_SECONDS}s"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _container_http_json(
    container_name: str,
    method: str,
    path: str,
    *,
    port: int,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any], str]:
    script = f"""
import json
import urllib.error
import urllib.request

method = {method!r}
path = {path!r}
payload = {payload or {}!r}
url = "http://localhost:{port}" + path
data = None
headers = {{"Content-Type": "application/json"}}
if method in ("POST", "PUT", "PATCH"):
    data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(url, method=method, headers=headers, data=data)
try:
    with urllib.request.urlopen(req, timeout={HTTP_TIMEOUT_SECONDS}) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        print(json.dumps({{"code": int(resp.status), "body": body}}))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="ignore")
    print(json.dumps({{"code": int(exc.code), "body": body}}))
except Exception as exc:
    print(json.dumps({{"code": 0, "body": str(exc)}}))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", container_name, "python", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=HTTP_TIMEOUT_SECONDS + 5,
    )
    if proc.returncode != 0:
        return 0, {}, proc.stderr.strip() or proc.stdout.strip()
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return 0, {}, "empty container response"
    line = out[-1].strip()
    try:
        payload_json = json.loads(line)
    except json.JSONDecodeError:
        return 0, {}, line
    code = int(payload_json.get("code") or 0)
    raw_body = str(payload_json.get("body") or "")
    try:
        body_json = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body_json = {}
    return code, body_json, raw_body


def _container_http_session_json(
    container_name: str,
    *,
    steps: list[dict[str, Any]],
    port: int,
) -> tuple[int, dict[str, Any], str]:
    script = f"""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

steps = {steps!r}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    NoRedirect(),
)
last = {{"code": 0, "body": "", "json": {{}}}}

for step in steps:
    method = str(step.get("method") or "GET").upper()
    path = str(step.get("path") or "/")
    body = step.get("body")
    form = step.get("form")
    headers = dict(step.get("headers") or {{}})
    data = None
    if form is not None:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        data = urllib.parse.urlencode(form).encode("utf-8")
    elif body is not None:
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://localhost:{port}" + path, method=method, headers=headers, data=data)
    try:
        with opener.open(req, timeout={HTTP_TIMEOUT_SECONDS}) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                payload = json.loads(raw) if raw else {{}}
            except json.JSONDecodeError:
                payload = {{}}
            last = {{"code": int(resp.status), "body": raw, "json": payload}}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw) if raw else {{}}
        except json.JSONDecodeError:
                payload = {{}}
        last = {{"code": int(exc.code), "body": raw, "json": payload}}
        if int(exc.code) not in {{301, 302, 303, 307, 308}}:
            break
    except Exception as exc:
        last = {{"code": 0, "body": str(exc), "json": {{}}}}
        break

print(json.dumps(last))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", container_name, "python", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=HTTP_TIMEOUT_SECONDS + 10,
    )
    if proc.returncode != 0:
        return 0, {}, proc.stderr.strip() or proc.stdout.strip()
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return 0, {}, "empty container response"
    line = out[-1].strip()
    try:
        payload_json = json.loads(line)
    except json.JSONDecodeError:
        return 0, {}, line
    code = int(payload_json.get("code") or 0)
    raw_body = str(payload_json.get("body") or "")
    body_json = payload_json.get("json") if isinstance(payload_json.get("json"), dict) else {}
    return code, body_json, raw_body


def _container_http_session_upload_json(
    container_name: str,
    *,
    port: int,
    upload_path: str,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    extra_form: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any], str]:
    blob_b64 = base64.b64encode(file_bytes).decode("ascii")
    script = f"""
import base64
import json
import requests

session = requests.Session()
login = session.post(
    "http://localhost:{port}/auth/dev-login",
    data={{"appId": "xyn-ui", "returnTo": "/app"}},
    allow_redirects=False,
    timeout={HTTP_TIMEOUT_SECONDS},
)
if login.status_code not in (200, 302, 303):
    print(json.dumps({{"code": int(login.status_code), "body": login.text}}))
    raise SystemExit(0)
session.get("http://localhost:{port}/xyn/api/me", timeout={HTTP_TIMEOUT_SECONDS})
blob = base64.b64decode({blob_b64!r})
resp = session.post(
    "http://localhost:{port}" + {upload_path!r},
    data={extra_form or {}!r},
    files={{{file_field!r}: ({filename!r}, blob, "application/zip")}},
    timeout={HTTP_TIMEOUT_SECONDS},
)
print(json.dumps({{"code": int(resp.status_code), "body": resp.text}}))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", container_name, "python", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=HTTP_TIMEOUT_SECONDS + 15,
    )
    if proc.returncode != 0:
        return 0, {}, proc.stderr.strip() or proc.stdout.strip()
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return 0, {}, "empty container response"
    line = out[-1].strip()
    try:
        payload_json = json.loads(line)
    except json.JSONDecodeError:
        return 0, {}, line
    code = int(payload_json.get("code") or 0)
    raw_body = str(payload_json.get("body") or "")
    try:
        body_json = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body_json = {}
    return code, body_json, raw_body


def _build_generated_artifact_manifest(*, app_spec: dict[str, Any], runtime_config: dict[str, Any]) -> dict[str, Any]:
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "generated-app"), default="generated-app")
    artifact_slug = _generated_artifact_slug(app_slug)
    title = str(app_spec.get("title") or app_slug).strip() or app_slug
    capability_manifest = build_resolved_capability_manifest(app_spec)
    suggestions = build_manifest_suggestions(artifact_slug=artifact_slug, manifest=capability_manifest)
    return {
        "artifact": {
            "id": artifact_slug,
            "type": "application",
            "slug": artifact_slug,
            "version": GENERATED_ARTIFACT_VERSION,
            "name": title,
            "generated": True,
        },
        "capability": {
            "visibility": "capabilities",
            "category": "application",
            "label": title,
            "description": "Generated application capability installed through the artifact registry.",
            "tags": ["generated", "application", app_slug],
            "order": 120,
        },
        "resolved_capability_manifest": capability_manifest,
        "suggestions": suggestions,
        "surfaces": {
            "manage": [{"label": "Workbench", "path": "/app/workbench", "order": 100}],
            "docs": [{"label": "Workbench", "path": "/app/workbench", "order": 1000}],
        },
        "content": {
            "app_spec": app_spec,
            "runtime_config": runtime_config,
            "resolved_capability_manifest": capability_manifest,
        },
    }


def _package_generated_app(
    *,
    workspace_id: uuid.UUID,
    source_job_id: str,
    app_spec: dict[str, Any],
    runtime_config: dict[str, Any],
) -> dict[str, Any]:
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "generated-app"), default="generated-app")
    artifact_slug = _generated_artifact_slug(app_slug)
    package_root = _generated_artifacts_root() / app_slug
    payload_root = package_root / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    artifact_manifest = _build_generated_artifact_manifest(app_spec=app_spec, runtime_config=runtime_config)
    artifact_manifest_path = package_root / "artifact.json"
    app_spec_path = payload_root / "app_spec.json"
    runtime_config_path = payload_root / "runtime_config.json"
    artifact_manifest_path.write_text(json.dumps(artifact_manifest, indent=2, sort_keys=True), encoding="utf-8")
    app_spec_path.write_text(json.dumps(app_spec, indent=2, sort_keys=True), encoding="utf-8")
    runtime_config_path.write_text(json.dumps(runtime_config, indent=2, sort_keys=True), encoding="utf-8")

    artifact_entry = {
        "type": "application",
        "slug": artifact_slug,
        "version": GENERATED_ARTIFACT_VERSION,
        "artifact_id": artifact_slug,
        "title": str(app_spec.get("title") or app_slug),
        "description": "Generated application artifact package",
        "dependencies": [],
        "bindings": [],
    }
    files: dict[str, bytes] = {}
    base = f"artifacts/application/{artifact_slug}/{GENERATED_ARTIFACT_VERSION}"
    artifact_zip_path = f"{base}/artifact.json"
    payload_zip_path = f"{base}/payload/payload.json"
    surfaces_zip_path = f"{base}/surfaces.json"
    runtime_roles_zip_path = f"{base}/runtime_roles.json"
    combined_payload = {
        "app_spec": app_spec,
        "runtime_config": runtime_config,
        "generated": True,
        "source_job_id": source_job_id,
        "source_workspace_id": str(workspace_id),
    }
    files[artifact_zip_path] = json.dumps(artifact_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[payload_zip_path] = json.dumps(combined_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[surfaces_zip_path] = b"[]"
    files[runtime_roles_zip_path] = b"[]"
    manifest = {
        "format_version": 1,
        "package_name": artifact_slug,
        "package_version": GENERATED_ARTIFACT_VERSION,
        "built_at": _iso_now(),
        "platform_compatibility": {"min_version": "1.0.0", "required_features": ["artifact_packages_v1"]},
        "artifacts": [
            {
                **artifact_entry,
                "artifact_hash": hashlib.sha256(files[artifact_zip_path]).hexdigest(),
            }
        ],
        "checksums": {path: hashlib.sha256(content).hexdigest() for path, content in files.items()},
    }
    files["manifest.json"] = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    package_zip_path = package_root / "package.zip"
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files.keys()):
            archive.writestr(path, files[path])
    package_zip_path.write_bytes(blob.getvalue())
    return {
        "artifact_slug": artifact_slug,
        "artifact_version": GENERATED_ARTIFACT_VERSION,
        "artifact_manifest_path": str(artifact_manifest_path),
        "artifact_package_path": str(package_zip_path),
        "artifact_dir": str(package_root),
        "runtime_config_path": str(runtime_config_path),
        "app_spec_path": str(app_spec_path),
        "package_size_bytes": package_zip_path.stat().st_size,
    }


def _import_generated_artifact_package_into_registry(
    *,
    container_name: str,
    artifact_slug: str,
    package_path: Path,
    port: int = 8000,
) -> dict[str, Any]:
    if not package_path.exists():
        raise RuntimeError(f"Generated artifact package not found: {package_path}")
    if not artifact_slug.startswith("app."):
        raise RuntimeError(f"Generated artifact slug must use app.* namespace: {artifact_slug}")
    if not _docker_container_running(container_name):
        raise RuntimeError(f"Platform API container is not running: {container_name}")
    code, body, text = _container_http_session_upload_json(
        container_name,
        port=port,
        upload_path="/xyn/api/artifacts/import",
        file_field="file",
        filename=package_path.name,
        file_bytes=package_path.read_bytes(),
    )
    if code not in {200, 201}:
        raise RuntimeError(f"Generated artifact import failed ({code}): {text}")
    artifacts = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
    imported = next((item for item in artifacts if isinstance(item, dict) and str(item.get("slug") or "") == artifact_slug), None)
    if not isinstance(imported, dict):
        raise RuntimeError(f"Generated artifact import response missing slug {artifact_slug}")
    return {
        "status": "imported",
        "package": body.get("package") if isinstance(body.get("package"), dict) else {},
        "receipt": body.get("receipt") if isinstance(body.get("receipt"), dict) else {},
        "artifact": imported,
    }


def _import_generated_artifact_package(
    *,
    artifact_slug: str,
    package_path: Path,
) -> dict[str, Any]:
    return _import_generated_artifact_package_into_registry(
        container_name=ROOT_PLATFORM_API_CONTAINER,
        artifact_slug=artifact_slug,
        package_path=package_path,
        port=8000,
    )


def _install_generated_artifact_in_sibling(
    *,
    sibling_api_container: str,
    workspace_slug: str,
    artifact_slug: str,
    artifact_version: str = "",
) -> dict[str, Any]:
    code, body, text = _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "GET",
                "path": "/xyn/api/me",
            },
            {
                "method": "GET",
                "path": "/xyn/api/workspaces",
            },
        ],
    )
    if code != 200:
        raise RuntimeError(f"Failed to enumerate sibling workspaces ({code}): {text}")
    rows = body.get("workspaces") if isinstance(body.get("workspaces"), list) else []
    workspace = next((row for row in rows if str(row.get("slug") or "").strip() == workspace_slug), None)
    if not isinstance(workspace, dict):
        raise RuntimeError(f"Sibling workspace with slug '{workspace_slug}' not found")
    workspace_id = str(workspace.get("id") or "").strip()
    if not workspace_id:
        raise RuntimeError("Sibling workspace id missing from workspace list response")

    install_code, install_body, install_text = _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "POST",
                "path": f"/xyn/api/workspaces/{workspace_id}/artifacts",
                "body": {
                    "artifact_id": artifact_slug,
                    "artifact_version": artifact_version,
                    "enabled": True,
                },
            },
        ],
    )
    if install_code not in {200, 201}:
        raise RuntimeError(f"Failed to install sibling artifact '{artifact_slug}' ({install_code}): {install_text}")
    artifact = install_body.get("artifact") if isinstance(install_body.get("artifact"), dict) else {}
    return {
        "workspace_id": workspace_id,
        "workspace_slug": workspace_slug,
        "artifact_slug": str(artifact.get("slug") or artifact_slug),
        "artifact_id": str(artifact.get("artifact_id") or ""),
        "binding_id": str(artifact.get("binding_id") or ""),
    }


def _register_sibling_runtime_target(
    *,
    sibling_api_container: str,
    workspace_id: str,
    app_slug: str,
    artifact_slug: str,
    title: str,
    runtime_target: dict[str, Any],
) -> dict[str, Any]:
    register_code, register_body, register_text = _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "POST",
                "path": f"/xyn/api/workspaces/{workspace_id}/app-runtime-targets",
                "body": {
                    "app_slug": app_slug,
                    "artifact_slug": artifact_slug,
                    "title": title,
                    "runtime_target": runtime_target,
                },
            },
        ],
    )
    if register_code not in {200, 201}:
        raise RuntimeError(f"Failed to register sibling runtime target ({register_code}): {register_text}")
    return register_body if isinstance(register_body, dict) else {}


def _find_revision_sibling_target(
    db: Session,
    *,
    root_workspace_id: uuid.UUID,
    revision_anchor: dict[str, Any],
    app_slug: str,
) -> Optional[dict[str, Any]]:
    anchor_workspace_id = str(revision_anchor.get("workspace_id") or "").strip()
    anchor_instance_id = str(revision_anchor.get("workspace_app_instance_id") or "").strip()
    anchor_artifact_slug = str(revision_anchor.get("artifact_slug") or "").strip()
    if not anchor_workspace_id or not anchor_artifact_slug:
        return None

    candidates = (
        db.query(Job)
        .filter(
            Job.workspace_id == root_workspace_id,
            Job.type == "provision_sibling_xyn",
            Job.status == JobStatus.SUCCEEDED.value,
        )
        .order_by(Job.updated_at.desc())
        .all()
    )
    for candidate in candidates:
        output = candidate.output_json if isinstance(candidate.output_json, dict) else {}
        installed_artifact = output.get("installed_artifact") if isinstance(output.get("installed_artifact"), dict) else {}
        runtime_registration = output.get("runtime_registration") if isinstance(output.get("runtime_registration"), dict) else {}
        runtime_instance = runtime_registration.get("instance") if isinstance(runtime_registration.get("instance"), dict) else {}
        runtime_target = output.get("runtime_target") if isinstance(output.get("runtime_target"), dict) else {}
        sibling_compose_project = str(output.get("compose_project") or "").strip()
        sibling_ui_url = str(output.get("ui_url") or "").strip()
        sibling_api_url = str(output.get("api_url") or "").strip()
        installed_workspace_id = str(installed_artifact.get("workspace_id") or "").strip()
        installed_artifact_slug = str(installed_artifact.get("artifact_slug") or "").strip()
        runtime_app_slug = str(runtime_target.get("app_slug") or "").strip()
        runtime_instance_id = str(runtime_instance.get("id") or "").strip()
        if installed_workspace_id != anchor_workspace_id:
            continue
        if installed_artifact_slug != anchor_artifact_slug:
            continue
        if runtime_app_slug and runtime_app_slug != app_slug:
            continue
        if anchor_instance_id and runtime_instance_id and runtime_instance_id != anchor_instance_id:
            continue
        if not sibling_compose_project or not sibling_ui_url or not sibling_api_url:
            continue
        return {
            "deployment_id": str(output.get("deployment_id") or ""),
            "compose_project": sibling_compose_project,
            "ui_url": sibling_ui_url,
            "api_url": sibling_api_url,
            "installed_artifact": installed_artifact,
            "runtime_target": runtime_target,
            "runtime_registration": runtime_registration,
        }
    return None


def _execute_sibling_palette_prompt(
    *,
    sibling_api_container: str,
    workspace_slug: str,
    prompt: str,
) -> tuple[int, dict[str, Any], str]:
    return _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "POST",
                "path": f"/xyn/api/palette/execute?workspace_slug={workspace_slug}",
                "body": {"prompt": prompt, "workspace_slug": workspace_slug},
            },
        ],
    )


def _wait_for_container_http_ok(container_name: str, path: str, *, port: int, timeout_seconds: int = 60) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        code, _, _ = _container_http_json(container_name, "GET", path, port=port)
        if code == 200:
            return True
        time.sleep(2)
    return False


def _append_job_log(log_lines: list[str], message: str) -> None:
    log_lines.append(f"[{_iso_now()}] {message}")


def _load_appspec_schema() -> dict[str, Any]:
    return json.loads(APPSPEC_SCHEMA_PATH.read_text(encoding="utf-8"))


def _persist_json_artifact(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    kind: str,
    payload: dict[str, Any],
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    specs_root = _workspace_root() / "app_specs"
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
        extra_metadata={"workspace_id": str(workspace_id), **(metadata or {})},
        created_at=_utc_now(),
    )
    db.add(row)
    db.flush()
    return str(row.id)


def _normalize_unique_strings(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _infer_entities_from_prompt(raw_prompt: str) -> list[str]:
    prompt = str(raw_prompt or "").lower()
    entity_map = {
        "devices": ("device", "devices"),
        "locations": ("location", "locations", "site", "sites", "rack", "racks", "room", "rooms"),
        "interfaces": ("interface", "interfaces"),
        "ip_addresses": ("ip address", "ip addresses", "ip_address", "ip_addresses"),
        "vlans": ("vlan", "vlans"),
    }
    entities: list[str] = []
    for slug, tokens in entity_map.items():
        if any(token in prompt for token in tokens):
            entities.append(slug)
    if "devices" not in entities and any(token in prompt for token in ("inventory", "network")):
        entities.append("devices")
    return _normalize_unique_strings(entities)


def _infer_requested_visuals_from_prompt(raw_prompt: str) -> list[str]:
    prompt = str(raw_prompt or "").lower()
    visuals: list[str] = []
    if any(token in prompt for token in ("chart", "report")) and "devices" in prompt and "status" in prompt:
        visuals.append("devices_by_status_chart")
    if any(token in prompt for token in ("chart", "report")) and "interfaces" in prompt and "status" in prompt:
        visuals.append("interfaces_by_status_chart")
    return _normalize_unique_strings(visuals)


def _infer_entities_from_app_spec(app_spec: dict[str, Any]) -> list[str]:
    entities = _normalize_unique_strings(app_spec.get("entities") if isinstance(app_spec.get("entities"), list) else [])
    if entities:
        return entities
    inferred: list[str] = []
    service_names = {
        str(service.get("name") or "").strip().lower()
        for service in app_spec.get("services", [])
        if isinstance(service, dict)
    }
    if "net-inventory-api" in service_names:
        inferred.extend(["devices", "locations"])
    reports = _normalize_unique_strings(app_spec.get("reports") if isinstance(app_spec.get("reports"), list) else [])
    if any(report == "interfaces_by_status" for report in reports):
        inferred.append("interfaces")
    source_prompt = str(app_spec.get("source_prompt") or "")
    inferred.extend(_infer_entities_from_prompt(source_prompt))
    return _normalize_unique_strings(inferred)


def _build_app_spec(
    *,
    workspace_id: uuid.UUID,
    title: str,
    raw_prompt: str,
    initial_intent: Optional[dict[str, Any]] = None,
    current_app_spec: Optional[dict[str, Any]] = None,
    current_app_summary: Optional[dict[str, Any]] = None,
    revision_anchor: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    prompt = raw_prompt.lower()
    mentions_inventory = any(token in prompt for token in ("inventory", "device", "devices", "network"))
    base_spec = copy.deepcopy(current_app_spec) if isinstance(current_app_spec, dict) else {
        "schema_version": "xyn.appspec.v0",
        "services": [
            {
                "name": "net-inventory-api",
                "image": _effective_net_inventory_image(),
                "env": {
                    "PORT": "8080",
                    "DATABASE_URL": "postgresql://xyn:xyn_dev_password@net-inventory-db:5432/net_inventory",
                },
                "ports": [{"container": 8080, "host": 0, "protocol": "tcp"}],
                "depends_on": ["net-inventory-db"],
            },
            {
                "name": "net-inventory-db",
                "image": "postgres:16-alpine",
                "env": {
                    "POSTGRES_DB": "net_inventory",
                    "POSTGRES_USER": "xyn",
                    "POSTGRES_PASSWORD": "xyn_dev_password",
                },
                "ports": [{"container": 5432, "host": 0, "protocol": "tcp"}],
                "depends_on": [],
            },
        ],
        "ingress": {"enabled": False},
        "data": {"postgres": {"required": True, "service": "net-inventory-db"}},
        "reports": ["devices_by_status"],
    }

    app_slug = str(base_spec.get("app_slug") or "").strip() or (
        "net-inventory" if mentions_inventory else _safe_slug(title, default="net-inventory")
    )
    app_title = str(title or base_spec.get("title") or "Network Inventory").strip() or "Network Inventory"
    requested_entities = _normalize_unique_strings(
        (
            (initial_intent or {}).get("requested_entities")
            if isinstance((initial_intent or {}).get("requested_entities"), list)
            else []
        )
    )
    requested_visuals = _normalize_unique_strings(
        (
            (initial_intent or {}).get("requested_visuals")
            if isinstance((initial_intent or {}).get("requested_visuals"), list)
            else []
        )
    )
    inferred_entities = _infer_entities_from_prompt(raw_prompt)
    inferred_visuals = _infer_requested_visuals_from_prompt(raw_prompt)
    existing_entities = _infer_entities_from_app_spec(base_spec)
    summary_entities = _normalize_unique_strings(
        (
            (current_app_summary or {}).get("entities")
            if isinstance((current_app_summary or {}).get("entities"), list)
            else []
        )
    )
    entities = _normalize_unique_strings(existing_entities + summary_entities + requested_entities + inferred_entities)
    if not entities:
        entities = ["devices", "locations"]

    existing_reports = _normalize_unique_strings(
        base_spec.get("reports") if isinstance(base_spec.get("reports"), list) else []
    )
    reports = existing_reports[:]
    visuals = _normalize_unique_strings(
        (
            _normalize_unique_strings(base_spec.get("requested_visuals") if isinstance(base_spec.get("requested_visuals"), list) else [])
            + requested_visuals
            + inferred_visuals
        )
    )
    if "devices" in entities and "devices_by_status_chart" not in visuals and "devices_by_status" not in reports:
        visuals.append("devices_by_status_chart")
    visual_report_map = {
        "devices_by_status_chart": "devices_by_status",
        "interfaces_by_status_chart": "interfaces_by_status",
    }
    for visual in visuals:
        report = visual_report_map.get(visual)
        if report and report not in reports:
            reports.append(report)

    requires_primitives = _normalize_unique_strings(
        base_spec.get("requires_primitives") if isinstance(base_spec.get("requires_primitives"), list) else []
    )
    if any(token in prompt for token in ("location", "locations", "address", "site", "rack", "closet", "building", "room")):
        requires_primitives.append("location")
    if "locations" in entities and "location" not in requires_primitives:
        requires_primitives.append("location")

    phase_1_scope = _normalize_unique_strings(
        (
            (initial_intent or {}).get("phase_1_scope")
            if isinstance((initial_intent or {}).get("phase_1_scope"), list)
            else []
        )
    )
    if not phase_1_scope:
        phase_1_scope = entities[:]

    spec = copy.deepcopy(base_spec)
    spec["schema_version"] = "xyn.appspec.v0"
    spec["app_slug"] = app_slug
    spec["title"] = app_title
    spec["workspace_id"] = str(workspace_id)
    spec["source_prompt"] = raw_prompt
    spec["entities"] = entities
    spec["phase_1_scope"] = phase_1_scope
    spec["requested_visuals"] = visuals
    spec["reports"] = reports
    if requires_primitives:
        spec["requires_primitives"] = _normalize_unique_strings(requires_primitives)
    if revision_anchor:
        spec["revision_anchor"] = copy.deepcopy(revision_anchor)
    return spec


def _ports_yaml(ports: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for port in ports:
        host_port = int(port.get("host") or 0)
        container_port = int(port.get("container") or 0)
        protocol = str(port.get("protocol") or "tcp").strip().lower()
        if protocol not in {"tcp", "udp"}:
            protocol = "tcp"
        lines.append(f'      - "{host_port}:{container_port}/{protocol}"')
    return lines


def _resolve_published_port(container_name: str, target: str) -> int:
    code, stdout, stderr = _run(["docker", "port", container_name, target])
    if code != 0:
        raise RuntimeError(f"Failed to resolve published port for {container_name} {target}: {stderr or stdout}")
    first = (stdout.splitlines() or [""])[0].strip()
    if ":" not in first:
        raise RuntimeError(f"Unexpected docker port output: {first}")
    return int(first.rsplit(":", 1)[1])


def _docker_container_running(container_name: str) -> bool:
    code, stdout, _ = _run(["docker", "inspect", "-f", "{{.State.Running}}", container_name])
    return code == 0 and stdout.strip().lower() == "true"

def _docker_network_exists(network_name: str) -> bool:
    code, stdout, _ = _run(["docker", "network", "inspect", network_name])
    return code == 0 and bool(stdout.strip())


def _materialize_net_inventory_compose(
    *,
    app_spec: dict[str, Any],
    deployment_dir: Path,
    compose_project: str,
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
) -> Path:
    app_service = next((row for row in app_spec.get("services", []) if row.get("name") == "net-inventory-api"), {})
    db_service = next((row for row in app_spec.get("services", []) if row.get("name") == "net-inventory-db"), {})
    entity_contracts_json = json.dumps(
        build_resolved_capability_manifest(app_spec).get("entities") or [],
        separators=(",", ":"),
        sort_keys=True,
    ).replace("'", "''")
    app_image = str(app_service.get("image") or _effective_net_inventory_image())
    app_ports = _ports_yaml(list(app_service.get("ports") or [{"host": 0, "container": 8080, "protocol": "tcp"}]))
    app_network_lines: list[str] = []
    trailer_lines: list[str] = []
    if external_network_name:
        alias = str(external_network_alias or f"{compose_project}-api").strip() or f"{compose_project}-api"
        app_network_lines = [
            "    networks:",
            "      default:",
            "      sibling-runtime:",
            "        aliases:",
            f"          - {alias}",
        ]
        trailer_lines = [
            "",
            "networks:",
            "  sibling-runtime:",
            "    external: true",
            f"    name: {external_network_name}",
        ]
    compose = deployment_dir / "docker-compose.yml"
    compose.write_text(
        "\n".join(
            [
                "services:",
                "  net-inventory-db:",
                f"    image: {db_service.get('image') or 'postgres:16-alpine'}",
                f"    container_name: {compose_project}-db",
                "    restart: unless-stopped",
                "    environment:",
                "      POSTGRES_DB: net_inventory",
                "      POSTGRES_USER: xyn",
                "      POSTGRES_PASSWORD: xyn_dev_password",
                "    healthcheck:",
                "      test: [\"CMD-SHELL\", \"pg_isready -U xyn -d net_inventory\"]",
                "      interval: 5s",
                "      timeout: 5s",
                "      retries: 20",
                "",
                "  net-inventory-api:",
                f"    image: {app_image}",
                f"    container_name: {compose_project}-api",
                "    restart: unless-stopped",
                "    environment:",
                "      PORT: \"8080\"",
                "      DATABASE_URL: postgresql://xyn:xyn_dev_password@net-inventory-db:5432/net_inventory",
                f"      GENERATED_ENTITY_CONTRACTS_JSON: '{entity_contracts_json}'",
                "      GENERATED_ENTITY_CONTRACTS_ALLOW_DEFAULTS: \"0\"",
                "    ports:",
                *app_ports,
                *app_network_lines,
                "    depends_on:",
                "      net-inventory-db:",
                "        condition: service_healthy",
                "",
                *trailer_lines,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return compose


def _deploy_generated_runtime(
    *,
    app_spec: dict[str, Any],
    deployment_dir: Path,
    compose_project: str,
    logs: list[str],
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
) -> dict[str, Any]:
    compose_path = _materialize_net_inventory_compose(
        app_spec=app_spec,
        deployment_dir=deployment_dir,
        compose_project=compose_project,
        external_network_name=external_network_name,
        external_network_alias=external_network_alias,
    )
    _append_job_log(logs, f"Wrote compose: {compose_path}")

    down_cmd = ["docker", "compose", "-p", compose_project, "-f", str(compose_path), "down", "--remove-orphans", "--volumes"]
    up_cmd = ["docker", "compose", "-p", compose_project, "-f", str(compose_path), "up", "-d"]
    down_code, down_stdout, down_stderr = _run(down_cmd, cwd=deployment_dir)
    _append_job_log(logs, f"Executed: {' '.join(down_cmd)}")
    if down_stdout:
        _append_job_log(logs, f"compose down stdout: {down_stdout[-600:]}")
    if down_stderr:
        _append_job_log(logs, f"compose down stderr: {down_stderr[-600:]}")
    code, stdout, stderr = _run(up_cmd, cwd=deployment_dir)
    _append_job_log(logs, f"Executed: {' '.join(up_cmd)}")
    if stdout:
        _append_job_log(logs, f"compose stdout: {stdout[-600:]}")
    if code != 0:
        _run(["docker", "compose", "-p", compose_project, "-f", str(compose_path), "down", "--remove-orphans"], cwd=deployment_dir)
        raise RuntimeError(f"docker compose up failed: {stderr or stdout}")
    app_port = _resolve_published_port(f"{compose_project}-api", "8080/tcp")
    alias = str(external_network_alias or f"{compose_project}-api").strip() or f"{compose_project}-api"
    output = {
        "compose_project": compose_project,
        "deployment_dir": str(deployment_dir),
        "compose_path": str(compose_path),
        "app_container_name": f"{compose_project}-api",
        "app_url": f"http://localhost:{app_port}",
        "ports": {"app_tcp": app_port},
    }
    if external_network_name:
        output["runtime_base_url"] = f"http://{alias}:8080"
        output["runtime_owner"] = "sibling"
        output["external_network"] = external_network_name
        output["network_alias"] = alias
    return output


def _handle_generate_app_spec(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = job.input_json or {}
    title = str(payload.get("title") or "Network Inventory").strip() or "Network Inventory"
    content = payload.get("content_json") if isinstance(payload.get("content_json"), dict) else {}
    raw_prompt = str(content.get("raw_prompt") or payload.get("raw_prompt") or title).strip()
    initial_intent = content.get("initial_intent") if isinstance(content.get("initial_intent"), dict) else {}
    revision_anchor = content.get("revision_anchor") if isinstance(content.get("revision_anchor"), dict) else None
    current_app_summary = content.get("current_app_summary") if isinstance(content.get("current_app_summary"), dict) else None
    current_app_spec = content.get("current_app_spec") if isinstance(content.get("current_app_spec"), dict) else None
    primitive_catalog = get_primitive_catalog()
    _append_job_log(logs, f"Loaded primitive catalog ({len(primitive_catalog)} entries)")
    _append_job_log(logs, f"Generating AppSpec from prompt: {raw_prompt}")
    note = create_execution_note(
        db,
        workspace_id=job.workspace_id,
        prompt_or_request=raw_prompt,
        findings=[
            "App-intent draft submit reached the non-trivial generation path.",
            "Primitive catalog inspection is required before finalizing AppSpec generation.",
            "The prompt requests a workspace-scoped network inventory application.",
        ],
        root_cause="A durable AppSpec is required before deployment so runtime behavior remains auditable and artifact-linked.",
        proposed_fix="Generate an AppSpec first, persist it as an artifact, then queue deployment and validation stages while carrying the execution note forward.",
        implementation_summary="Started findings-first execution record for app generation.",
        validation_summary=["AppSpec generation not yet validated at note creation time."],
        debt_recorded=[],
        related_artifact_ids=[],
        status="in_progress",
        extra_metadata={"job_id": str(job.id), "job_type": job.type},
    )
    _append_job_log(logs, f"Created execution-note artifact: {note.id}")

    # TODO(artifact-first, DEBT-07):
    # The generated artifact now acts as the canonical runtime identity.
    # AppSpec remains primarily a build intermediate. Future work may
    # consolidate AppSpec into an ArtifactSpec so prompts generate artifacts
    # directly while preserving the current packaging and install semantics.
    app_spec = _build_app_spec(
        workspace_id=job.workspace_id,
        title=title,
        raw_prompt=raw_prompt,
        initial_intent=initial_intent,
        current_app_spec=current_app_spec,
        current_app_summary=current_app_summary,
        revision_anchor=revision_anchor,
    )
    try:
        validate(instance=app_spec, schema=_load_appspec_schema())
    except ValidationError as exc:
        raise RuntimeError(f"AppSpec validation failed: {exc.message}") from exc

    artifact_id = _persist_json_artifact(
        db,
        workspace_id=job.workspace_id,
        name=f"appspec.{app_spec['app_slug']}",
        kind="app_spec",
        payload=app_spec,
        metadata={"job_id": str(job.id)},
    )
    _append_job_log(logs, f"Persisted AppSpec artifact: {artifact_id}")

    selected_images = {svc.get("name"): svc.get("image") for svc in app_spec.get("services", []) if isinstance(svc, dict)}
    selected_ports = {
        svc.get("name"): svc.get("ports")
        for svc in app_spec.get("services", [])
        if isinstance(svc, dict)
    }
    generated_artifact_runtime_config = {
        "app_slug": app_spec["app_slug"],
        "artifact_slug": _generated_artifact_slug(str(app_spec.get("app_slug") or "generated-app")),
        "artifact_version": GENERATED_ARTIFACT_VERSION,
        "app_spec_artifact_id": artifact_id,
        "images": selected_images,
        "ports": selected_ports,
        "services": app_spec.get("services") if isinstance(app_spec.get("services"), list) else [],
        "workspace_id": str(job.workspace_id),
        "source_job_id": str(job.id),
    }
    packaged_artifact = _package_generated_app(
        workspace_id=job.workspace_id,
        source_job_id=str(job.id),
        app_spec=app_spec,
        runtime_config=generated_artifact_runtime_config,
    )
    _append_job_log(
        logs,
        f"Packaged generated artifact {packaged_artifact['artifact_slug']} at {packaged_artifact['artifact_package_path']}",
    )
    registry_artifact: dict[str, Any] = {}
    registry_import_error = ""
    try:
        registry_artifact = _import_generated_artifact_package(
            artifact_slug=str(packaged_artifact["artifact_slug"]),
            package_path=Path(str(packaged_artifact["artifact_package_path"])),
        )
        _append_job_log(
            logs,
            f"Imported generated artifact {packaged_artifact['artifact_slug']} into Django registry",
        )
    except Exception as exc:
        registry_import_error = f"{exc.__class__.__name__}: {exc}"
        _append_job_log(logs, f"Generated artifact import fallback engaged: {registry_import_error}")
    update_execution_note(
        db,
        artifact_id=note.id,
        implementation_summary="Generated and validated AppSpec, persisted it as an instance-local artifact, and packaged the generated app as an importable Django artifact bundle.",
        validation_summary=[
            "Primitive catalog loaded successfully.",
            "AppSpec validated against xyn.appspec.v0 schema.",
            f"AppSpec artifact persisted: {artifact_id}.",
            f"Generated artifact package created: {packaged_artifact['artifact_slug']}@{packaged_artifact['artifact_version']}.",
            (
                f"Generated artifact imported into registry: {packaged_artifact['artifact_slug']}"
                if registry_artifact
                else f"Generated artifact registry import deferred: {registry_import_error or 'unknown error'}."
            ),
        ],
        related_artifact_ids=[artifact_id],
        extra_metadata_updates={"app_spec_artifact_id": artifact_id},
    )
    follow_up = [
        {
            "type": "deploy_app_local",
            "input_json": {
                "app_spec": app_spec,
                "app_spec_artifact_id": artifact_id,
                "generated_artifact": {
                    **packaged_artifact,
                    "registry_import": registry_artifact,
                    "registry_import_error": registry_import_error,
                },
                "execution_note_artifact_id": str(note.id),
                "source_job_id": str(job.id),
            },
        }
    ]
    return (
        {
            "app_spec": app_spec,
            "app_spec_artifact_id": artifact_id,
            "app_spec_schema": "xyn.appspec.v0",
            "primitive_catalog": primitive_catalog,
            "selected_images": selected_images,
            "selected_ports": selected_ports,
            "derived_urls": {"seed_ui": "http://localhost", "seed_api": "http://seed.localhost"},
            "generated_artifact": {
                **packaged_artifact,
                "registry_import": registry_artifact,
                "registry_import_error": registry_import_error,
            },
            "execution_note_artifact_id": str(note.id),
        },
        follow_up,
    )


def _handle_deploy_app_local(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = job.input_json or {}
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    generated_artifact = payload.get("generated_artifact") if isinstance(payload.get("generated_artifact"), dict) else {}
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "net-inventory"), default="net-inventory")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    deployment_dir = _deployments_root() / app_slug / stamp
    deployment_dir.mkdir(parents=True, exist_ok=True)
    compose_project = _safe_slug(f"xyn-app-{app_slug}", default="xyn-app")
    app_output = {
        "app_slug": app_slug,
        **_deploy_generated_runtime(
            app_spec=app_spec,
            deployment_dir=deployment_dir,
            compose_project=compose_project,
            logs=logs,
        ),
    }
    if execution_note_artifact_id:
        update_execution_note(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Materialized local docker-compose deployment for the generated app and resolved a running app URL.",
            append_validation=[
                f"Compose written: {app_output['compose_path']}",
                f"Local deployment started successfully at {app_output['app_url']}.",
            ],
            related_artifact_ids=[
                *[str(item) for item in (payload.get('app_spec_artifact_id'), execution_note_artifact_id) if item],
            ],
            extra_metadata_updates={"app_url": app_output["app_url"], "compose_project": compose_project},
        )
    _append_job_log(logs, f"Local app URL: {app_output['app_url']}")
    _append_job_log(logs, "Queued sibling provisioning stage")
    follow_up = [
        {
            "type": "provision_sibling_xyn",
            "input_json": {
                "deployment": app_output,
                "app_spec": app_spec,
                "generated_artifact": generated_artifact,
                "execution_note_artifact_id": execution_note_artifact_id,
                "source_job_id": str(job.id),
            },
        }
    ]
    return app_output, follow_up


def _handle_provision_sibling_xyn(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = job.input_json or {}
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    deployment = payload.get("deployment") if isinstance(payload.get("deployment"), dict) else {}
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "net-inventory"), default="net-inventory")
    revision_anchor = app_spec.get("revision_anchor") if isinstance(app_spec.get("revision_anchor"), dict) else {}
    workspace = db.query(Workspace).filter(Workspace.id == job.workspace_id).first()
    workspace_slug = str(getattr(workspace, "slug", "default") or "default")
    sibling: dict[str, Any]
    reused_sibling = _find_revision_sibling_target(
        db,
        root_workspace_id=job.workspace_id,
        revision_anchor=revision_anchor,
        app_slug=app_slug,
    )
    if reused_sibling:
        sibling = {
            "deployment_id": reused_sibling.get("deployment_id"),
            "compose_project": reused_sibling.get("compose_project"),
            "ui_url": reused_sibling.get("ui_url"),
            "api_url": reused_sibling.get("api_url"),
        }
        _append_job_log(
            logs,
            "Reusing anchored sibling Xyn deployment "
            f"deployment_id={sibling.get('deployment_id')} ui_url={sibling.get('ui_url')}",
        )
    else:
        sibling_name = _safe_slug(f"smoke-{deployment.get('app_slug') or 'app'}-{str(job.id)[:6]}", default="smoke-app")
        ui_host = f"{sibling_name}.localhost"
        api_host = f"api.{sibling_name}.localhost"
        _append_job_log(logs, f"Provisioning sibling Xyn: name={sibling_name} ui_host={ui_host} api_host={api_host}")
        try:
            sibling = provision_local_instance(
                ProvisionLocalRequest(
                    name=sibling_name,
                    force=True,
                    workspace_slug=workspace_slug,
                    ui_host=ui_host,
                    api_host=api_host,
                )
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            raise RuntimeError(f"Sibling provisioning failed: {detail}") from exc

    sibling_output = {
        "deployment_id": sibling.get("deployment_id"),
        "compose_project": sibling.get("compose_project"),
        "ui_url": sibling.get("ui_url"),
        "api_url": sibling.get("api_url"),
    }
    sibling_project = str(sibling.get("compose_project") or "").strip()
    sibling_api_container = f"{sibling_project}-api" if sibling_project else ""
    sibling_network = f"{sibling_project}_default" if sibling_project else ""
    installed_artifact: dict[str, Any] | None = None
    sibling_runtime: dict[str, Any] | None = None
    sibling_registry_import: dict[str, Any] = {}
    generated_artifact = payload.get("generated_artifact") if isinstance(payload.get("generated_artifact"), dict) else {}
    if sibling_api_container and _docker_container_running(sibling_api_container):
        preferred_artifact_slug = str(generated_artifact.get("artifact_slug") or "").strip()
        preferred_artifact_version = str(generated_artifact.get("artifact_version") or "").strip()
        preferred_artifact_package_path = Path(str(generated_artifact.get("artifact_package_path") or "")).expanduser()
        if not preferred_artifact_slug or not preferred_artifact_package_path.exists():
            raise RuntimeError(
                f"Generated artifact package is missing for sibling install: "
                f"slug={preferred_artifact_slug or '<empty>'} path={preferred_artifact_package_path}"
            )
        sibling_registry_import = _import_generated_artifact_package_into_registry(
            container_name=sibling_api_container,
            artifact_slug=preferred_artifact_slug,
            package_path=preferred_artifact_package_path,
            port=8000,
        )
        _append_job_log(
            logs,
            f"Imported generated artifact {preferred_artifact_slug}@{preferred_artifact_version or GENERATED_ARTIFACT_VERSION} into sibling registry",
        )
        installed_artifact = _install_generated_artifact_in_sibling(
            sibling_api_container=sibling_api_container,
            workspace_slug=workspace_slug,
            artifact_slug=preferred_artifact_slug,
            artifact_version=preferred_artifact_version,
        )
        _append_job_log(
            logs,
            f"Installed generated artifact {preferred_artifact_slug}@{preferred_artifact_version or 'latest'} into sibling workspace",
        )
    sibling_output["installed_artifact"] = installed_artifact
    sibling_output["installed_artifact_source"] = "generated"
    if sibling_registry_import:
        sibling_output["generated_artifact_registry_import"] = sibling_registry_import
    _append_job_log(
        logs,
        "Installed sibling artifact "
        f"workspace={installed_artifact.get('workspace_slug')} artifact={installed_artifact.get('artifact_slug')} "
        "source=generated",
    )
    if not sibling_network or not _docker_network_exists(sibling_network):
        raise RuntimeError(f"Sibling network not available for runtime target registration: {sibling_network or '<empty>'}")
    sibling_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    reused_runtime_target = reused_sibling.get("runtime_target") if isinstance(reused_sibling, dict) and isinstance(reused_sibling.get("runtime_target"), dict) else {}
    sibling_runtime_project = str(reused_runtime_target.get("compose_project") or "").strip() or _safe_slug(
        f"xyn-sibling-{app_slug}-{str(job.id)[:6]}",
        default="xyn-sibling-app",
    )
    sibling_runtime_dir = _deployments_root() / app_slug / f"sibling-{sibling_stamp}-{str(job.id)[:6]}"
    sibling_runtime_dir.mkdir(parents=True, exist_ok=True)
    sibling_runtime = _deploy_generated_runtime(
        app_spec=app_spec,
        deployment_dir=sibling_runtime_dir,
        compose_project=sibling_runtime_project,
        logs=logs,
        external_network_name=str(reused_runtime_target.get("external_network") or sibling_network),
        external_network_alias=str(reused_runtime_target.get("network_alias") or f"{sibling_runtime_project}-api"),
    )
    sibling_runtime.update(
        {
            "app_slug": app_slug,
            "runtime_owner": "sibling",
            "source_build_job_id": str(payload.get("source_job_id") or ""),
            "source_workspace_id": str(job.workspace_id),
        }
    )
    registration = _register_sibling_runtime_target(
        sibling_api_container=sibling_api_container,
        workspace_id=str(installed_artifact.get("workspace_id") or ""),
        app_slug=app_slug,
        artifact_slug=str(installed_artifact.get("artifact_slug") or generated_artifact.get("artifact_slug") or f"app.{app_slug}"),
        title=str(app_spec.get("title") or app_slug),
        runtime_target=sibling_runtime,
    )
    sibling_output["runtime_target"] = sibling_runtime
    sibling_output["runtime_registration"] = registration
    _append_job_log(
        logs,
        "Registered sibling-owned runtime target "
        f"base_url={sibling_runtime.get('runtime_base_url')} workspace={installed_artifact.get('workspace_slug')}",
    )
    if execution_note_artifact_id:
        update_execution_note(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Provisioned a sibling Xyn instance as the next validation environment for the generated application.",
            append_validation=[
                f"Sibling Xyn provisioned with ui_url={sibling_output.get('ui_url')}",
                f"Sibling Xyn provisioned with api_url={sibling_output.get('api_url')}",
                (
                    f"Installed generated artifact {installed_artifact.get('artifact_slug')} into sibling workspace "
                    f"{installed_artifact.get('workspace_slug')}"
                    if installed_artifact
                    else "No sibling artifact installation was recorded."
                ),
                (
                    f"Registered sibling-owned runtime target {sibling_runtime.get('runtime_base_url')}"
                    if sibling_runtime
                    else "No sibling-owned runtime target was registered."
                ),
            ],
            extra_metadata_updates={
                "sibling_ui_url": sibling_output.get("ui_url"),
                "sibling_api_url": sibling_output.get("api_url"),
                "sibling_installed_artifact_slug": installed_artifact.get("artifact_slug") if installed_artifact else None,
                "sibling_runtime_base_url": sibling_runtime.get("runtime_base_url") if sibling_runtime else None,
            },
        )
    follow_up = [
        {
            "type": "smoke_test",
            "input_json": {
                "deployment": deployment,
                "sibling": sibling_output,
                "app_spec": app_spec,
                "generated_artifact": generated_artifact,
                "execution_note_artifact_id": execution_note_artifact_id,
                "source_job_id": str(job.id),
            },
        }
    ]
    return sibling_output, follow_up


def _handle_smoke_test(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = job.input_json or {}
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    deployment = payload.get("deployment") if isinstance(payload.get("deployment"), dict) else {}
    sibling = payload.get("sibling") if isinstance(payload.get("sibling"), dict) else {}
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    generated_artifact = payload.get("generated_artifact") if isinstance(payload.get("generated_artifact"), dict) else {}
    app_container_name = str(deployment.get("app_container_name") or "").strip()
    if not app_container_name:
        raise RuntimeError("smoke_test missing deployment.app_container_name")

    _append_job_log(logs, f"Waiting for app health in container: {app_container_name}")
    if not _wait_for_container_http_ok(app_container_name, "/health", port=8080, timeout_seconds=APP_DEPLOY_HEALTH_TIMEOUT_SECONDS):
        raise RuntimeError(f"App health endpoint did not become ready in {app_container_name}")

    workspace = db.query(Workspace).filter(Workspace.id == job.workspace_id).first()
    workspace_slug = str(getattr(workspace, "slug", "default") or "default")
    health_code, health_body, health_text = _container_http_json(app_container_name, "GET", "/health", port=8080)
    if health_code != 200:
        raise RuntimeError(f"App health check failed ({health_code}): {health_text}")

    list_code, list_body, list_text = _container_http_json(
        app_container_name,
        "GET",
        f"/devices?workspace_id={job.workspace_id}",
        port=8080,
    )
    if list_code != 200:
        raise RuntimeError(f"GET /devices failed ({list_code}): {list_text}")
    location_code, location_body, location_text = _container_http_json(
        app_container_name,
        "POST",
        "/locations",
        port=8080,
        payload={
            "workspace_id": str(job.workspace_id),
            "name": "seeded-location-1",
            "kind": "site",
            "city": "Austin",
        },
    )
    if location_code not in {200, 201}:
        raise RuntimeError(f"POST /locations failed ({location_code}): {location_text}")
    location_id = str(location_body.get("id") or "").strip() if isinstance(location_body, dict) else ""
    create_code, create_body, create_text = _container_http_json(
        app_container_name,
        "POST",
        "/devices",
        port=8080,
        payload={
            "workspace_id": str(job.workspace_id),
            "name": "seeded-device-1",
            "kind": "router",
            "status": "online",
            "location_id": location_id or None,
        },
    )
    if create_code not in {200, 201}:
        raise RuntimeError(f"POST /devices failed ({create_code}): {create_text}")
    list_locations_code, list_locations_body, list_locations_text = _container_http_json(
        app_container_name,
        "GET",
        f"/locations?workspace_id={job.workspace_id}",
        port=8080,
    )
    if list_locations_code != 200:
        raise RuntimeError(f"GET /locations failed ({list_locations_code}): {list_locations_text}")
    report_code, report_body, report_text = _container_http_json(
        app_container_name,
        "GET",
        f"/reports/devices-by-status?workspace_id={job.workspace_id}",
        port=8080,
    )
    if report_code != 200:
        raise RuntimeError(f"GET /reports/devices-by-status failed ({report_code}): {report_text}")

    generated_artifact_slug = str(generated_artifact.get("artifact_slug") or "").strip()
    generated_artifact_version = str(generated_artifact.get("artifact_version") or "").strip()
    registry_catalog: dict[str, Any] = {}
    if generated_artifact_slug:
        registry_status, registry_body, registry_text = _container_http_session_json(
            ROOT_PLATFORM_API_CONTAINER,
            port=8000,
            steps=[
                {
                    "method": "POST",
                    "path": "/auth/dev-login",
                    "form": {"appId": "xyn-ui", "returnTo": "/app"},
                },
                {
                    "method": "GET",
                    "path": "/xyn/api/artifacts/catalog",
                },
            ],
        )
        if registry_status != 200:
            raise RuntimeError(f"Registry catalog check failed ({registry_status}): {registry_text}")
        registry_rows = registry_body.get("artifacts") if isinstance(registry_body.get("artifacts"), list) else []
        registry_match = next(
            (
                row
                for row in registry_rows
                if isinstance(row, dict)
                and str(row.get("slug") or "").strip() == generated_artifact_slug
                and str(row.get("package_version") or "").strip() == generated_artifact_version
            ),
            None,
        )
        if not isinstance(registry_match, dict):
            raise RuntimeError(
                f"Generated artifact {generated_artifact_slug}@{generated_artifact_version} not found in registry catalog"
            )
        registry_catalog = registry_match

    sibling_project = str(sibling.get("compose_project") or "").strip()
    sibling_api_container = f"{sibling_project}-api" if sibling_project else ""
    sibling_ui_container = f"{sibling_project}-ui" if sibling_project else ""
    if not sibling_api_container or not _docker_container_running(sibling_api_container):
        raise RuntimeError("Sibling API container is not running")
    if not sibling_ui_container or not _docker_container_running(sibling_ui_container):
        raise RuntimeError("Sibling UI container is not running")
    sibling_health_code = 0
    sibling_health_body: dict[str, Any] | str = {}
    sibling_health_text = ""
    for health_path in ("/health", "/api/v1/health", "/xyn/api/health", "/xyn/api/v1/health", "/", "/xyn/api/auth/mode", "/xyn/api/me"):
        code, body, text = _container_http_json(sibling_api_container, "GET", health_path, port=8000)
        if code in {200, 401}:
            sibling_health_code = code
            sibling_health_body = body or {"path": health_path}
            sibling_health_text = text
            break
    if sibling_health_code != 200:
        raise RuntimeError(f"Sibling API health check failed ({code}): {text}")
    _append_job_log(logs, f"Sibling health OK: {sibling.get('api_url')}")
    sibling_runtime = sibling.get("runtime_target") if isinstance(sibling.get("runtime_target"), dict) else {}
    sibling_runtime_container = str(sibling_runtime.get("app_container_name") or "").strip()
    sibling_runtime_base_url = str(sibling_runtime.get("runtime_base_url") or "").strip()
    sibling_workspace_id = str((sibling.get("installed_artifact") or {}).get("workspace_id") or "").strip()
    sibling_workspace_slug = str((sibling.get("installed_artifact") or {}).get("workspace_slug") or workspace_slug).strip() or workspace_slug
    if not sibling_runtime_container or not _docker_container_running(sibling_runtime_container):
        raise RuntimeError("Sibling runtime container is not running")
    if not sibling_workspace_id:
        raise RuntimeError("Sibling installed artifact workspace id missing")
    if not _wait_for_container_http_ok(sibling_runtime_container, "/health", port=8080, timeout_seconds=APP_DEPLOY_HEALTH_TIMEOUT_SECONDS):
        raise RuntimeError(f"Sibling runtime health endpoint did not become ready in {sibling_runtime_container}")
    sibling_runtime_health_code, sibling_runtime_health_body, sibling_runtime_health_text = _container_http_json(
        sibling_runtime_container,
        "GET",
        "/health",
        port=8080,
    )
    if sibling_runtime_health_code != 200:
        raise RuntimeError(f"Sibling runtime health check failed ({sibling_runtime_health_code}): {sibling_runtime_health_text}")
    sibling_artifacts_status, sibling_artifacts_body, sibling_artifacts_text = _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "GET",
                "path": f"/xyn/api/workspaces/{sibling_workspace_id}/artifacts",
            },
        ],
    )
    if sibling_artifacts_status != 200:
        raise RuntimeError(f"Sibling artifact listing failed ({sibling_artifacts_status}): {sibling_artifacts_text}")
    sibling_artifacts = sibling_artifacts_body.get("artifacts") if isinstance(sibling_artifacts_body.get("artifacts"), list) else []
    if generated_artifact_slug:
        sibling_match = next(
            (
                row
                for row in sibling_artifacts
                if isinstance(row, dict)
                and str(row.get("slug") or "").strip() == generated_artifact_slug
                and str(row.get("package_version") or "").strip() == generated_artifact_version
            ),
            None,
        )
        if not isinstance(sibling_match, dict):
            raise RuntimeError(
                f"Sibling workspace is missing generated artifact {generated_artifact_slug}@{generated_artifact_version}"
            )
    sibling_location_code, sibling_location_body, sibling_location_text = _container_http_json(
        sibling_runtime_container,
        "POST",
        "/locations",
        port=8080,
        payload={
            "workspace_id": sibling_workspace_id,
            "name": "sibling-location-1",
            "kind": "site",
            "city": "Austin",
        },
    )
    if sibling_location_code not in {200, 201}:
        raise RuntimeError(f"Sibling POST /locations failed ({sibling_location_code}): {sibling_location_text}")
    sibling_location_id = str(sibling_location_body.get("id") or "").strip() if isinstance(sibling_location_body, dict) else ""
    sibling_create_code, sibling_create_body, sibling_create_text = _container_http_json(
        sibling_runtime_container,
        "POST",
        "/devices",
        port=8080,
        payload={
            "workspace_id": sibling_workspace_id,
            "name": "sibling-device-1",
            "kind": "router",
            "status": "online",
            "location_id": sibling_location_id or None,
        },
    )
    if sibling_create_code not in {200, 201}:
        raise RuntimeError(f"Sibling POST /devices failed ({sibling_create_code}): {sibling_create_text}")

    sibling_interface_code = 0
    sibling_interface_body: dict[str, Any] = {}
    sibling_interface_report_code = 0
    sibling_interface_report_body: dict[str, Any] = {}
    app_entities = _infer_entities_from_app_spec(app_spec)
    if "interfaces" in app_entities:
        sibling_device_id = str(sibling_create_body.get("id") or "").strip() if isinstance(sibling_create_body, dict) else ""
        if not sibling_device_id:
            raise RuntimeError("Sibling POST /devices did not return a device id required for interface seeding")
        sibling_interface_code, sibling_interface_body, sibling_interface_text = _container_http_json(
            sibling_runtime_container,
            "POST",
            "/interfaces",
            port=8080,
            payload={
                "workspace_id": sibling_workspace_id,
                "device_id": sibling_device_id,
                "name": "eth0",
                "status": "up",
            },
        )
        if sibling_interface_code not in {200, 201}:
            raise RuntimeError(f"Sibling POST /interfaces failed ({sibling_interface_code}): {sibling_interface_text}")
        sibling_interface_list_code, sibling_interface_list_body, sibling_interface_list_text = _container_http_json(
            sibling_runtime_container,
            "GET",
            f"/interfaces?workspace_id={sibling_workspace_id}",
            port=8080,
        )
        if sibling_interface_list_code != 200:
            raise RuntimeError(f"Sibling GET /interfaces failed ({sibling_interface_list_code}): {sibling_interface_list_text}")
        sibling_interface_items = sibling_interface_list_body if isinstance(sibling_interface_list_body, list) else (
            sibling_interface_list_body.get("items")
            if isinstance(sibling_interface_list_body, dict) and isinstance(sibling_interface_list_body.get("items"), list)
            else []
        )
        if not any(str(item.get("name") or "").strip() == "eth0" for item in sibling_interface_items if isinstance(item, dict)):
            raise RuntimeError("Sibling GET /interfaces did not include the seeded interface")
        sibling_interface_report_code, sibling_interface_report_body, sibling_interface_report_text = _container_http_json(
            sibling_runtime_container,
            "GET",
            f"/reports/interfaces-by-status?workspace_id={sibling_workspace_id}",
            port=8080,
        )
        if sibling_interface_report_code != 200:
            raise RuntimeError(
                f"Sibling GET /reports/interfaces-by-status failed ({sibling_interface_report_code}): {sibling_interface_report_text}"
            )

    palette_status, palette_result, palette_text = _execute_sibling_palette_prompt(
        sibling_api_container=sibling_api_container,
        workspace_slug=sibling_workspace_slug,
        prompt="show devices",
    )
    if palette_status != 200:
        raise RuntimeError(f"Sibling palette request failed ({palette_status}): {palette_text}")
    if palette_result.get("kind") != "table":
        raise RuntimeError(f"Palette did not return table: {palette_result}")
    if not isinstance(palette_result.get("rows"), list) or not palette_result.get("rows"):
        raise RuntimeError("Palette show devices returned no rows")
    palette_meta = palette_result.get("meta") if isinstance(palette_result.get("meta"), dict) else {}
    if sibling_runtime_base_url and str(palette_meta.get("base_url") or "").strip() != sibling_runtime_base_url:
        raise RuntimeError(
            f"Sibling palette targeted unexpected runtime base URL: {palette_meta.get('base_url')} != {sibling_runtime_base_url}"
        )
    _append_job_log(logs, f"Palette check returned {len(palette_result.get('rows') or [])} rows")
    palette_create_location_status, palette_create_location_result, palette_create_location_text = _execute_sibling_palette_prompt(
        sibling_api_container=sibling_api_container,
        workspace_slug=sibling_workspace_slug,
        prompt="create location",
    )
    if palette_create_location_status != 200:
        raise RuntimeError(
            f"Sibling palette create location request failed ({palette_create_location_status}): {palette_create_location_text}"
        )
    if palette_create_location_result.get("kind") != "text":
        raise RuntimeError(f"Palette did not return create-location completion prompt: {palette_create_location_result}")
    missing_fields = palette_create_location_result.get("meta", {}).get("missing_fields")
    if not isinstance(missing_fields, list) or "name" not in missing_fields or "city" not in missing_fields:
        raise RuntimeError(f"Palette create location did not report missing required fields: {palette_create_location_result}")

    palette_create_location_filled_status, palette_create_location_filled_result, palette_create_location_filled_text = _execute_sibling_palette_prompt(
        sibling_api_container=sibling_api_container,
        workspace_slug=sibling_workspace_slug,
        prompt="create location named sibling-location-2 in Austin TX USA",
    )
    if palette_create_location_filled_status != 200:
        raise RuntimeError(
            f"Sibling palette create location completion failed ({palette_create_location_filled_status}): {palette_create_location_filled_text}"
        )
    if palette_create_location_filled_result.get("kind") != "table":
        raise RuntimeError(f"Palette did not return location creation table: {palette_create_location_filled_result}")
    if not isinstance(palette_create_location_filled_result.get("rows"), list) or not palette_create_location_filled_result.get("rows"):
        raise RuntimeError("Palette create location completion returned no rows")

    palette_locations_status, palette_locations_result, palette_locations_text = _execute_sibling_palette_prompt(
        sibling_api_container=sibling_api_container,
        workspace_slug=sibling_workspace_slug,
        prompt="show locations",
    )
    if palette_locations_status != 200:
        raise RuntimeError(f"Sibling palette locations request failed ({palette_locations_status}): {palette_locations_text}")
    if palette_locations_result.get("kind") != "table":
        raise RuntimeError(f"Palette did not return locations table: {palette_locations_result}")
    if not isinstance(palette_locations_result.get("rows"), list) or not palette_locations_result.get("rows"):
        raise RuntimeError("Palette show locations returned no rows")
    sibling_interfaces_palette: dict[str, Any] = {}
    sibling_interfaces_chart: dict[str, Any] = {}
    if "interfaces" in app_entities:
        palette_interfaces_status, palette_interfaces_result, palette_interfaces_text = _execute_sibling_palette_prompt(
            sibling_api_container=sibling_api_container,
            workspace_slug=sibling_workspace_slug,
            prompt="show interfaces",
        )
        if palette_interfaces_status != 200:
            raise RuntimeError(f"Sibling palette interfaces request failed ({palette_interfaces_status}): {palette_interfaces_text}")
        if palette_interfaces_result.get("kind") != "table":
            raise RuntimeError(f"Palette did not return interface table: {palette_interfaces_result}")
        if not isinstance(palette_interfaces_result.get("rows"), list) or not palette_interfaces_result.get("rows"):
            raise RuntimeError("Palette show interfaces returned no rows")
        sibling_interfaces_palette = palette_interfaces_result

        palette_interfaces_chart_status, palette_interfaces_chart_result, palette_interfaces_chart_text = _execute_sibling_palette_prompt(
            sibling_api_container=sibling_api_container,
            workspace_slug=sibling_workspace_slug,
            prompt="show interfaces by status",
        )
        if palette_interfaces_chart_status != 200:
            raise RuntimeError(
                f"Sibling palette interface chart request failed ({palette_interfaces_chart_status}): {palette_interfaces_chart_text}"
            )
        if palette_interfaces_chart_result.get("kind") != "bar_chart":
            raise RuntimeError(f"Palette did not return interface chart: {palette_interfaces_chart_result}")
        sibling_interfaces_chart = palette_interfaces_chart_result

    stopped_root_runtime = {"status": "skipped"}
    restarted_root_runtime = {"status": "skipped"}
    palette_after_root_stop: dict[str, Any] = {}
    compose_path = Path(str(deployment.get("compose_path") or "").strip())
    compose_project = str(deployment.get("compose_project") or "").strip()
    if compose_path.exists() and compose_project:
        stop_code, stop_out, stop_err = _run(["docker", "compose", "-p", compose_project, "-f", str(compose_path), "stop"])
        if stop_code != 0:
            raise RuntimeError(f"Failed to stop root runtime during smoke validation: {stop_err or stop_out}")
        stopped_root_runtime = {"status": "stopped", "stdout": stop_out, "stderr": stop_err}
        try:
            palette_after_stop_status, palette_after_stop_result, palette_after_stop_text = _execute_sibling_palette_prompt(
                sibling_api_container=sibling_api_container,
                workspace_slug=sibling_workspace_slug,
                prompt="show devices",
            )
            if palette_after_stop_status != 200:
                raise RuntimeError(
                    f"Sibling palette after root stop failed ({palette_after_stop_status}): {palette_after_stop_text}"
                )
            if not isinstance(palette_after_stop_result.get("rows"), list) or not palette_after_stop_result.get("rows"):
                raise RuntimeError("Sibling palette after root stop returned no rows")
            after_stop_meta = palette_after_stop_result.get("meta") if isinstance(palette_after_stop_result.get("meta"), dict) else {}
            if sibling_runtime_base_url and str(after_stop_meta.get("base_url") or "").strip() != sibling_runtime_base_url:
                raise RuntimeError(
                    "Sibling palette after root stop targeted unexpected runtime base URL: "
                    f"{after_stop_meta.get('base_url')} != {sibling_runtime_base_url}"
                )
            palette_after_root_stop = palette_after_stop_result
        finally:
            restart_code, restart_out, restart_err = _run(["docker", "compose", "-p", compose_project, "-f", str(compose_path), "up", "-d"])
            restarted_root_runtime = {"status": "restarted" if restart_code == 0 else "failed", "stdout": restart_out, "stderr": restart_err}
            if restart_code != 0:
                raise RuntimeError(f"Failed to restart root runtime after smoke validation: {restart_err or restart_out}")
            if not _wait_for_container_http_ok(app_container_name, "/health", port=8080, timeout_seconds=APP_DEPLOY_HEALTH_TIMEOUT_SECONDS):
                raise RuntimeError("Root runtime did not become healthy after restart")
    if execution_note_artifact_id:
        update_execution_note(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Completed deployment smoke tests, sibling reachability checks, and palette verification for the generated application.",
            append_validation=[
                "App health endpoint returned 200.",
                "Location CRUD smoke checks succeeded.",
                "Device CRUD smoke checks succeeded.",
                "Sibling Xyn health check succeeded.",
                "Sibling runtime health endpoint returned 200.",
                "Sibling runtime location/device CRUD smoke checks succeeded.",
                f"Palette returned {len(palette_result.get('rows') or [])} rows for show devices.",
                f"Palette create location requested missing fields: {', '.join(str(field) for field in missing_fields)}.",
                f"Palette returned {len(palette_create_location_filled_result.get('rows') or [])} rows for completed create location.",
                f"Palette returned {len(palette_locations_result.get('rows') or [])} rows for show locations.",
                (
                    f"Palette returned {len(sibling_interfaces_palette.get('rows') or [])} rows for show interfaces."
                    if sibling_interfaces_palette
                    else "Interface palette validation not required for this AppSpec."
                ),
                (
                    f"Palette returned {len(sibling_interfaces_chart.get('values') or [])} buckets for show interfaces by status."
                    if sibling_interfaces_chart
                    else "Interface chart validation not required for this AppSpec."
                ),
                (
                    f"Sibling palette still returned {len(palette_after_root_stop.get('rows') or [])} rows after root runtime stop."
                    if palette_after_root_stop
                    else "Sibling palette was not revalidated after root runtime stop."
                ),
            ],
            status="completed",
        )

    return (
        {
            "app_health": {"code": health_code, "body": health_body or health_text},
            "app_checks": {
                "list_devices": {"code": list_code, "body": list_body or list_text},
                "create_location": {"code": location_code, "body": location_body or location_text},
                "list_locations": {"code": list_locations_code, "body": list_locations_body or list_locations_text},
                "create_device": {"code": create_code, "body": create_body or create_text},
                "report_devices_by_status": {"code": report_code, "body": report_body or report_text},
            },
            "sibling_health": {"code": sibling_health_code, "body": sibling_health_body or sibling_health_text},
            "sibling_runtime": {
                "base_url": sibling_runtime_base_url,
                "health": {"code": sibling_runtime_health_code, "body": sibling_runtime_health_body or sibling_runtime_health_text},
                "create_location": {"code": sibling_location_code, "body": sibling_location_body or sibling_location_text},
                "create_device": {"code": sibling_create_code, "body": sibling_create_body or sibling_create_text},
                "create_interface": (
                    {"code": sibling_interface_code, "body": sibling_interface_body}
                    if sibling_interface_code in {200, 201}
                    else None
                ),
                "report_interfaces_by_status": (
                    {"code": sibling_interface_report_code, "body": sibling_interface_report_body}
                    if sibling_interface_report_code == 200
                    else None
                ),
            },
            "sibling_xyn": sibling,
            "generated_artifact": {
                "registry_catalog": registry_catalog,
                "installed_in_sibling": generated_artifact_slug,
                "installed_version": generated_artifact_version,
            },
            "palette": palette_result,
            "palette_create_location": palette_create_location_result,
            "palette_locations": palette_locations_result,
            "palette_interfaces": sibling_interfaces_palette,
            "palette_interfaces_by_status": sibling_interfaces_chart,
            "palette_after_root_runtime_stop": palette_after_root_stop,
            "root_runtime_stop": stopped_root_runtime,
            "root_runtime_restart": restarted_root_runtime,
            "status": "passed",
        },
        [],
    )


def _claim_next_job(db: Session) -> Optional[Job]:
    row = (
        db.query(Job)
        .filter(Job.status == JobStatus.QUEUED.value)
        .order_by(Job.created_at.asc())
        .first()
    )
    if not row:
        return None
    row.status = JobStatus.RUNNING.value
    row.updated_at = _utc_now()
    prefix = row.logs_text.rstrip() + "\n" if row.logs_text else ""
    row.logs_text = f"{prefix}[{_iso_now()}] Worker claimed job {row.id} ({row.type})"
    db.commit()
    db.refresh(row)
    return row


def _enqueue_job(db: Session, *, workspace_id: uuid.UUID, job_type: str, input_json: dict[str, Any]) -> str:
    next_job = Job(
        workspace_id=workspace_id,
        type=job_type,
        status=JobStatus.QUEUED.value,
        input_json=input_json,
        output_json={},
        logs_text=f"[{_iso_now()}] Queued by app-job-worker.",
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(next_job)
    db.flush()
    return str(next_job.id)


def _recover_running_jobs(db: Session) -> None:
    running = db.query(Job).filter(Job.status == JobStatus.RUNNING.value).all()
    if not running:
        return
    for row in running:
        row.status = JobStatus.FAILED.value
        payload = row.output_json if isinstance(row.output_json, dict) else {}
        payload["error"] = "Job interrupted by process restart before completion."
        row.output_json = payload
        prefix = row.logs_text.rstrip() + "\n" if row.logs_text else ""
        row.logs_text = f"{prefix}[{_iso_now()}] Worker startup recovered stale RUNNING job as FAILED."
        row.updated_at = _utc_now()
    db.commit()


def _execute_job(job_id: uuid.UUID) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return
        logs: list[str] = []
        output_json: dict[str, Any] = {}
        follow_up_jobs: list[dict[str, Any]] = []
        _append_job_log(logs, f"Executing job type={job.type}")
        try:
            if job.type == "generate_app_spec":
                output_json, follow_up_jobs = _handle_generate_app_spec(db, job, logs)
            elif job.type == "deploy_app_local":
                output_json, follow_up_jobs = _handle_deploy_app_local(db, job, logs)
            elif job.type == "provision_sibling_xyn":
                output_json, follow_up_jobs = _handle_provision_sibling_xyn(db, job, logs)
            elif job.type == "smoke_test":
                output_json, follow_up_jobs = _handle_smoke_test(db, job, logs)
            else:
                raise RuntimeError(f"Unsupported job type: {job.type}")
            queued_ids = []
            for item in follow_up_jobs:
                next_id = _enqueue_job(
                    db,
                    workspace_id=job.workspace_id,
                    job_type=str(item.get("type") or "").strip(),
                    input_json=item.get("input_json") if isinstance(item.get("input_json"), dict) else {},
                )
                queued_ids.append({"job_type": item.get("type"), "job_id": next_id})
            if queued_ids:
                output_json["queued_jobs"] = queued_ids
                for item in queued_ids:
                    _append_job_log(logs, f"Queued follow-up job: {item['job_type']} ({item['job_id']})")
            job.status = JobStatus.SUCCEEDED.value
            job.output_json = output_json
            _append_job_log(logs, "Job completed successfully")
        except Exception as exc:
            job.status = JobStatus.FAILED.value
            output_json = output_json or {}
            output_json["error"] = str(exc)
            job.output_json = output_json
            execution_note_artifact_id = str((job.input_json or {}).get("execution_note_artifact_id") or (output_json or {}).get("execution_note_artifact_id") or "").strip()
            if execution_note_artifact_id:
                try:
                    update_execution_note(
                        db,
                        artifact_id=uuid.UUID(execution_note_artifact_id),
                        implementation_summary=f"Execution stopped during job type={job.type}.",
                        append_validation=[f"Failure during {job.type}: {exc}"],
                        status="failed",
                    )
                except Exception:
                    pass
            _append_job_log(logs, f"Job failed: {exc}")
        existing = job.logs_text.rstrip() + "\n" if job.logs_text else ""
        job.logs_text = existing + "\n".join(logs)
        job.updated_at = _utc_now()
        db.commit()
    finally:
        db.close()


def _worker_loop(stop_event: threading.Event) -> None:
    bootstrap_db = SessionLocal()
    try:
        _recover_running_jobs(bootstrap_db)
    finally:
        bootstrap_db.close()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            row = _claim_next_job(db)
            if not row:
                time.sleep(POLL_SECONDS)
                continue
            _execute_job(row.id)
        finally:
            db.close()


@dataclass
class AppJobWorkerHandle:
    thread: threading.Thread
    stop_event: threading.Event


def start_app_job_worker() -> AppJobWorkerHandle:
    stop_event = threading.Event()
    thread = threading.Thread(target=_worker_loop, args=(stop_event,), daemon=True, name="xyn-app-job-worker")
    thread.start()
    return AppJobWorkerHandle(thread=thread, stop_event=stop_event)


def stop_app_job_worker(handle: Optional[AppJobWorkerHandle]) -> None:
    if not handle:
        return
    handle.stop_event.set()
    handle.thread.join(timeout=5)
