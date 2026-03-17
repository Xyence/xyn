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
import re
import subprocess
import threading
import time
import uuid
import base64
import hashlib
import io
import zipfile
from collections import deque
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
POLICY_BUNDLE_SCHEMA_PATH = Path(__file__).resolve().parent / "contracts" / "policy_bundle_v0.schema.json"
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


def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _prefer_local_platform_images_for_smoke() -> bool:
    # Local app-builder smoke runs should validate against the platform code
    # currently running in this workspace, not a potentially stale :dev image.
    return _as_bool(os.getenv("XYN_APP_SMOKE_PREFER_LOCAL_IMAGES", "true"))


def _workspace_root() -> Path:
    root = Path(
        os.getenv("XYN_WORKSPACE_ROOT")
        or os.getenv("XYN_LOCAL_WORKSPACE_ROOT")
        or os.getenv("XYNSEED_WORKSPACE")
        or default_instance_workspace_root()
    ).resolve()
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
    return NET_INVENTORY_IMAGE


def _generated_artifact_slug(app_slug: str) -> str:
    return f"app.{_safe_slug(app_slug, default='generated-app')}"


def _policy_bundle_slug(app_slug: str) -> str:
    return f"policy.{_safe_slug(app_slug, default='generated-app')}"


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


def _build_generated_policy_artifact_manifest(*, app_spec: dict[str, Any], policy_bundle: dict[str, Any]) -> dict[str, Any]:
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "generated-app"), default="generated-app")
    artifact_slug = _policy_bundle_slug(app_slug)
    title = str(policy_bundle.get("title") or f"{str(app_spec.get('title') or app_slug).strip() or app_slug} Policy Bundle").strip()
    families = list(policy_bundle.get("policy_families") or [])
    return {
        "artifact": {
            "id": artifact_slug,
            "type": "policy_bundle",
            "slug": artifact_slug,
            "version": GENERATED_ARTIFACT_VERSION,
            "name": title,
            "generated": True,
        },
        "capability": {
            "visibility": "contextual",
            "category": "policy",
            "label": title,
            "description": "Generated application policy bundle for future validation, rendering, explanation, and enforcement flows.",
            "tags": ["generated", "policy_bundle", app_slug],
            "order": 140,
        },
        "summary": {
            "app_slug": app_slug,
            "policy_families": families,
            "policy_count": sum(
                len(policy_bundle.get("policies", {}).get(key) or [])
                for key in (
                    "validation_policies",
                    "relation_constraints",
                    "transition_policies",
                    "invariant_policies",
                    "derived_policies",
                    "trigger_policies",
                )
            ),
            "future_capabilities": list((policy_bundle.get("explanation") or {}).get("future_capabilities") or []),
        },
        "content": {
            "policy_bundle": policy_bundle,
            "app_slug": app_slug,
            "generated_artifact_slug": _generated_artifact_slug(app_slug),
        },
    }


