"""Release plan/apply/status endpoints."""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Header, Response
from jsonschema import validate, ValidationError
from pydantic import BaseModel

from core.releases.compiler import compile_release_to_runtime
from core.releases.compose_renderer import render_compose
from core.releases.k8s_backend import validate_runtime_spec, K8sValidationError
from core.releases import store
from core.artifact_registry import apply_release_spec_artifact_resolution
from core.database import SessionLocal
from core import schemas

router = APIRouter()


def _compose_base_cmd() -> list[str]:
    override = os.environ.get("XYNSEED_COMPOSE_BIN", "").strip()
    if override:
        return override.split()
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return ["docker", "compose"]


def _require_auth(authorization: str | None):
    token = os.environ.get("XYNSEED_API_TOKEN", "").strip()
    if not token:
        return
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization token")
    expected = f"Bearer {token}"
    if authorization.strip() != expected:
        raise HTTPException(status_code=403, detail="Invalid authorization token")


class PlanRequest(BaseModel):
    release_spec: Dict[str, Any]


class ApplyRequest(BaseModel):
    release_id: str
    plan_id: str


class RestartPlanRequest(BaseModel):
    serviceName: str


class DestroyPlanRequest(BaseModel):
    removeVolumes: bool = False


class ApplyPlanRequest(BaseModel):
    plan_id: str


def _load_contract_schema_json(name: str) -> Dict[str, Any]:
    import json
    schema_path = store.contracts_root() / "schemas" / name
    return json.loads(schema_path.read_text())


def _validate_schema(instance: Dict[str, Any], schema_name: str) -> None:
    schema = _load_contract_schema_json(schema_name)
    validate(instance=instance, schema=schema)


