"""Phase-2 app-intent pipeline worker.

Executes queued jobs:
- generate_app_spec
- deploy_app_local
- provision_sibling_xyn
- smoke_test
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from jsonschema import ValidationError, validate
from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.context_packs import default_instance_workspace_root
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


def _build_app_spec(*, workspace_id: uuid.UUID, title: str, raw_prompt: str) -> dict[str, Any]:
    prompt = raw_prompt.lower()
    mentions_inventory = any(token in prompt for token in ("inventory", "device", "devices", "network"))
    app_slug = "net-inventory" if mentions_inventory else _safe_slug(title, default="net-inventory")
    requires_primitives: list[str] = []
    if any(token in prompt for token in ("location", "address", "site", "rack", "closet", "building", "room")):
        requires_primitives.append("location")

    spec = {
        "schema_version": "xyn.appspec.v0",
        "app_slug": app_slug,
        "title": title or "Network Inventory",
        "workspace_id": str(workspace_id),
        "services": [
            {
                "name": "net-inventory-api",
                "image": NET_INVENTORY_IMAGE,
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
        "source_prompt": raw_prompt,
    }
    if requires_primitives:
        spec["requires_primitives"] = requires_primitives
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


def _materialize_net_inventory_compose(*, app_spec: dict[str, Any], deployment_dir: Path, compose_project: str) -> Path:
    app_service = next((row for row in app_spec.get("services", []) if row.get("name") == "net-inventory-api"), {})
    db_service = next((row for row in app_spec.get("services", []) if row.get("name") == "net-inventory-db"), {})
    app_image = str(app_service.get("image") or NET_INVENTORY_IMAGE)
    app_ports = _ports_yaml(list(app_service.get("ports") or [{"host": 0, "container": 8080, "protocol": "tcp"}]))
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
                "    ports:",
                *app_ports,
                "    depends_on:",
                "      net-inventory-db:",
                "        condition: service_healthy",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return compose


def _handle_generate_app_spec(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = job.input_json or {}
    title = str(payload.get("title") or "Network Inventory").strip() or "Network Inventory"
    content = payload.get("content_json") if isinstance(payload.get("content_json"), dict) else {}
    raw_prompt = str(content.get("raw_prompt") or payload.get("raw_prompt") or title).strip()
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

    app_spec = _build_app_spec(workspace_id=job.workspace_id, title=title, raw_prompt=raw_prompt)
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
    update_execution_note(
        db,
        artifact_id=note.id,
        implementation_summary="Generated and validated AppSpec, then persisted it as an instance-local artifact for downstream deployment.",
        validation_summary=[
            "Primitive catalog loaded successfully.",
            "AppSpec validated against xyn.appspec.v0 schema.",
            f"AppSpec artifact persisted: {artifact_id}.",
        ],
        related_artifact_ids=[artifact_id],
        extra_metadata_updates={"app_spec_artifact_id": artifact_id},
    )

    selected_images = {svc.get("name"): svc.get("image") for svc in app_spec.get("services", []) if isinstance(svc, dict)}
    selected_ports = {
        svc.get("name"): svc.get("ports")
        for svc in app_spec.get("services", [])
        if isinstance(svc, dict)
    }
    follow_up = [
        {
            "type": "deploy_app_local",
            "input_json": {
                "app_spec": app_spec,
                "app_spec_artifact_id": artifact_id,
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
            "execution_note_artifact_id": str(note.id),
        },
        follow_up,
    )


def _handle_deploy_app_local(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = job.input_json or {}
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "net-inventory"), default="net-inventory")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    deployment_dir = _deployments_root() / app_slug / stamp
    deployment_dir.mkdir(parents=True, exist_ok=True)
    compose_project = _safe_slug(f"xyn-app-{app_slug}", default="xyn-app")
    compose_path = _materialize_net_inventory_compose(app_spec=app_spec, deployment_dir=deployment_dir, compose_project=compose_project)
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
    app_output = {
        "app_slug": app_slug,
        "compose_project": compose_project,
        "deployment_dir": str(deployment_dir),
        "compose_path": str(compose_path),
        "app_container_name": f"{compose_project}-api",
        "app_url": f"http://localhost:{app_port}",
        "ports": {"app_tcp": app_port},
    }
    if execution_note_artifact_id:
        update_execution_note(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Materialized local docker-compose deployment for the generated app and resolved a running app URL.",
            append_validation=[
                f"Compose written: {compose_path}",
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
    workspace = db.query(Workspace).filter(Workspace.id == job.workspace_id).first()
    workspace_slug = str(getattr(workspace, "slug", "default") or "default")
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
    if execution_note_artifact_id:
        update_execution_note(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Provisioned a sibling Xyn instance as the next validation environment for the generated application.",
            append_validation=[
                f"Sibling Xyn provisioned with ui_url={sibling_output.get('ui_url')}",
                f"Sibling Xyn provisioned with api_url={sibling_output.get('api_url')}",
            ],
            extra_metadata_updates={
                "sibling_ui_url": sibling_output.get("ui_url"),
                "sibling_api_url": sibling_output.get("api_url"),
            },
        )
    follow_up = [
        {
            "type": "smoke_test",
            "input_json": {
                "deployment": deployment,
                "sibling": sibling_output,
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

    palette_result = execute_palette_prompt(
        db,
        prompt="show devices",
        workspace_id=job.workspace_id,
        workspace_slug=workspace_slug,
    )
    if palette_result.get("kind") != "table":
        raise RuntimeError(f"Palette did not return table: {palette_result}")
    if not isinstance(palette_result.get("rows"), list) or not palette_result.get("rows"):
        raise RuntimeError("Palette show devices returned no rows")
    _append_job_log(logs, f"Palette check returned {len(palette_result.get('rows') or [])} rows")
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
                f"Palette returned {len(palette_result.get('rows') or [])} rows for show devices.",
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
            "sibling_xyn": sibling,
            "palette": palette_result,
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