def _package_generated_app(
    *,
    workspace_id: uuid.UUID,
    source_job_id: str,
    app_spec: dict[str, Any],
    policy_bundle: dict[str, Any],
    runtime_config: dict[str, Any],
) -> dict[str, Any]:
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "generated-app"), default="generated-app")
    artifact_slug = _generated_artifact_slug(app_slug)
    policy_artifact_slug = _policy_bundle_slug(app_slug)
    package_root = _generated_artifacts_root() / app_slug
    payload_root = package_root / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    artifact_manifest = _build_generated_artifact_manifest(app_spec=app_spec, runtime_config=runtime_config)
    artifact_manifest["content"]["policy_bundle_summary"] = {
        "artifact_slug": policy_artifact_slug,
        "title": str(policy_bundle.get("title") or "").strip(),
        "policy_families": list(policy_bundle.get("policy_families") or []),
    }
    policy_artifact_manifest = _build_generated_policy_artifact_manifest(app_spec=app_spec, policy_bundle=policy_bundle)
    artifact_manifest_path = package_root / "artifact.json"
    app_spec_path = payload_root / "app_spec.json"
    policy_bundle_path = payload_root / "policy_bundle.json"
    runtime_config_path = payload_root / "runtime_config.json"
    artifact_manifest_path.write_text(json.dumps(artifact_manifest, indent=2, sort_keys=True), encoding="utf-8")
    app_spec_path.write_text(json.dumps(app_spec, indent=2, sort_keys=True), encoding="utf-8")
    policy_bundle_path.write_text(json.dumps(policy_bundle, indent=2, sort_keys=True), encoding="utf-8")
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
    policy_artifact_entry = {
        "type": "policy_bundle",
        "slug": policy_artifact_slug,
        "version": GENERATED_ARTIFACT_VERSION,
        "artifact_id": policy_artifact_slug,
        "title": str(policy_bundle.get("title") or f"{str(app_spec.get('title') or app_slug).strip() or app_slug} Policy Bundle"),
        "description": "Generated application policy bundle",
        "dependencies": [],
        "bindings": [],
    }
    files: dict[str, bytes] = {}
    base = f"artifacts/application/{artifact_slug}/{GENERATED_ARTIFACT_VERSION}"
    policy_base = f"artifacts/policy_bundle/{policy_artifact_slug}/{GENERATED_ARTIFACT_VERSION}"
    artifact_zip_path = f"{base}/artifact.json"
    payload_zip_path = f"{base}/payload/payload.json"
    surfaces_zip_path = f"{base}/surfaces.json"
    runtime_roles_zip_path = f"{base}/runtime_roles.json"
    policy_artifact_zip_path = f"{policy_base}/artifact.json"
    policy_payload_zip_path = f"{policy_base}/payload/payload.json"
    policy_surfaces_zip_path = f"{policy_base}/surfaces.json"
    policy_runtime_roles_zip_path = f"{policy_base}/runtime_roles.json"
    combined_payload = {
        "app_spec": app_spec,
        "policy_bundle": policy_bundle,
        "runtime_config": runtime_config,
        "generated": True,
        "source_job_id": source_job_id,
        "source_workspace_id": str(workspace_id),
    }
    policy_payload = {
        "policy_bundle": policy_bundle,
        "generated": True,
        "source_job_id": source_job_id,
        "source_workspace_id": str(workspace_id),
        "app_slug": app_slug,
    }
    files[artifact_zip_path] = json.dumps(artifact_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[payload_zip_path] = json.dumps(combined_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[surfaces_zip_path] = b"[]"
    files[runtime_roles_zip_path] = b"[]"
    files[policy_artifact_zip_path] = json.dumps(policy_artifact_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[policy_payload_zip_path] = json.dumps(policy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[policy_surfaces_zip_path] = b"[]"
    files[policy_runtime_roles_zip_path] = b"[]"
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
            },
            {
                **policy_artifact_entry,
                "artifact_hash": hashlib.sha256(files[policy_artifact_zip_path]).hexdigest(),
            },
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
        "policy_bundle_slug": policy_artifact_slug,
        "artifact_manifest_path": str(artifact_manifest_path),
        "artifact_package_path": str(package_zip_path),
        "artifact_dir": str(package_root),
        "runtime_config_path": str(runtime_config_path),
        "app_spec_path": str(app_spec_path),
        "policy_bundle_path": str(policy_bundle_path),
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


def _load_policy_bundle_schema() -> dict[str, Any]:
    return json.loads(POLICY_BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8"))


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


def _policy_family_from_statement(statement: str) -> str:
    lowered = str(statement or "").strip().lower()
    if any(token in lowered for token in ("vote counts", "counts per", "count per", "rollup", "aggregate", "total")):
        return "derived_policies"
    if "selected" in lowered and any(token in lowered for token in ("exactly one", "more than one", "only one", "at most one", "at least one")):
        return "invariant_policies"
    if any(token in lowered for token in ("does not belong", "belong to", "exactly one", "more than one", "only one")):
        return "relation_constraints"
    if any(token in lowered for token in ("automatically", "when ", "upon ", "after ")) and any(
        token in lowered for token in ("become", "set ", "mark ", "selected")
    ):
        return "trigger_policies"
    if any(token in lowered for token in ("status", "state", "transition", "allows", "does not allow", "must have")):
        return "transition_policies"
    return "validation_policies"


def _policy_targets_from_statement(statement: str, *, entity_contracts: list[dict[str, Any]]) -> dict[str, Any]:
    lowered = str(statement or "").strip().lower()
    entity_keys: list[str] = []
    field_names: list[str] = []
    for contract in entity_contracts:
        if not isinstance(contract, dict):
            continue
        entity_key = str(contract.get("key") or "").strip()
        singular = str(contract.get("singular_label") or entity_key.rstrip("s")).strip().lower()
        plural = str(contract.get("plural_label") or entity_key).strip().lower()
        if singular and singular in lowered or plural and plural in lowered:
            entity_keys.append(entity_key)
        for field in contract.get("fields") if isinstance(contract.get("fields"), list) else []:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or "").strip()
            if field_name and field_name.replace("_", " ") in lowered:
                field_names.append(field_name)
    return {
        "entity_keys": _normalize_unique_strings(entity_keys),
        "field_names": _normalize_unique_strings(field_names),
    }


def _policy_entity_token_matches(contract: dict[str, Any], token: str) -> bool:
    normalized = str(token or "").strip().lower()
    if not normalized:
        return False
    singular = str(contract.get("singular_label") or str(contract.get("key") or "").rstrip("s")).strip().lower()
    plural = str(contract.get("plural_label") or contract.get("key") or "").strip().lower()
    candidates = {singular, plural}
    if singular.endswith("e"):
        candidates.add(f"{singular[:-1]}ing")
    if singular:
        candidates.add(f"{singular}ing")
    return normalized in {item for item in candidates if item}


def _policy_statement_entity_mentions(statement: str, *, entity_contracts: list[dict[str, Any]]) -> list[str]:
    lowered = str(statement or "").strip().lower()
    tokens = re.findall(r"[a-z_]+", lowered)
    mentions: list[str] = []
    for contract in entity_contracts:
        if not isinstance(contract, dict):
            continue
        singular = str(contract.get("singular_label") or str(contract.get("key") or "").rstrip("s")).strip().lower()
        plural = str(contract.get("plural_label") or contract.get("key") or "").strip().lower()
        phrase_match = any(candidate and candidate in lowered for candidate in (singular, plural))
        token_match = any(_policy_entity_token_matches(contract, token) for token in tokens)
        if phrase_match or token_match:
            mentions.append(str(contract.get("key") or "").strip())
    return _normalize_unique_strings(mentions)


def _policy_status_field(contract: dict[str, Any]) -> tuple[str | None, list[str]]:
    for field in contract.get("fields") if isinstance(contract.get("fields"), list) else []:
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("name") or "").strip()
        options = [str(option).strip() for option in field.get("options") if str(option).strip()] if isinstance(field.get("options"), list) else []
        if field_name == "status" and options:
            return field_name, options
    return None, []


def _compile_relation_constraint_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    sequence = start_sequence
    for entity_key, contract in contracts.items():
        relationships = contract.get("relationships") if isinstance(contract.get("relationships"), list) else []
        relation_rows = [row for row in relationships if isinstance(row, dict) and str(row.get("field") or "").strip()]
        if len(relation_rows) < 2:
            continue
        for relation in relation_rows:
            source_field = str(relation.get("field") or "").strip()
            related_entity = str(relation.get("target_entity") or "").strip()
            if not source_field or not related_entity:
                continue
            related_contract = contracts.get(related_entity)
            if not related_contract:
                continue
            related_relationships = related_contract.get("relationships") if isinstance(related_contract.get("relationships"), list) else []
            for sibling_relation in relation_rows:
                comparison_field = str(sibling_relation.get("field") or "").strip()
                comparison_entity = str(sibling_relation.get("target_entity") or "").strip()
                if not comparison_field or not comparison_entity or comparison_field == source_field:
                    continue
                backlink = next(
                    (
                        row
                        for row in related_relationships
                        if isinstance(row, dict)
                        and str(row.get("target_entity") or "").strip() == comparison_entity
                        and str(row.get("field") or "").strip()
                    ),
                    None,
                )
                if not backlink:
                    continue
                key = (entity_key, source_field, comparison_field, str(backlink.get("field") or "").strip())
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{entity_key}.{source_field} must align with {comparison_field}",
                        "description": (
                            f"Ensure the related {related_entity.rstrip('s')} referenced by {source_field} belongs to the same "
                            f"{comparison_entity.rstrip('s')} referenced by {comparison_field}."
                        ),
                        "family": "relation_constraints",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [entity_key, related_entity, comparison_entity],
                            "field_names": [source_field, comparison_field, str(backlink.get("field") or "").strip()],
                        },
                        "parameters": {
                            "runtime_rule": "match_related_field",
                            "entity_key": entity_key,
                            "source_field": source_field,
                            "related_entity": related_entity,
                            "related_lookup_field": str(relation.get("target_field") or "id").strip() or "id",
                            "related_match_field": str(backlink.get("field") or "").strip(),
                            "comparison_field": comparison_field,
                            "comparison_entity": comparison_entity,
                        },
                        "source": {
                            "kind": "derived_from_entity_contracts",
                            "reason": "multiple_relationship_consistency",
                        },
                        "explanation": {
                            "user_summary": (
                                f"{entity_key.rstrip('s').replace('_', ' ')} references must stay aligned across related records."
                            ),
                            "why_it_exists": "Derived from generated relationship structure so cross-parent mismatches are rejected generically.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _compile_parent_status_gate_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for section in ("behavior", "validation") for item in sections.get(section, []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        mentions = _policy_statement_entity_mentions(statement, entity_contracts=entity_contracts)
        if not mentions:
            continue
        child_contract = next(
            (
                contract
                for contract in entity_contracts
                if isinstance(contract, dict)
                and any(_policy_entity_token_matches(contract, token) for token in re.findall(r"[a-z_]+", lowered))
                and len(contract.get("relationships") or []) > 0
            ),
            None,
        )
        if not child_contract:
            continue
        child_entity = str(child_contract.get("key") or "").strip()
        for relation in child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []:
            if not isinstance(relation, dict):
                continue
            parent_entity = str(relation.get("target_entity") or "").strip()
            if parent_entity not in mentions:
                continue
            parent_contract = contracts.get(parent_entity)
            if not parent_contract:
                continue
            status_field, status_options = _policy_status_field(parent_contract)
            if not status_field or not status_options:
                continue
            mentioned_statuses = [option for option in status_options if re.search(rf"\b{re.escape(option.lower())}\b", lowered)]
            allowed_statuses: list[str] = []
            blocked_statuses: list[str] = []
            if re.search(r"\bnot\s+\w+\b", lowered) and "prevent" in lowered and mentioned_statuses:
                allowed_statuses = mentioned_statuses
            elif "does not allow" in lowered or "not allow" in lowered or "blocked" in lowered:
                blocked_statuses = mentioned_statuses
            elif "allow" in lowered and mentioned_statuses:
                allowed_statuses = mentioned_statuses
            if not allowed_statuses and not blocked_statuses:
                continue
            key = (child_entity, str(relation.get("field") or "").strip(), tuple(sorted(allowed_statuses)), tuple(sorted(blocked_statuses)))
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": f"{app_slug}-{sequence:03d}",
                    "name": f"{child_entity} writes must respect {parent_entity} status",
                    "description": statement,
                    "family": "validation_policies",
                    "status": "compiled",
                    "enforcement_stage": "runtime_enforced",
                    "targets": {
                        "entity_keys": [child_entity, parent_entity],
                        "field_names": [str(relation.get("field") or "").strip(), status_field],
                    },
                    "parameters": {
                        "runtime_rule": "parent_status_gate",
                        "entity_key": child_entity,
                        "parent_entity": parent_entity,
                        "parent_relation_field": str(relation.get("field") or "").strip(),
                        "parent_status_field": status_field,
                        "allowed_parent_statuses": allowed_statuses,
                        "blocked_parent_statuses": blocked_statuses,
                        "on_operations": ["create", "update"],
                    },
                    "source": {
                        "kind": "prompt_section",
                        "text": statement,
                    },
                    "explanation": {
                        "user_summary": statement,
                        "why_it_exists": "Derived from prompt-described status-gated write behavior.",
                    },
                }
            )
            sequence += 1
    return rows, sequence