def _index_by_name(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {item.get("name"): item for item in items if item.get("name")}


def _diff_actions(
    previous: Optional[Dict[str, Any]],
    current: Dict[str, Any]
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    if previous is None:
        for target_type in ("deployment", "service", "volume", "network", "route"):
            for item in current.get(f"{target_type}s", []):
                name = item.get("name")
                if not name:
                    continue
                actions.append({
                    "op": "create",
                    "targetType": target_type,
                    "targetName": name,
                    "details": {}
                })
        return sorted(actions, key=lambda a: (a["targetType"], a["targetName"]))

    for target_type in ("deployment", "service", "volume", "network", "route"):
        prev_items = _index_by_name(previous.get(f"{target_type}s", []))
        curr_items = _index_by_name(current.get(f"{target_type}s", []))

        for name, curr_item in curr_items.items():
            prev_item = prev_items.get(name)
            if prev_item is None:
                actions.append({
                    "op": "create",
                    "targetType": target_type,
                    "targetName": name,
                    "details": {}
                })
            elif prev_item != curr_item:
                actions.append({
                    "op": "update",
                    "targetType": target_type,
                    "targetName": name,
                    "details": {
                        "previous": prev_item,
                        "current": curr_item
                    }
                })

        for name in prev_items.keys():
            if name not in curr_items:
                actions.append({
                    "op": "delete",
                    "targetType": target_type,
                    "targetName": name,
                    "details": {}
                })

    return sorted(actions, key=lambda a: (a["targetType"], a["targetName"]))


def _release_plan_artifacts(release_id: str) -> Dict[str, str]:
    artifacts = store.latest_artifacts(release_id)
    if not artifacts:
        raise HTTPException(status_code=404, detail="Release artifacts not found")
    return artifacts


def _load_latest_runtime_or_404(release_id: str) -> Dict[str, Any]:
    runtime = store.load_latest_runtime(release_id)
    if not runtime:
        raise HTTPException(status_code=404, detail="Release not found")
    return runtime


def _build_action_plan(
    release_id: str,
    action_kind: str,
    target_name: str,
    options: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    revision = store.latest_revision(release_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="Release not found")
    artifacts = _release_plan_artifacts(release_id)
    plan_id = str(uuid.uuid4())
    plan = {
        "planId": plan_id,
        "releaseId": release_id,
        "revisionFrom": revision,
        "revisionTo": revision,
        "summary": f"{action_kind} {target_name}",
        "actions": [
            {
                "op": "update",
                "targetType": "release",
                "targetName": target_name,
                "details": {"action": action_kind}
            }
        ],
        "artifacts": artifacts,
        "actionKind": action_kind,
        "options": options or {}
    }
    store.save_plan(release_id, plan_id, plan)
    return plan


def _compose_env(runtime_spec: Dict[str, Any]) -> Dict[str, str]:
    project_name = f"{runtime_spec['metadata']['namespace']}_{runtime_spec['metadata']['name']}"
    return {"COMPOSE_PROJECT_NAME": project_name, **dict(os.environ)}


def _compose_apply(compose_path: str | os.PathLike, command: List[str], env: Dict[str, str]) -> tuple[int, str, str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(Path(compose_path).parent),
        env=env
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _apply_compose_action(
    runtime_spec: Dict[str, Any],
    compose_path: str,
    action_kind: str,
    options: Dict[str, Any]
) -> tuple[str, str, Dict[str, str]]:
    compose_cmd = _compose_base_cmd()
    env = _compose_env(runtime_spec)
    command = [*compose_cmd, "-f", str(compose_path)]
    if action_kind == "stop":
        command += ["stop"]
    elif action_kind == "restart":
        service_name = options.get("serviceName")
        if not service_name:
            return "failed", "serviceName is required", {}
        command += ["restart", service_name]
    elif action_kind == "destroy":
        command += ["down"]
        if options.get("removeVolumes"):
            command += ["--volumes"]
    else:
        return "failed", f"Unsupported action '{action_kind}'", {}

    returncode, stdout, stderr = _compose_apply(compose_path, command, env)
    artifacts: Dict[str, str] = {}
    if stdout:
        artifacts["stdoutArtifactId"] = store.save_text_artifact(stdout, ".log")
    if stderr:
        artifacts["stderrArtifactId"] = store.save_text_artifact(stderr, ".log")

    if returncode != 0:
        return "failed", stderr or "compose action failed", artifacts
    return "succeeded", stdout or f"{action_kind} succeeded", artifacts

@router.post("/releases/plan", response_model=schemas.ReleasePlan)
async def plan_release(request: PlanRequest, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    release_spec = request.release_spec

    try:
        _validate_schema(release_spec, "ReleaseSpec.schema.json")
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"ReleaseSpec validation error: {exc.message}")

    db = SessionLocal()
    try:
        release_spec = apply_release_spec_artifact_resolution(db, release_spec, workspace_slug="default")
    finally:
        db.close()

    metadata = release_spec["metadata"]
    release_id = f"{metadata['namespace']}.{metadata['name']}"
    revision_from = store.latest_revision(release_id)
    revision_to = (revision_from or 0) + 1

    runtime_spec = compile_release_to_runtime(release_spec, revision=revision_to, release_id=release_id)
    try:
        _validate_schema(runtime_spec, "RuntimeSpec.schema.json")
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"RuntimeSpec validation error: {exc.message}")

    compose_yaml = None
    backend_type = runtime_spec["release"]["backend"]["type"]
    if backend_type == "compose":
        compose_yaml = render_compose(runtime_spec)
    elif backend_type == "k8s":
        try:
            validate_runtime_spec(runtime_spec)
        except K8sValidationError as exc:
            raise HTTPException(status_code=400, detail=f"K8s validation error: {exc}")

    artifacts = store.save_revision_artifacts(
        release_id,
        revision_to,
        release_spec,
        runtime_spec,
        compose_yaml
    )

    previous_runtime = None
    if revision_from is not None:
        previous_runtime = store.load_runtime_revision(release_id, revision_from)
    actions = _diff_actions(previous_runtime, runtime_spec)
    plan_id = str(uuid.uuid4())
    plan = {
        "planId": plan_id,
        "releaseId": release_id,
        "revisionFrom": revision_from,
        "revisionTo": revision_to,
        "summary": f"{len(actions)} actions",
        "actions": actions,
        "artifacts": artifacts
    }
    store.save_plan(release_id, plan_id, plan)
    return plan


@router.post("/releases/{release_id}/plan/stop", response_model=schemas.ReleasePlan)
async def plan_stop_release(release_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    _load_latest_runtime_or_404(release_id)
    return _build_action_plan(release_id, "stop", release_id)


@router.post("/releases/{release_id}/plan/restart", response_model=schemas.ReleasePlan)
async def plan_restart_release(
    release_id: str,
    request: RestartPlanRequest,
    authorization: str | None = Header(default=None)
):
    _require_auth(authorization)
    runtime_spec = _load_latest_runtime_or_404(release_id)
    service_names = [item["name"] for item in runtime_spec.get("deployments", []) if item.get("name")]
    if request.serviceName not in service_names:
        raise HTTPException(status_code=400, detail="Unknown service name")
    return _build_action_plan(
        release_id,
        "restart",
        request.serviceName,
        options={"serviceName": request.serviceName}
    )


@router.post("/releases/{release_id}/plan/destroy", response_model=schemas.ReleasePlan)
async def plan_destroy_release(
    release_id: str,
    request: DestroyPlanRequest,
    authorization: str | None = Header(default=None)
):
    _require_auth(authorization)
    _load_latest_runtime_or_404(release_id)
    return _build_action_plan(
        release_id,
        "destroy",
        release_id,
        options={"removeVolumes": request.removeVolumes}
    )


@router.get("/releases", response_model=List[schemas.ReleaseSummary])
async def list_releases(authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    return store.list_release_summaries()


@router.get("/releases/{release_id}", response_model=schemas.ReleaseDetails)
async def get_release(release_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    runtime_spec = store.load_latest_runtime(release_id)
    release_spec = store.load_latest_release_spec(release_id)
    artifacts = store.latest_artifacts(release_id)
    if not runtime_spec or not release_spec or not artifacts:
        raise HTTPException(status_code=404, detail="Release not found")
    revision = runtime_spec["release"]["revision"]
    backend = runtime_spec["release"]["backend"]["type"]
    runtime_path = artifacts.get("runtimeSpecPath", "")
    return {
        "releaseId": release_id,
        "name": runtime_spec["metadata"]["name"],
        "namespace": runtime_spec["metadata"]["namespace"],
        "revision": revision,
        "backend": backend,
        "updatedAt": store.updated_at_for_relative_path(runtime_path) if runtime_path else store._now_iso(),
        "releaseSpec": release_spec,
        "artifacts": artifacts
    }


@router.get("/releases/{release_id}/operations", response_model=List[schemas.Operation])
async def list_release_operations(release_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    _load_latest_runtime_or_404(release_id)
    return store.list_release_operations(release_id)


@router.get("/releases/{release_id}/artifacts/{artifact_kind}")
async def get_release_artifact(
    release_id: str,
    artifact_kind: str,
    authorization: str | None = Header(default=None)
):
    _require_auth(authorization)
    artifacts = store.latest_artifacts(release_id)
    if not artifacts:
        raise HTTPException(status_code=404, detail="Release artifacts not found")
    key_map = {
        "releaseSpec": "releaseSpecPath",
        "runtimeSpec": "runtimeSpecPath",
        "composeYaml": "composeYamlPath"
    }
    artifact_key = key_map.get(artifact_kind)
    if not artifact_key:
        raise HTTPException(status_code=400, detail="Unknown artifact kind")
    relative_path = artifacts.get(artifact_key)
    if not relative_path:
        raise HTTPException(status_code=404, detail="Artifact missing")
    content = store.load_artifact_by_path(relative_path)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = "application/json" if relative_path.endswith(".json") else "text/plain"
    return Response(content, media_type=media_type)


@router.post("/releases/apply", response_model=schemas.Operation)
async def apply_release(request: ApplyRequest, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    plan = store.load_plan(request.release_id, request.plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")

    runtime_path = plan["artifacts"].get("runtimeSpecPath")
    if not runtime_path:
        raise HTTPException(status_code=400, detail="Plan missing runtimeSpecPath")

    runtime_spec = store.load_runtime_by_path(runtime_path)
    if not runtime_spec:
        raise HTTPException(status_code=404, detail="RuntimeSpec not found")

    operation_id = str(uuid.uuid4())
    status = "running"
    message = ""

    backend_type = runtime_spec["release"]["backend"]["type"]
    started_at = store._now_iso()

    operation_artifacts: Dict[str, str] = {}

    if backend_type == "compose":
        compose_path_rel = plan["artifacts"].get("composeYamlPath")
        if not compose_path_rel:
            status = "failed"
            message = "composeYamlPath missing from plan"
        else:
            compose_path = store.load_compose_path(compose_path_rel)
            action_kind = plan.get("actionKind")
            if action_kind:
                status, message, operation_artifacts = _apply_compose_action(
                    runtime_spec,
                    str(compose_path),
                    action_kind,
                    plan.get("options", {})
                )
            else:
                compose_cmd = _compose_base_cmd()
                env = _compose_env(runtime_spec)
                result = subprocess.run(
                    [*compose_cmd, "-f", str(compose_path), "up", "-d"],
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=str(compose_path.parent),
                    env=env
                )
                if result.returncode != 0:
                    status = "failed"
                    message = result.stderr.strip() or "docker compose apply failed"
                else:
                    status = "succeeded"
                    message = result.stdout.strip() or "apply succeeded"
    else:
        status = "failed"
        message = f"backend '{backend_type}' apply not implemented"

    finished_at = store._now_iso()
    artifacts = operation_artifacts
    if backend_type == "k8s":
        artifacts = {"notImplemented": "k8s"}

    operation = {
        "operationId": operation_id,
        "releaseId": request.release_id,
        "type": plan.get("actionKind") or "apply",
        "status": status,
        "createdAt": started_at,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "planId": request.plan_id,
        "message": message,
        "artifacts": artifacts
    }

    store.save_operation(request.release_id, operation_id, operation)
    return operation


@router.get("/releases/{release_id}/status", response_model=schemas.ReleaseStatus)
async def get_release_status(release_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    runtime_spec = store.load_latest_runtime(release_id)
    if not runtime_spec:
        raise HTTPException(status_code=404, detail="Release not found")

    services = []
    backend_type = runtime_spec["release"]["backend"]["type"]
    message = ""

    if backend_type == "compose":
        revisions_dir = store.release_revisions_dir(release_id)
        latest = store.latest_revision(release_id)
        compose_path = None
        if latest is not None:
            candidate = revisions_dir / str(latest) / "compose.yaml"
            if candidate.exists():
                compose_path = candidate

        if compose_path:
            project_name = f"{runtime_spec['metadata']['namespace']}_{runtime_spec['metadata']['name']}"
            env = {"COMPOSE_PROJECT_NAME": project_name, **dict(os.environ)}
            compose_cmd = _compose_base_cmd()
            result = subprocess.run(
                [*compose_cmd, "-f", str(compose_path), "ps", "--format", "json"],
                capture_output=True,
                text=True,
                check=False,
                cwd=str(compose_path.parent),
                env=env
            )
            if result.returncode == 0 and result.stdout.strip():
                import json
                try:
                    payload = json.loads(result.stdout)
                    if isinstance(payload, dict):
                        entries = [payload]
                    elif isinstance(payload, list):
                        entries = payload
                    else:
                        entries = []
                    if not entries:
                        entries = [
                            json.loads(line)
                            for line in result.stdout.splitlines()
                            if line.strip()
                        ]
                    for entry in entries:
                        state = entry.get("State") or "unknown"
                        health = entry.get("Health") or "unknown"
                        services.append({
                            "name": entry.get("Service", "unknown"),
                            "state": "running" if state == "running" else "stopped",
                            "health": health,
                            "details": entry
                        })
                except json.JSONDecodeError:
                    message = "unable to parse docker compose status output"
            else:
                message = result.stderr.strip() or "docker compose status failed"
        else:
            message = "compose.yaml not found for latest revision"
    else:
        message = f"backend '{backend_type}' status not implemented"
        services = [
            {
                "name": service.get("name", "unknown"),
                "state": "unknown",
                "health": "unknown",
                "details": {}
            }
            for service in runtime_spec.get("services", [])
        ]

    return {
        "releaseId": release_id,
        "desiredRevision": runtime_spec["release"]["revision"],
        "observed": {
            "timestamp": store._now_iso(),
            "backend": backend_type,
            "message": message
        },
        "services": services
    }


@router.get("/operations/{operation_id}", response_model=schemas.Operation)
async def get_operation(operation_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    operation = store.find_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation


@router.get("/operations/{operation_id}/logs")
async def get_operation_logs(operation_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    operation = store.find_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    artifacts = operation.get("artifacts", {})
    stdout_id = artifacts.get("stdoutArtifactId")
    stderr_id = artifacts.get("stderrArtifactId")
    output = []
    if stdout_id:
        stdout = store.load_artifact_text(stdout_id)
        if stdout:
            output.append(stdout)
    if stderr_id:
        stderr = store.load_artifact_text(stderr_id)
        if stderr:
            output.append(stderr)
    if not output:
        raise HTTPException(status_code=404, detail="No logs available")
    return Response("\n\n".join(output), media_type="text/plain")


@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    content = store.load_artifact_text(artifact_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return Response(content, media_type="text/plain")


@router.post("/plans/{plan_id}/apply", response_model=schemas.Operation)
async def apply_plan(plan_id: str, authorization: str | None = Header(default=None)):
    _require_auth(authorization)
    plan = store.find_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    release_id = plan.get("releaseId")
    if not release_id:
        raise HTTPException(status_code=400, detail="Plan missing releaseId")
    request = ApplyRequest(release_id=release_id, plan_id=plan_id)
    return await apply_release(request, authorization)