def _compile_transition_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    statements = " ".join(str(item or "").strip().lower() for section in ("behavior", "validation") for item in sections.get(section, []))
    sequence = start_sequence
    rows: list[dict[str, Any]] = []
    for contract in entity_contracts:
        if not isinstance(contract, dict):
            continue
        entity_key = str(contract.get("key") or "").strip()
        if not entity_key:
            continue
        singular = str(contract.get("singular_label") or entity_key.rstrip("s")).strip().lower()
        status_field, status_options = _policy_status_field(contract)
        if not status_field or len(status_options) < 2:
            continue
        if singular not in statements and entity_key not in statements and "status" not in statements:
            continue
        allowed_transitions = {
            option: _normalize_unique_strings(
                [option]
                + ([status_options[index + 1]] if index + 1 < len(status_options) else [])
            )
            for index, option in enumerate(status_options)
        }
        rows.append(
            {
                "id": f"{app_slug}-{sequence:03d}",
                "name": f"{entity_key}.{status_field} transition guard",
                "description": f"Restrict {entity_key} {status_field} changes to the declared ordered states.",
                "family": "transition_policies",
                "status": "compiled",
                "enforcement_stage": "runtime_enforced",
                "targets": {
                    "entity_keys": [entity_key],
                    "field_names": [status_field],
                },
                "parameters": {
                    "runtime_rule": "field_transition_guard",
                    "entity_key": entity_key,
                    "field_name": status_field,
                    "allowed_transitions": allowed_transitions,
                },
                "source": {
                    "kind": "derived_from_entity_contracts",
                    "reason": "ordered_status_enum",
                },
                "explanation": {
                    "user_summary": f"{entity_key.replace('_', ' ')} status changes follow the declared status order.",
                    "why_it_exists": "Derived from ordered status options in the generated entity contract.",
                },
            }
        )
        sequence += 1
    return rows, sequence


def _contract_field(contract: dict[str, Any], field_name: str) -> dict[str, Any] | None:
    for field in contract.get("fields") if isinstance(contract.get("fields"), list) else []:
        if not isinstance(field, dict):
            continue
        if str(field.get("name") or "").strip() == field_name:
            return field
    return None


def _compile_related_count_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("validation", []) + sections.get("views", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "count" not in lowered and "counts" not in lowered:
            continue
        mentions = _policy_statement_entity_mentions(statement, entity_contracts=entity_contracts)
        for child_entity, child_contract in contracts.items():
            relationships = child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                parent_entity = str(relation.get("target_entity") or "").strip()
                relation_field = str(relation.get("field") or "").strip()
                if not parent_entity or not relation_field:
                    continue
                if child_entity not in mentions or parent_entity not in mentions:
                    continue
                key = (parent_entity, child_entity, relation_field)
                if key in seen:
                    continue
                seen.add(key)
                output_field = f"{child_entity}_count"
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{parent_entity} {child_entity} count",
                        "description": statement,
                        "family": "derived_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [parent_entity, child_entity],
                            "field_names": [relation_field, output_field],
                        },
                        "parameters": {
                            "runtime_rule": "related_count",
                            "entity_key": parent_entity,
                            "child_entity": child_entity,
                            "child_relation_field": relation_field,
                            "output_field": output_field,
                            "surfaces": ["list", "detail"],
                        },
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described aggregate/count requirement.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _compile_trigger_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("behavior", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "automatic" not in lowered and "automatically" not in lowered:
            continue
        mentions = _policy_statement_entity_mentions(statement, entity_contracts=entity_contracts)
        if len(mentions) < 2:
            continue
        for source_entity, source_contract in contracts.items():
            if source_entity not in mentions:
                continue
            relationships = source_contract.get("relationships") if isinstance(source_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                target_entity = str(relation.get("target_entity") or "").strip()
                relation_field = str(relation.get("field") or "").strip()
                if not target_entity or target_entity not in mentions:
                    continue
                condition_field = ""
                condition_value: Any = None
                selected_field = _contract_field(source_contract, "selected")
                if isinstance(selected_field, dict):
                    condition_field = "selected"
                    condition_value = "yes" if "yes" in [str(option).strip().lower() for option in selected_field.get("options") or []] else True
                else:
                    status_field, status_options = _policy_status_field(source_contract)
                    if status_field and "selected" in {option.lower() for option in status_options}:
                        condition_field = status_field
                        condition_value = "selected"
                target_status_field, target_status_options = _policy_status_field(contracts.get(target_entity, {}))
                if not condition_field or not target_status_field or "selected" not in {option.lower() for option in target_status_options}:
                    continue
                trigger_key = (source_entity, condition_field, str(condition_value), target_entity, relation_field, target_status_field)
                if trigger_key in seen:
                    continue
                seen.add(trigger_key)
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{source_entity} selected updates {target_entity} status",
                        "description": statement,
                        "family": "trigger_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [source_entity, target_entity],
                            "field_names": [condition_field, relation_field, target_status_field],
                        },
                        "parameters": {
                            "runtime_rule": "post_write_related_update",
                            "source_entity": source_entity,
                            "on_operations": ["create", "update"],
                            "condition_field": condition_field,
                            "condition_equals": condition_value,
                            "target_entity": target_entity,
                            "target_relation_field": relation_field,
                            "target_lookup_field": str(relation.get("target_field") or "id").strip() or "id",
                            "target_update_field": target_status_field,
                            "target_update_value": "selected",
                        },
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described post-write state update behavior.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _compile_parent_scoped_uniqueness_invariants(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("behavior", []) + sections.get("validation", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "selected" not in lowered or not any(token in lowered for token in ("only one", "exactly one", "more than one", "at most one")):
            continue
        mentions = set(_policy_statement_entity_mentions(statement, entity_contracts=entity_contracts))
        for child_entity, child_contract in contracts.items():
            selected_field = _contract_field(child_contract, "selected")
            if not isinstance(selected_field, dict):
                continue
            relationships = child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                parent_entity = str(relation.get("target_entity") or "").strip()
                parent_relation_field = str(relation.get("field") or "").strip()
                if not parent_entity or not parent_relation_field:
                    continue
                if mentions and (child_entity not in mentions or parent_entity not in mentions):
                    continue
                invariant_key = (child_entity, parent_entity, parent_relation_field, "selected")
                if invariant_key in seen:
                    continue
                seen.add(invariant_key)
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{child_entity} selection unique within {parent_entity}",
                        "description": statement,
                        "family": "invariant_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [child_entity, parent_entity],
                            "field_names": [parent_relation_field, "selected"],
                        },
                        "parameters": {
                            "runtime_rule": "at_most_one_matching_child_per_parent",
                            "entity_key": child_entity,
                            "parent_entity": parent_entity,
                            "parent_relation_field": parent_relation_field,
                            "match_field": "selected",
                            "match_value": "yes",
                            "on_operations": ["create", "update"],
                        },
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described single-selection invariant.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _infer_parent_state_gate_from_statement(
    *,
    statement: str,
    parent_contract: dict[str, Any],
) -> tuple[str | None, str | None]:
    status_field, status_options = _policy_status_field(parent_contract)
    if not status_field or not status_options:
        return None, None
    lowered = str(statement or "").strip().lower()
    for option in status_options:
        lowered_option = str(option or "").strip().lower()
        if not lowered_option:
            continue
        if (
            f"in {lowered_option} status" in lowered
            or f"status {lowered_option}" in lowered
            or f"status is {lowered_option}" in lowered
            or f"status = {lowered_option}" in lowered
        ):
            return status_field, option
    return None, None


def _compile_parent_scoped_minimum_selection_invariants(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("behavior", []) + sections.get("validation", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "selected" not in lowered:
            continue
        if not any(token in lowered for token in ("exactly one", "at least one", "must have one", "must have exactly one")):
            continue
        mentions = set(_policy_statement_entity_mentions(statement, entity_contracts=entity_contracts))
        for child_entity, child_contract in contracts.items():
            selected_field = _contract_field(child_contract, "selected")
            if not isinstance(selected_field, dict):
                continue
            relationships = child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                parent_entity = str(relation.get("target_entity") or "").strip()
                parent_relation_field = str(relation.get("field") or "").strip()
                if not parent_entity or not parent_relation_field:
                    continue
                if mentions and (child_entity not in mentions or parent_entity not in mentions):
                    continue
                parent_contract = contracts.get(parent_entity)
                if not parent_contract:
                    continue
                parent_state_field, parent_state_value = _infer_parent_state_gate_from_statement(
                    statement=statement,
                    parent_contract=parent_contract,
                )
                invariant_key = (
                    child_entity,
                    parent_entity,
                    parent_relation_field,
                    "selected",
                    parent_state_field or "",
                    parent_state_value or "",
                )
                if invariant_key in seen:
                    continue
                seen.add(invariant_key)
                parameters: dict[str, Any] = {
                    "runtime_rule": "at_least_one_matching_child_per_parent",
                    "entity_key": child_entity,
                    "parent_entity": parent_entity,
                    "parent_relation_field": parent_relation_field,
                    "match_field": "selected",
                    "match_value": "yes",
                    "on_parent_operations": ["create", "update"],
                    "on_child_operations": ["create", "update", "delete"],
                }
                if parent_state_field and parent_state_value:
                    parameters["parent_state_field"] = parent_state_field
                    parameters["parent_state_value"] = parent_state_value
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{parent_entity} requires selected {child_entity}",
                        "description": statement,
                        "family": "invariant_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [parent_entity, child_entity],
                            "field_names": _normalize_unique_strings(
                                [parent_relation_field, "selected", parent_state_field or ""]
                            ),
                        },
                        "parameters": parameters,
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described required-selection invariant.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _augment_contracts_with_inferred_selection_flags(
    *,
    raw_prompt: str,
    contracts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prompt = str(raw_prompt or "").lower()
    if "selected" not in prompt:
        return contracts
    updated = copy.deepcopy(contracts)
    for contract in updated:
        if not isinstance(contract, dict):
            continue
        relationships = contract.get("relationships") if isinstance(contract.get("relationships"), list) else []
        if not relationships:
            continue
        fields = contract.get("fields") if isinstance(contract.get("fields"), list) else []
        field_names = {str(field.get("name") or "").strip() for field in fields if isinstance(field, dict)}
        if "selected" in field_names:
            continue
        singular = str(contract.get("singular_label") or str(contract.get("key") or "").rstrip("s")).strip().lower()
        plural = str(contract.get("plural_label") or contract.get("key") or "").strip().lower()
        if singular not in prompt and plural not in prompt:
            continue
        if not any(token in prompt for token in (f"selected {singular}", f"selected {plural}", f"{singular} is selected", f"{plural} is selected", f"mark one {singular}", f"mark {singular}")):
            continue
        fields.append(
            {
                "name": "selected",
                "type": "string",
                "required": False,
                "readable": True,
                "writable": True,
                "identity": False,
                "options": ["yes", "no"],
            }
        )
        validation = contract.get("validation") if isinstance(contract.get("validation"), dict) else {}
        validation["allowed_on_update"] = _normalize_unique_strings(list(validation.get("allowed_on_update") or []) + ["selected"])
        contract["validation"] = validation
        presentation = contract.get("presentation") if isinstance(contract.get("presentation"), dict) else {}
        presentation["default_list_fields"] = _normalize_unique_strings(list(presentation.get("default_list_fields") or []) + ["selected"])
        presentation["default_detail_fields"] = _normalize_unique_strings(list(presentation.get("default_detail_fields") or []) + ["selected"])
        contract["presentation"] = presentation
    return updated


def _build_policy_bundle(
    *,
    workspace_id: uuid.UUID,
    app_spec: dict[str, Any],
    raw_prompt: str,
) -> dict[str, Any]:
    app_slug = str(app_spec.get("app_slug") or "generated-app").strip() or "generated-app"
    app_title = str(app_spec.get("title") or app_slug).strip() or app_slug
    entity_contracts = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    sections = _extract_objective_sections(raw_prompt)
    families = {
        "validation_policies": [],
        "relation_constraints": [],
        "transition_policies": [],
        "invariant_policies": [],
        "derived_policies": [],
        "trigger_policies": [],
    }
    sequence = 1
    for section_name in ("behavior", "validation"):
        for statement in sections.get(section_name, []):
            text = str(statement or "").strip()
            if not text:
                continue
            family = _policy_family_from_statement(text)
            entry = {
                "id": f"{app_slug}-{sequence:03d}",
                "name": text[:96],
                "description": text,
                "family": family,
                "status": "documented",
                "enforcement_stage": "not_compiled",
                "targets": _policy_targets_from_statement(text, entity_contracts=[row for row in entity_contracts if isinstance(row, dict)]),
                "parameters": {},
                "source": {
                    "kind": "prompt_section",
                    "section": section_name,
                    "text": text,
                },
                "explanation": {
                    "user_summary": text,
                    "why_it_exists": f"Derived from the generated app request {section_name} section.",
                },
            }
            families[family].append(entry)
            sequence += 1

    compiled_relation_constraints, sequence = _compile_relation_constraint_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        start_sequence=sequence,
    )
    families["relation_constraints"].extend(compiled_relation_constraints)
    compiled_parent_status_gates, sequence = _compile_parent_status_gate_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["validation_policies"].extend(compiled_parent_status_gates)
    compiled_transition_policies, sequence = _compile_transition_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["transition_policies"].extend(compiled_transition_policies)
    compiled_derived_policies, sequence = _compile_related_count_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["derived_policies"].extend(compiled_derived_policies)
    compiled_trigger_policies, sequence = _compile_trigger_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["trigger_policies"].extend(compiled_trigger_policies)
    compiled_invariant_policies, sequence = _compile_parent_scoped_uniqueness_invariants(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["invariant_policies"].extend(compiled_invariant_policies)
    compiled_minimum_invariants, sequence = _compile_parent_scoped_minimum_selection_invariants(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["invariant_policies"].extend(compiled_minimum_invariants)

    future_capabilities = [
        "render_policy_bundle",
        "validate_policy_bundle",
        "compile_policy_bundle",
        "simulate_policy_bundle",
        "explain_policy_bundle",
    ]
    return {
        "schema_version": "xyn.policy_bundle.v0",
        "bundle_id": _policy_bundle_slug(app_slug),
        "app_slug": app_slug,
        "workspace_id": str(workspace_id),
        "title": f"{app_title} Policy Bundle",
        "description": "Prompt-derived business policy bundle for the generated application. This artifact is durable and inspectable. A narrow generic subset is compiled into runtime enforcement, while unsupported families remain documented-only.",
        "scope": {
            "artifact_slug": _generated_artifact_slug(app_slug),
            "applies_to": ["generated_runtime", "palette", "future_editor", "future_validator"],
        },
        "ownership": {
            "owner_kind": "generated_application",
            "editable": True,
            "source": "generated_from_prompt",
        },
        "policy_families": [key for key, rows in families.items() if rows] or list(families.keys()),
        "policies": families,
        "configurable_parameters": [],
        "explanation": {
            "summary": "Policy bundle scaffolds business-rule intent separately from entity contracts so rendering, editing, validation, and runtime enforcement can target the same durable artifact. The current runtime slice compiles relation constraints, status-based write gates, transition guards, parent-scoped selection invariants (at-most-one plus optional-gated at-least-one), related-count projections, and simple post-write related updates.",
            "coverage": {
                "documented_policy_count": sum(len(rows) for rows in families.values()),
                "compiled_policy_count": sum(
                    1
                    for rows in families.values()
                    for item in rows
                    if isinstance(item, dict) and str(item.get("enforcement_stage") or "").strip() == "runtime_enforced"
                ),
                "entity_contract_count": len(entity_contracts),
            },
            "future_capabilities": future_capabilities,
        },
    }


def _title_case_words(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[\s_]+", str(value or "").strip()) if part)


def _pluralize_label(value: str) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    if not lower:
        return "records"
    if lower.endswith("y") and lower[-2:] not in {"ay", "ey", "iy", "oy", "uy"}:
        return f"{text[:-1]}ies"
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return f"{text}es"
    return f"{text}s"


def _extract_objective_sections(objective: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {
        "core_entities": [],
        "behavior": [],
        "views": [],
        "validation": [],
    }
    text = re.sub(r"\s+", " ", str(objective or "")).strip()
    if not text:
        return sections
    section_patterns = {
        "core_entities": re.compile(
            r"core entities\s*:\s*(.*?)(?=\bbehavior\s*:|\bviews\s*/\s*usability\s*:|\bviews\s*:|\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "behavior": re.compile(
            r"behavior\s*:\s*(.*?)(?=\bviews\s*/\s*usability\s*:|\bviews\s*:|\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "views": re.compile(
            r"(?:views\s*/\s*usability|views)\s*:\s*(.*?)(?=\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "validation": re.compile(r"(?:validation\s*/\s*rules|validation)\s*:\s*(.*)$", re.IGNORECASE),
    }
    for section_name, pattern in section_patterns.items():
        match = pattern.search(text)
        if not match:
            continue
        body = str(match.group(1) or "").strip()
        if not body:
            continue
        if section_name == "core_entities":
            sections[section_name].extend(part.strip() for part in re.split(r"\s+(?=\d+\.)", body) if part.strip())
            continue
        sections[section_name].extend(
            re.sub(r"^\s*[-*]\s*", "", part).strip()
            for part in re.split(r"\s+-\s+", body)
            if re.sub(r"^\s*[-*]\s*", "", part).strip()
        )
    return sections


def _extract_app_name_from_prompt(raw_prompt: str, *, fallback: str) -> str:
    text = str(raw_prompt or "").strip()
    patterns = [
        re.compile(r'called\s+[“"]([^”"]+)[”"]', re.IGNORECASE),
        re.compile(r'build\s+(?:a|an)\s+.*?\s+called\s+([^.;]+)', re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value = str(match.group(1) or "").strip().strip(".")
            if value:
                return value
    return str(fallback or "").strip() or "Generated App"


def _extract_objective_entities(raw_prompt: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _extract_objective_sections(raw_prompt).get("core_entities", []):
        cleaned_line = re.sub(r"^\s*\d+\.\s*", "", line).strip()
        if not cleaned_line:
            continue
        parts = [part.strip() for part in re.split(r"\s+-\s+", cleaned_line) if part.strip()]
        if not parts:
            continue
        label = _title_case_words(parts[0])
        fields = [part.strip() for part in parts[1:] if part.strip()]
        if label:
            rows.append({"label": label, "fields": fields})
    return rows


def _field_options_from_token(token: str) -> list[str]:
    match = re.search(r"\(([^)]+)\)", token)
    if not match:
        return []
    return _normalize_unique_strings(
        part.strip()
        for part in re.split(r"[,/]|\\bor\\b", str(match.group(1) or ""), flags=re.IGNORECASE)
    )


def _sanitize_field_label(token: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", "", str(token or "")).strip()
    return cleaned


def _field_key(token: str) -> str:
    return _safe_slug(str(token or "").replace("/", " ").replace("-", " "), default="field").replace("-", "_")


def _field_type_for_token(token: str, *, options: list[str]) -> str:
    key = _field_key(token)
    if key.endswith("_id"):
        return "uuid"
    if key in {"created_at", "updated_at", "poll_date", "date"}:
        return "datetime" if key in {"created_at", "updated_at"} else "string"
    if options and {item.casefold() for item in options} <= {"yes", "no", "true", "false"}:
        return "string"
    return "string"


def _build_entity_contracts_from_prompt(raw_prompt: str) -> list[dict[str, Any]]:
    entity_rows = _extract_objective_entities(raw_prompt)
    if not entity_rows:
        return []

    singular_index: dict[str, tuple[str, str]] = {}
    for row in entity_rows:
        label = str(row["label"])
        singular_label = label.lower()
        plural_label = _pluralize_label(singular_label)
        entity_key = _safe_slug(plural_label, default="records").replace("-", "_")
        singular_index[_safe_slug(singular_label, default="record").replace("-", "_")] = (entity_key, singular_label)

    contracts: list[dict[str, Any]] = []
    for row in entity_rows:
        label = str(row["label"])
        singular_label = label.lower()
        plural_label = _pluralize_label(singular_label)
        entity_key = _safe_slug(plural_label, default="records").replace("-", "_")
        field_rows: list[dict[str, Any]] = [
            {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
            {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
        ]
        seen_field_names = {"id", "workspace_id"}
        relationships: list[dict[str, Any]] = []
        required_on_create: list[str] = ["workspace_id"]
        allowed_on_update: list[str] = []

        raw_fields = row.get("fields") if isinstance(row.get("fields"), list) else []
        for token in raw_fields:
            cleaned = _sanitize_field_label(str(token))
            options = _field_options_from_token(str(token))
            normalized = _field_key(cleaned)
            relation_target = singular_index.get(normalized)
            if relation_target:
                field_name = f"{normalized}_id"
                relation = {
                    "target_entity": relation_target[0],
                    "target_field": "id",
                    "relation_kind": "belongs_to",
                }
                field_rows.append(
                    {
                        "name": field_name,
                        "type": "uuid",
                        "required": True,
                        "readable": True,
                        "writable": True,
                        "identity": False,
                        "relation": relation,
                    }
                )
                seen_field_names.add(field_name)
                relationships.append(
                    {
                        "field": field_name,
                        "target_entity": relation_target[0],
                        "target_field": "id",
                        "relation_kind": "belongs_to",
                        "required": True,
                    }
                )
                required_on_create.append(field_name)
                allowed_on_update.append(field_name)
                continue

            field_name = normalized
            if field_name in seen_field_names:
                continue
            field: dict[str, Any] = {
                "name": field_name,
                "type": _field_type_for_token(field_name, options=options),
                "required": field_name not in {"notes"},
                "readable": True,
                "writable": field_name not in {"created_at", "updated_at"},
                "identity": field_name in {"name", "title", "voter_name"},
            }
            if options:
                field["options"] = options
            field_rows.append(field)
            seen_field_names.add(field_name)
            if field["writable"]:
                allowed_on_update.append(field_name)
            if field["required"] and field["writable"]:
                required_on_create.append(field_name)

        for standard_field in (
            {"name": "created_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
            {"name": "updated_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
        ):
            if standard_field["name"] in seen_field_names:
                continue
            field_rows.append(standard_field)
            seen_field_names.add(standard_field["name"])
        title_field = next(
            (
                candidate
                for candidate in ("title", "name", "voter_name")
                if any(str(field.get("name") or "") == candidate for field in field_rows)
            ),
            "id",
        )
        default_list_fields = [name for name in (title_field, "status", "poll_id", "lunch_option_id") if any(str(field.get("name") or "") == name for field in field_rows)]
        if not default_list_fields:
            default_list_fields = [str(field.get("name") or "") for field in field_rows if str(field.get("name") or "") not in {"id", "workspace_id", "created_at", "updated_at"}][:4]
        default_detail_fields = ["id", title_field]
        for name in [str(field.get("name") or "") for field in field_rows]:
            if name and name not in default_detail_fields and name not in {"updated_at"}:
                default_detail_fields.append(name)
        contracts.append(
            {
                "key": entity_key,
                "singular_label": singular_label,
                "plural_label": plural_label,
                "collection_path": f"/{entity_key}",
                "item_path_template": f"/{entity_key}" + "/{id}",
                "operations": {
                    "list": {"declared": True, "method": "GET", "path": f"/{entity_key}"},
                    "get": {"declared": True, "method": "GET", "path": f"/{entity_key}" + "/{id}"},
                    "create": {"declared": True, "method": "POST", "path": f"/{entity_key}"},
                    "update": {"declared": True, "method": "PATCH", "path": f"/{entity_key}" + "/{id}"},
                    "delete": {"declared": True, "method": "DELETE", "path": f"/{entity_key}" + "/{id}"},
                },
                "fields": field_rows,
                "presentation": {
                    "default_list_fields": _normalize_unique_strings(default_list_fields),
                    "default_detail_fields": _normalize_unique_strings(default_detail_fields),
                    "title_field": title_field,
                },
                "validation": {
                    "required_on_create": _normalize_unique_strings(required_on_create),
                    "allowed_on_update": _normalize_unique_strings(allowed_on_update),
                },
                "relationships": relationships,
            }
        )
    return _augment_contracts_with_inferred_selection_flags(raw_prompt=raw_prompt, contracts=contracts)


def _infer_entities_from_prompt(raw_prompt: str) -> list[str]:
    structured_contracts = _build_entity_contracts_from_prompt(raw_prompt)
    if structured_contracts:
        return [str(row.get("key") or "").strip() for row in structured_contracts if str(row.get("key") or "").strip()]
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
    contract_rows = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    contract_keys = _normalize_unique_strings(
        [str(row.get("key") or "").strip() for row in contract_rows if isinstance(row, dict)]
    )
    if contract_keys:
        return contract_keys
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
        "ingress": {"enabled": False},
        "data": {"postgres": {"required": True}},
        "reports": [],
    }

    extracted_title = _extract_app_name_from_prompt(raw_prompt, fallback=title or str(base_spec.get("title") or "Generated App"))
    app_slug = str(base_spec.get("app_slug") or "").strip() or (
        "net-inventory" if mentions_inventory else _safe_slug(extracted_title, default=_safe_slug(title, default="generated-app"))
    )
    app_title = str(extracted_title or title or base_spec.get("title") or "Generated App").strip() or "Generated App"
    db_name = _safe_slug(app_slug, default="generated-app").replace("-", "_")
    app_service_name = f"{app_slug}-api"
    db_service_name = f"{app_slug}-db"
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
    generated_contracts = _build_entity_contracts_from_prompt(raw_prompt)
    current_contracts = (
        copy.deepcopy(base_spec.get("entity_contracts"))
        if isinstance(base_spec.get("entity_contracts"), list)
        else []
    )
    entity_contracts = copy.deepcopy(current_contracts or generated_contracts)
    entities = _normalize_unique_strings(existing_entities + summary_entities + requested_entities + inferred_entities)
    if entity_contracts:
        contract_keys = _normalize_unique_strings(
            [str(row.get("key") or "").strip() for row in entity_contracts if isinstance(row, dict)]
        )
        entities = _normalize_unique_strings(contract_keys + entities)
    if not entities:
        raise RuntimeError(
            "AppSpec generation could not derive any entity contracts from the request. "
            "The generic builder must not silently fall back to inventory semantics."
        )

    existing_reports = _normalize_unique_strings(base_spec.get("reports") if isinstance(base_spec.get("reports"), list) else [])
    reports = existing_reports[:]
    visuals = _normalize_unique_strings(
        (
            _normalize_unique_strings(base_spec.get("requested_visuals") if isinstance(base_spec.get("requested_visuals"), list) else [])
            + requested_visuals
            + inferred_visuals
        )
    )
    if not entity_contracts and "devices" in entities and "devices_by_status_chart" not in visuals and "devices_by_status" not in reports:
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
    spec["purpose"] = str(raw_prompt or "").strip()
    spec["entities"] = entities
    spec["phase_1_scope"] = phase_1_scope
    spec["requested_visuals"] = visuals
    spec["reports"] = reports
    if entity_contracts:
        spec["entity_contracts"] = entity_contracts
    spec["services"] = [
        {
            "name": app_service_name,
            "image": _effective_net_inventory_image(),
            "env": {
                "PORT": "8080",
                "SERVICE_NAME": app_service_name,
                "APP_TITLE": app_title,
                "DATABASE_URL": f"postgresql://xyn:xyn_dev_password@{db_service_name}:5432/{db_name}",
            },
            "ports": [{"container": 8080, "host": 0, "protocol": "tcp"}],
            "depends_on": [db_service_name],
        },
        {
            "name": db_service_name,
            "image": "postgres:16-alpine",
            "env": {
                "POSTGRES_DB": db_name,
                "POSTGRES_USER": "xyn",
                "POSTGRES_PASSWORD": "xyn_dev_password",
            },
            "ports": [{"container": 5432, "host": 0, "protocol": "tcp"}],
            "depends_on": [],
        },
    ]
    spec.setdefault("data", {})
    if not isinstance(spec.get("data"), dict):
        spec["data"] = {}
    spec["data"].setdefault("postgres", {})
    if not isinstance(spec["data"].get("postgres"), dict):
        spec["data"]["postgres"] = {}
    spec["data"]["postgres"]["required"] = True
    spec["data"]["postgres"]["service"] = db_service_name
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
    policy_bundle: dict[str, Any] | None = None,
    deployment_dir: Path,
    compose_project: str,
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
) -> Path:
    services = [row for row in app_spec.get("services", []) if isinstance(row, dict)]
    db_service = next((row for row in services if "postgres" in str(row.get("image") or "").lower()), {})
    app_service = next((row for row in services if row is not db_service), {})
    app_service_name = str(app_service.get("name") or "generated-app-api").strip() or "generated-app-api"
    db_service_name = str(db_service.get("name") or "generated-app-db").strip() or "generated-app-db"
    db_env = db_service.get("env") if isinstance(db_service.get("env"), dict) else {}
    app_env = app_service.get("env") if isinstance(app_service.get("env"), dict) else {}
    db_name = str(db_env.get("POSTGRES_DB") or "generated_app").strip() or "generated_app"
    db_user = str(db_env.get("POSTGRES_USER") or "xyn").strip() or "xyn"
    db_password = str(db_env.get("POSTGRES_PASSWORD") or "xyn_dev_password").strip() or "xyn_dev_password"
    entity_contracts_json = json.dumps(
        build_resolved_capability_manifest(app_spec).get("entities") or [],
        separators=(",", ":"),
        sort_keys=True,
    ).replace("'", "''")
    policy_bundle_json = json.dumps(policy_bundle or {}, separators=(",", ":"), sort_keys=True).replace("'", "''")
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
                f"  {db_service_name}:",
                f"    image: {db_service.get('image') or 'postgres:16-alpine'}",
                f"    container_name: {compose_project}-db",
                "    restart: unless-stopped",
                "    environment:",
                f"      POSTGRES_DB: \"{db_name}\"",
                f"      POSTGRES_USER: \"{db_user}\"",
                f"      POSTGRES_PASSWORD: \"{db_password}\"",
                "    healthcheck:",
                f"      test: [\"CMD-SHELL\", \"pg_isready -U {db_user} -d {db_name}\"]",
                "      interval: 5s",
                "      timeout: 5s",
                "      retries: 20",
                "",
                f"  {app_service_name}:",
                f"    image: {app_image}",
                f"    container_name: {compose_project}-api",
                "    restart: unless-stopped",
                "    environment:",
                f"      PORT: \"{str(app_env.get('PORT') or '8080')}\"",
                f"      SERVICE_NAME: \"{str(app_env.get('SERVICE_NAME') or app_service_name)}\"",
                f"      APP_TITLE: \"{str(app_env.get('APP_TITLE') or app_spec.get('title') or app_service_name)}\"",
                f"      DATABASE_URL: \"{str(app_env.get('DATABASE_URL') or f'postgresql://{db_user}:{db_password}@{db_service_name}:5432/{db_name}')}\"",
                f"      GENERATED_ENTITY_CONTRACTS_JSON: '{entity_contracts_json}'",
                f"      GENERATED_POLICY_BUNDLE_JSON: '{policy_bundle_json}'",
                "      GENERATED_ENTITY_CONTRACTS_ALLOW_DEFAULTS: \"0\"",
                "    ports:",
                *app_ports,
                *app_network_lines,
                "    depends_on:",
                f"      {db_service_name}:",
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
    policy_bundle: dict[str, Any] | None,
    deployment_dir: Path,
    compose_project: str,
    logs: list[str],
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
) -> dict[str, Any]:
    compose_path = _materialize_net_inventory_compose(
        app_spec=app_spec,
        policy_bundle=policy_bundle,
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
            "The prompt requests a generated application contract that must remain faithful to the user's described domain.",
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

    policy_bundle = _build_policy_bundle(
        workspace_id=job.workspace_id,
        app_spec=app_spec,
        raw_prompt=raw_prompt,
    )
    try:
        validate(instance=policy_bundle, schema=_load_policy_bundle_schema())
    except ValidationError as exc:
        raise RuntimeError(f"Policy bundle validation failed: {exc.message}") from exc

    artifact_id = _persist_json_artifact(
        db,
        workspace_id=job.workspace_id,
        name=f"appspec.{app_spec['app_slug']}",
        kind="app_spec",
        payload=app_spec,
        metadata={"job_id": str(job.id)},
    )
    _append_job_log(logs, f"Persisted AppSpec artifact: {artifact_id}")
    policy_bundle_artifact_id = _persist_json_artifact(
        db,
        workspace_id=job.workspace_id,
        name=_policy_bundle_slug(str(app_spec.get("app_slug") or "generated-app")),
        kind="policy_bundle",
        payload=policy_bundle,
        metadata={"job_id": str(job.id), "app_spec_artifact_id": artifact_id},
    )
    _append_job_log(logs, f"Persisted policy bundle artifact: {policy_bundle_artifact_id}")

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
        "policy_bundle_artifact_id": policy_bundle_artifact_id,
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
        policy_bundle=policy_bundle,
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
            "Policy bundle validated against xyn.policy_bundle.v0 schema.",
            f"AppSpec artifact persisted: {artifact_id}.",
            f"Policy bundle artifact persisted: {policy_bundle_artifact_id}.",
            f"Generated artifact package created: {packaged_artifact['artifact_slug']}@{packaged_artifact['artifact_version']}.",
            (
                f"Generated artifact imported into registry: {packaged_artifact['artifact_slug']}"
                if registry_artifact
                else f"Generated artifact registry import deferred: {registry_import_error or 'unknown error'}."
            ),
        ],
        related_artifact_ids=[artifact_id, policy_bundle_artifact_id],
        extra_metadata_updates={"app_spec_artifact_id": artifact_id, "policy_bundle_artifact_id": policy_bundle_artifact_id},
    )
    follow_up = [
        {
            "type": "deploy_app_local",
            "input_json": {
                "app_spec": app_spec,
                "policy_bundle": policy_bundle,
                "app_spec_artifact_id": artifact_id,
                "policy_bundle_artifact_id": policy_bundle_artifact_id,
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
            "policy_bundle": policy_bundle,
            "app_spec_artifact_id": artifact_id,
            "policy_bundle_artifact_id": policy_bundle_artifact_id,
            "app_spec_schema": "xyn.appspec.v0",
            "policy_bundle_schema": "xyn.policy_bundle.v0",
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
    policy_bundle = payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {}
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "net-inventory"), default="net-inventory")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    deployment_dir = _deployments_root() / app_slug / stamp
    deployment_dir.mkdir(parents=True, exist_ok=True)
    compose_project = _safe_slug(f"xyn-app-{app_slug}", default="xyn-app")
    app_output = {
        "app_slug": app_slug,
        **_deploy_generated_runtime(
            app_spec=app_spec,
            policy_bundle=policy_bundle,
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
                "policy_bundle": policy_bundle,
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
                    prefer_local_images=_prefer_local_platform_images_for_smoke(),
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
        policy_bundle=payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {},
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
                "policy_bundle": payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {},
                "generated_artifact": generated_artifact,
                "execution_note_artifact_id": execution_note_artifact_id,
                "source_job_id": str(job.id),
            },
        }
    ]
    return sibling_output, follow_up


def _field_map_from_contract(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = contract.get("fields") if isinstance(contract.get("fields"), list) else []
    return {
        str(row.get("name") or "").strip(): row
        for row in rows
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    }


def _extract_items_from_response(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return [row for row in body.get("items") if isinstance(row, dict)]
    return []


def _sample_field_value(
    *,
    contract: dict[str, Any],
    field: dict[str, Any],
    workspace_id: str,
    created_records: dict[str, dict[str, Any]],
) -> Any:
    field_name = str(field.get("name") or "").strip()
    relation = field.get("relation") if isinstance(field.get("relation"), dict) else None
    if field_name == "workspace_id":
        return workspace_id
    if relation:
        target_key = str(relation.get("target_entity") or "").strip()
        target = created_records.get(target_key)
        if not isinstance(target, dict):
            return None
        return str(target.get(relation.get("target_field") or "id") or "").strip() or None
    options = _normalize_unique_strings(field.get("options") if isinstance(field.get("options"), list) else [])
    if options:
        return options[0]
    field_type = str(field.get("type") or "string").strip().lower()
    singular = str(contract.get("singular_label") or contract.get("key") or "record").strip().replace(" ", "-")
    if field_name in {"title", "name"}:
        return f"{singular}-1"
    if field_name == "voter_name":
        return "alex"
    if field_name.endswith("_date") or field_name == "date":
        return "2026-03-17"
    if field_name in {"created_at", "updated_at"}:
        return None
    if field_type.startswith("bool"):
        return True
    return f"{singular}-{field_name}-1"


def _build_contract_seed_payload(
    *,
    contract: dict[str, Any],
    workspace_id: str,
    created_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    fields = _field_map_from_contract(contract)
    required = _normalize_unique_strings(
        (contract.get("validation") or {}).get("required_on_create")
        if isinstance(contract.get("validation"), dict)
        else []
    )
    payload: dict[str, Any] = {}
    for field_name in required:
        field = fields.get(field_name)
        if not isinstance(field, dict) or not bool(field.get("writable", False)):
            continue
        payload[field_name] = _sample_field_value(
            contract=contract,
            field=field,
            workspace_id=workspace_id,
            created_records=created_records,
        )
    for field_name, field in fields.items():
        if field_name in payload or not bool(field.get("writable", False)):
            continue
        if field_name in {"notes", "status", "active"}:
            payload[field_name] = _sample_field_value(
                contract=contract,
                field=field,
                workspace_id=workspace_id,
                created_records=created_records,
            )
    return {key: value for key, value in payload.items() if value is not None}


def _build_contract_update_payload(contract: dict[str, Any]) -> dict[str, Any]:
    fields = _field_map_from_contract(contract)
    allowed = _normalize_unique_strings(
        (contract.get("validation") or {}).get("allowed_on_update")
        if isinstance(contract.get("validation"), dict)
        else []
    )
    for field_name in allowed:
        field = fields.get(field_name)
        if not isinstance(field, dict):
            continue
        options = _normalize_unique_strings(field.get("options") if isinstance(field.get("options"), list) else [])
        if len(options) > 1:
            return {field_name: options[1]}
        if field_name in {"name", "title", "notes"}:
            return {field_name: f"updated-{field_name}"}
    return {}


def _policy_bundle_entries(policy_bundle: dict[str, Any], family: str) -> list[dict[str, Any]]:
    policies = policy_bundle.get("policies") if isinstance(policy_bundle.get("policies"), dict) else {}
    rows = policies.get(family) if isinstance(policies.get(family), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _compiled_runtime_policies(
    *,
    policy_bundle: dict[str, Any],
    family: str,
    runtime_rule: str,
    entity_key: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for policy in _policy_bundle_entries(policy_bundle, family):
        params = policy.get("parameters") if isinstance(policy.get("parameters"), dict) else {}
        if str(params.get("runtime_rule") or "").strip() != runtime_rule:
            continue
        if str(params.get("entity_key") or "").strip() != entity_key:
            continue
        matches.append(policy)
    return matches


def _allowed_transition_path(
    *,
    current_status: str,
    allowed_statuses: list[str],
    allowed_transitions: dict[str, list[str]],
) -> list[str] | None:
    current = str(current_status or "").strip()
    targets = {str(value).strip() for value in allowed_statuses if str(value).strip()}
    if not current or not targets:
        return None
    if current in targets:
        return []
    queue: deque[tuple[str, list[str]]] = deque([(current, [])])
    seen = {current}
    while queue:
        state, path = queue.popleft()
        for candidate in allowed_transitions.get(state, []):
            next_state = str(candidate or "").strip()
            if not next_state or next_state in seen:
                continue
            next_path = path + [next_state]
            if next_state in targets:
                return next_path
            seen.add(next_state)
            queue.append((next_state, next_path))
    return None


def _ensure_parent_status_gate_prerequisites(
    *,
    container_name: str,
    port: int,
    workspace_id: str,
    contract: dict[str, Any],
    entity_contracts: list[dict[str, Any]],
    created_records: dict[str, dict[str, Any]],
    policy_bundle: dict[str, Any],
) -> None:
    entity_key = str(contract.get("key") or "").strip()
    if not entity_key or not policy_bundle:
        return
    contracts = {
        str(item.get("key") or "").strip(): item
        for item in entity_contracts
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }
    gates = _compiled_runtime_policies(
        policy_bundle=policy_bundle,
        family="validation_policies",
        runtime_rule="parent_status_gate",
        entity_key=entity_key,
    )
    for gate in gates:
        params = gate.get("parameters") if isinstance(gate.get("parameters"), dict) else {}
        if "create" not in {str(value).strip() for value in params.get("on_operations") or [] if str(value).strip()}:
            continue
        parent_entity = str(params.get("parent_entity") or "").strip()
        parent_status_field = str(params.get("parent_status_field") or "").strip()
        allowed_statuses = [str(value).strip() for value in params.get("allowed_parent_statuses") or [] if str(value).strip()]
        if not parent_entity or not parent_status_field or not allowed_statuses:
            continue
        parent_contract = contracts.get(parent_entity)
        parent_record = created_records.get(parent_entity)
        if not isinstance(parent_contract, dict) or not isinstance(parent_record, dict):
            continue
        current_status = str(parent_record.get(parent_status_field) or "").strip()
        if current_status in set(allowed_statuses):
            continue
        transition_policy = next(
            (
                policy
                for policy in _compiled_runtime_policies(
                    policy_bundle=policy_bundle,
                    family="transition_policies",
                    runtime_rule="field_transition_guard",
                    entity_key=parent_entity,
                )
                if str(((policy.get("parameters") or {}).get("field_name")) or "").strip() == parent_status_field
            ),
            None,
        )
        transition_params = transition_policy.get("parameters") if isinstance((transition_policy or {}).get("parameters"), dict) else {}
        transition_path = _allowed_transition_path(
            current_status=current_status,
            allowed_statuses=allowed_statuses,
            allowed_transitions=transition_params.get("allowed_transitions") if isinstance(transition_params.get("allowed_transitions"), dict) else {},
        )
        if transition_path is None:
            transition_path = [allowed_statuses[0]]
        item_ref = str(parent_record.get("id") or "").strip()
        item_template = str(parent_contract.get("item_path_template") or f"/{parent_entity}" + "/{id}").strip()
        item_path = item_template.replace("{id}", item_ref)
        for next_status in transition_path:
            patch_code, patch_body, patch_text = _container_http_json(
                container_name,
                "PATCH",
                f"{item_path}?workspace_id={workspace_id}",
                port=port,
                payload={parent_status_field: next_status},
            )
            if patch_code != 200:
                raise RuntimeError(f"PATCH {item_path} failed ({patch_code}): {patch_text}")
            if isinstance(patch_body, dict):
                parent_record = patch_body
                created_records[parent_entity] = patch_body


def _exercise_runtime_contracts(
    *,
    container_name: str,
    port: int,
    workspace_id: str,
    entity_contracts: list[dict[str, Any]],
    policy_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    created_records: dict[str, dict[str, Any]] = {}
    pending = [row for row in entity_contracts if isinstance(row, dict)]
    while pending:
        progressed = False
        for contract in pending[:]:
            relationships = contract.get("relationships") if isinstance(contract.get("relationships"), list) else []
            deps = {
                str(rel.get("target_entity") or "").strip()
                for rel in relationships
                if isinstance(rel, dict) and str(rel.get("target_entity") or "").strip()
            }
            if any(dep not in created_records for dep in deps):
                continue
            entity_key = str(contract.get("key") or "").strip()
            collection_path = str(contract.get("collection_path") or f"/{entity_key}").strip()
            _ensure_parent_status_gate_prerequisites(
                container_name=container_name,
                port=port,
                workspace_id=workspace_id,
                contract=contract,
                entity_contracts=entity_contracts,
                created_records=created_records,
                policy_bundle=policy_bundle or {},
            )
            seed_payload = _build_contract_seed_payload(
                contract=contract,
                workspace_id=workspace_id,
                created_records=created_records,
            )
            create_code, create_body, create_text = _container_http_json(
                container_name,
                "POST",
                collection_path,
                port=port,
                payload=seed_payload,
            )
            if create_code not in {200, 201}:
                raise RuntimeError(f"POST {collection_path} failed ({create_code}): {create_text}")
            created_record = create_body if isinstance(create_body, dict) else {}
            created_records[entity_key] = created_record
            list_code, list_body, list_text = _container_http_json(
                container_name,
                "GET",
                f"{collection_path}?workspace_id={workspace_id}",
                port=port,
            )
            if list_code != 200:
                raise RuntimeError(f"GET {collection_path} failed ({list_code}): {list_text}")
            items = _extract_items_from_response(list_body)
            if not items:
                raise RuntimeError(f"GET {collection_path} returned no items after seeding {entity_key}")
            item_ref = str(created_record.get("id") or "").strip()
            item_path_template = str(contract.get("item_path_template") or f"{collection_path}" + "/{id}")
            item_path = item_path_template.replace("{id}", item_ref)
            get_code, get_body, get_text = _container_http_json(
                container_name,
                "GET",
                f"{item_path}?workspace_id={workspace_id}",
                port=port,
            )
            if get_code != 200:
                raise RuntimeError(f"GET {item_path} failed ({get_code}): {get_text}")
            update_payload = _build_contract_update_payload(contract)
            update_result: dict[str, Any] | None = None
            if update_payload:
                update_code, update_body, update_text = _container_http_json(
                    container_name,
                    "PATCH",
                    f"{item_path}?workspace_id={workspace_id}",
                    port=port,
                    payload=update_payload,
                )
                if update_code != 200:
                    raise RuntimeError(f"PATCH {item_path} failed ({update_code}): {update_text}")
                update_result = {"code": update_code, "body": update_body or update_text}
            results[entity_key] = {
                "seed_payload": seed_payload,
                "create": {"code": create_code, "body": create_body or create_text},
                "list": {"code": list_code, "body": list_body or list_text},
                "get": {"code": get_code, "body": get_body or get_text},
                "update": update_result,
            }
            pending.remove(contract)
            progressed = True
        if not progressed:
            unresolved = [str(row.get("key") or "").strip() for row in pending if isinstance(row, dict)]
            raise RuntimeError(f"Could not resolve seed order for generated entity contracts: {unresolved}")
    return results


def _handle_smoke_test(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = job.input_json or {}
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    deployment = payload.get("deployment") if isinstance(payload.get("deployment"), dict) else {}
    sibling = payload.get("sibling") if isinstance(payload.get("sibling"), dict) else {}
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    policy_bundle = payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {}
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
    entity_contracts = build_resolved_capability_manifest(app_spec).get("entities") if isinstance(build_resolved_capability_manifest(app_spec).get("entities"), list) else []
    if not entity_contracts:
        raise RuntimeError("Generated app contract smoke requires resolved entity contracts")
    local_contract_checks = _exercise_runtime_contracts(
        container_name=app_container_name,
        port=8080,
        workspace_id=str(job.workspace_id),
        entity_contracts=entity_contracts,
        policy_bundle=policy_bundle,
    )

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
    sibling_runtime_public_url = str(
        sibling_runtime.get("public_app_url") or sibling_runtime.get("app_url") or sibling.get("ui_url") or ""
    ).strip()
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
    sibling_contract_checks = _exercise_runtime_contracts(
        container_name=sibling_runtime_container,
        port=8080,
        workspace_id=sibling_workspace_id,
        entity_contracts=entity_contracts,
        policy_bundle=policy_bundle,
    )

    manifest = build_resolved_capability_manifest(app_spec)
    list_commands = [
        row
        for row in (manifest.get("commands") if isinstance(manifest.get("commands"), list) else [])
        if isinstance(row, dict) and str(row.get("operation_kind") or "") == "list"
    ]
    if not list_commands:
        raise RuntimeError("Generated app contract smoke requires at least one declared list command")
    primary_list_command = list_commands[0]
    palette_prompt = str(primary_list_command.get("prompt") or "").strip()
    palette_status, palette_result, palette_text = _execute_sibling_palette_prompt(
        sibling_api_container=sibling_api_container,
        workspace_slug=sibling_workspace_slug,
        prompt=palette_prompt,
    )
    if palette_status != 200:
        raise RuntimeError(f"Sibling palette request failed ({palette_status}): {palette_text}")
    if palette_result.get("kind") != "table":
        raise RuntimeError(f"Palette did not return table: {palette_result}")
    if not isinstance(palette_result.get("rows"), list) or not palette_result.get("rows"):
        raise RuntimeError(f"Palette {palette_prompt} returned no rows")
    palette_meta = palette_result.get("meta") if isinstance(palette_result.get("meta"), dict) else {}
    palette_base_url = str(palette_meta.get("base_url") or "").strip()
    allowed_runtime_urls = {value for value in (sibling_runtime_base_url, sibling_runtime_public_url) if value}
    if allowed_runtime_urls and palette_base_url not in allowed_runtime_urls:
        raise RuntimeError(
            "Sibling palette targeted unexpected runtime base URL: "
            f"{palette_meta.get('base_url')} not in {sorted(allowed_runtime_urls)}"
        )
    _append_job_log(logs, f"Palette check returned {len(palette_result.get('rows') or [])} rows for {palette_prompt}")

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
                prompt=palette_prompt,
            )
            if palette_after_stop_status != 200:
                raise RuntimeError(
                    f"Sibling palette after root stop failed ({palette_after_stop_status}): {palette_after_stop_text}"
                )
            if not isinstance(palette_after_stop_result.get("rows"), list) or not palette_after_stop_result.get("rows"):
                raise RuntimeError("Sibling palette after root stop returned no rows")
            after_stop_meta = palette_after_stop_result.get("meta") if isinstance(palette_after_stop_result.get("meta"), dict) else {}
            after_stop_base_url = str(after_stop_meta.get("base_url") or "").strip()
            if allowed_runtime_urls and after_stop_base_url not in allowed_runtime_urls:
                raise RuntimeError(
                    "Sibling palette after root stop targeted unexpected runtime base URL: "
                    f"{after_stop_meta.get('base_url')} not in {sorted(allowed_runtime_urls)}"
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
            implementation_summary="Completed platform plumbing checks and generated-application contract smoke for the deployed application.",
            append_validation=[
                "Platform plumbing smoke passed: local runtime, sibling API, sibling runtime, and artifact install all became reachable.",
                f"Generated app contract smoke passed for entities: {', '.join(str(row.get('key') or '') for row in entity_contracts)}.",
                f"Palette returned {len(palette_result.get('rows') or [])} rows for {palette_prompt}.",
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
            "platform_plumbing": {
                "app_health": {"code": health_code, "body": health_body or health_text},
                "sibling_health": {"code": sibling_health_code, "body": sibling_health_body or sibling_health_text},
                "sibling_runtime": {
                    "base_url": sibling_runtime_base_url,
                    "health": {"code": sibling_runtime_health_code, "body": sibling_runtime_health_body or sibling_runtime_health_text},
                },
                "generated_artifact": {
                    "registry_catalog": registry_catalog,
                    "installed_in_sibling": generated_artifact_slug,
                    "installed_version": generated_artifact_version,
                },
            },
            "generated_app_contract_smoke": {
                "local_runtime": local_contract_checks,
                "sibling_runtime": sibling_contract_checks,
                "palette": {"prompt": palette_prompt, "result": palette_result},
            },
            "sibling_xyn": sibling,
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
