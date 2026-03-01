"""Kernel artifact role loader.

Loads workspace-installed artifacts from xyn-api bindings and registers
artifact roles into the running FastAPI app.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class HttpProxyApp:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b"unsupported scope"})
            return

        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        method = scope.get("method", "GET")
        path = scope.get("path", "/")
        query = (scope.get("query_string") or b"").decode("utf-8")
        target = f"{self.base_url}{path}"
        if query:
            target = f"{target}?{query}"

        headers = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
            if k.decode("latin-1").lower() not in {"host", "content-length", "connection"}
        }

        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=float(os.getenv("XYN_KERNEL_PROXY_TIMEOUT_SECONDS", "30"))) as client:
                upstream = await client.request(method=method, url=target, content=body, headers=headers)
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            logger.exception("proxy request failed target=%s error=%s", target, exc)
            await send(
                {
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"upstream unavailable"}'})
            return

        response_headers = [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in upstream.headers.items()
            if k.lower() not in {"content-encoding", "transfer-encoding", "connection"}
        ]
        await send({"type": "http.response.start", "status": upstream.status_code, "headers": response_headers})
        await send({"type": "http.response.body", "body": upstream.content})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _manifest_roots() -> list[Path]:
    raw = os.getenv("XYN_KERNEL_MANIFEST_ROOTS", str(_repo_root().parent)).strip()
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        token = part.strip()
        if not token:
            continue
        roots.append(Path(token))
    return roots


def _resolve_path(ref: str, roots: list[Path]) -> Path | None:
    ref = str(ref or "").strip()
    if not ref:
        return None
    candidate = Path(ref)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    for root in roots:
        maybe = root / ref
        if maybe.exists():
            return maybe
    return None


@contextmanager
def _patched_syspath(paths: list[str]):
    additions = [p for p in paths if p and p not in sys.path]
    if additions:
        sys.path[:0] = additions
    try:
        yield
    finally:
        for p in additions:
            if p in sys.path:
                sys.path.remove(p)


def _load_entrypoint(entrypoint: str, pythonpaths: list[str]) -> Any:
    if ":" not in entrypoint:
        raise ValueError(f"entrypoint must be module:attr, got '{entrypoint}'")
    module_name, attr_name = entrypoint.split(":", 1)
    with _patched_syspath(pythonpaths):
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)


def _register_api_router(app: FastAPI, role: dict[str, Any], roots: list[Path], artifact: dict[str, Any]) -> None:
    entrypoint = str(role.get("entrypoint") or "").strip()
    if not entrypoint:
        raise ValueError("api_router role missing entrypoint")

    pythonpaths_raw = role.get("pythonpath") if isinstance(role.get("pythonpath"), list) else []
    pythonpaths: list[str] = []
    for item in pythonpaths_raw:
        resolved = _resolve_path(str(item), roots)
        pythonpaths.append(str(resolved or item))

    loaded = _load_entrypoint(entrypoint, pythonpaths)
    mount_path = str(role.get("mount_path") or role.get("prefix") or "").strip()

    if hasattr(loaded, "routes") and isinstance(getattr(loaded, "routes"), list):
        prefix = mount_path if mount_path and mount_path != "/" else ""
        app.include_router(loaded, prefix=prefix)
        logger.info("registered api_router as APIRouter artifact_id=%s entrypoint=%s prefix=%s", artifact.get("artifact_id"), entrypoint, prefix or "/")
        return

    if callable(loaded):
        asgi_app: ASGIApp = loaded  # type: ignore[assignment]
        app.mount(mount_path or "/", asgi_app)
        logger.info("registered api_router as ASGI app artifact_id=%s entrypoint=%s mount=%s", artifact.get("artifact_id"), entrypoint, mount_path or "/")
        return

    raise ValueError(f"unsupported api_router entrypoint object for {entrypoint}")


def _register_ui_mount(app: FastAPI, role: dict[str, Any], roots: list[Path], artifact: dict[str, Any]) -> None:
    mount_path = str(role.get("mount_path") or "/").strip() or "/"
    static_dir = str(role.get("static_dir") or "").strip()
    dev_proxy_url = str(role.get("dev_proxy_url") or "").strip()

    if static_dir:
        resolved_static = _resolve_path(static_dir, roots)
        if resolved_static and resolved_static.exists() and resolved_static.is_dir():
            app.mount(mount_path, StaticFiles(directory=str(resolved_static), html=True), name=f"ui-{artifact.get('artifact_id')}")
            logger.info("registered ui_mount static artifact_id=%s mount=%s dir=%s", artifact.get("artifact_id"), mount_path, resolved_static)
            return

    if dev_proxy_url:
        app.mount(mount_path, HttpProxyApp(dev_proxy_url), name=f"ui-proxy-{artifact.get('artifact_id')}")
        logger.info("registered ui_mount proxy artifact_id=%s mount=%s upstream=%s", artifact.get("artifact_id"), mount_path, dev_proxy_url)
        return

    raise ValueError("ui_mount role requires static_dir (existing) or dev_proxy_url")


def _role_mount_path(role_name: str, role: dict[str, Any]) -> str | None:
    if role_name == "ui_mount":
        mount_path = str(role.get("mount_path") or "/").strip() or "/"
        return mount_path
    if role_name == "api_router":
        mount_path = role.get("mount_path")
        if mount_path is None:
            mount_path = role.get("prefix")
        if mount_path is None:
            return None
        mount_path = str(mount_path).strip()
        if not mount_path:
            return None
        return mount_path
    return None


def _role_priority(role_name: str, role: dict[str, Any]) -> int:
    mount_path = _role_mount_path(role_name, role)
    if role_name == "api_router":
        return 0
    if role_name == "ui_mount" and mount_path != "/":
        return 1
    if role_name == "ui_mount":
        return 2
    if role_name == "worker":
        return 3
    return 4


def _validate_root_mount_collisions(plans: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    root_artifacts: list[str] = []
    for artifact, manifest in plans:
        roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []
        for role in roles:
            if not isinstance(role, dict):
                continue
            role_name = str(role.get("role") or "").strip().lower()
            mount_path = _role_mount_path(role_name, role)
            if mount_path == "/":
                root_artifacts.append(str(artifact.get("artifact_id") or artifact.get("id") or ""))
    unique = sorted({artifact_id for artifact_id in root_artifacts if artifact_id})
    if len(unique) > 1:
        raise RuntimeError(f"multiple artifact roles attempt to mount '/': {', '.join(unique)}")


def register_manifest_roles(app: FastAPI, manifest: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []
    roots = _manifest_roots()
    registered: list[str] = []

    parsed_roles: list[tuple[int, str, dict[str, Any]]] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        role_name = str(role.get("role") or "").strip().lower()
        if not role_name:
            continue
        parsed_roles.append((_role_priority(role_name, role), role_name, role))

    for _, role_name, role in sorted(parsed_roles, key=lambda item: item[0]):
        if role_name == "api_router":
            _register_api_router(app, role, roots, artifact)
            registered.append("api_router")
        elif role_name == "ui_mount":
            _register_ui_mount(app, role, roots, artifact)
            registered.append("ui_mount")
        elif role_name == "worker":
            # Phase 1 keeps worker role optional; loader acknowledges role without activation.
            logger.info("worker role declared but not activated in phase1 artifact_id=%s", artifact.get("artifact_id"))
            registered.append("worker")

    return {
        "artifact_id": str(artifact.get("artifact_id") or ""),
        "title": str(artifact.get("title") or artifact.get("name") or ""),
        "roles": registered,
    }


async def _fetch_workspace_artifacts() -> list[dict[str, Any]]:
    inline = os.getenv("XYN_KERNEL_BINDINGS_JSON", "").strip()
    if inline:
        payload = json.loads(inline)
        return payload if isinstance(payload, list) else []

    workspace_id = os.getenv("XYN_KERNEL_WORKSPACE_ID", "").strip()
    if not workspace_id:
        logger.warning("XYN_KERNEL_WORKSPACE_ID is not set; skipping artifact role load")
        return []

    api_base = os.getenv("XYN_API_BASE_URL", "http://localhost:8000").rstrip("/")
    url = os.getenv("XYN_KERNEL_BINDINGS_URL", "").strip() or f"{api_base}/xyn/internal/workspaces/{workspace_id}/artifacts"
    token = os.getenv("XYENCE_INTERNAL_TOKEN", "").strip()
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Internal-Token"] = token

    async with httpx.AsyncClient(timeout=float(os.getenv("XYN_KERNEL_BINDINGS_TIMEOUT_SECONDS", "10"))) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("artifacts") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _resolve_manifest_ref(artifact: dict[str, Any], roots: list[Path]) -> Path | None:
    manifest_ref = str(artifact.get("manifest_ref") or "").strip()
    if manifest_ref:
        resolved = _resolve_path(manifest_ref, roots)
        if resolved:
            return resolved

    slug = str(artifact.get("slug") or "").strip()
    if slug:
        candidates = [
            f"{slug}/artifact.manifest.json",
            f"{slug}/manifest.json",
            f"{slug}.manifest.json",
        ]
        for candidate in candidates:
            resolved = _resolve_path(candidate, roots)
            if resolved:
                return resolved
    return None


async def load_workspace_artifacts_into_app(app: FastAPI) -> list[dict[str, Any]]:
    rows = await _fetch_workspace_artifacts()
    roots = _manifest_roots()
    plans: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for row in rows:
        if not bool(row.get("enabled", True)):
            continue
        if str(row.get("installed_state") or "").strip().lower() != "installed":
            continue

        manifest_path = _resolve_manifest_ref(row, roots)
        if not manifest_path:
            logger.warning("manifest not found for artifact_id=%s title=%s", row.get("artifact_id"), row.get("title") or row.get("name"))
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            plans.append((row, manifest))
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            logger.exception("failed to load artifact_id=%s manifest=%s error=%s", row.get("artifact_id"), manifest_path, exc)

    _validate_root_mount_collisions(plans)

    loaded: list[dict[str, Any]] = [
        {
            "artifact_id": str(row.get("artifact_id") or ""),
            "title": str(row.get("title") or row.get("name") or ""),
            "roles": [],
        }
        for row, _ in plans
    ]
    role_jobs: list[tuple[int, int, str, dict[str, Any]]] = []
    for idx, (_, manifest) in enumerate(plans):
        roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []
        for role in roles:
            if not isinstance(role, dict):
                continue
            role_name = str(role.get("role") or "").strip().lower()
            if not role_name:
                continue
            role_jobs.append((_role_priority(role_name, role), idx, role_name, role))

    for _, idx, role_name, role in sorted(role_jobs, key=lambda item: (item[0], item[1])):
        artifact = plans[idx][0]
        if role_name == "api_router":
            _register_api_router(app, role, roots, artifact)
            loaded[idx]["roles"].append("api_router")
        elif role_name == "ui_mount":
            _register_ui_mount(app, role, roots, artifact)
            loaded[idx]["roles"].append("ui_mount")
        elif role_name == "worker":
            logger.info("worker role declared but not activated in phase1 artifact_id=%s", artifact.get("artifact_id"))
            loaded[idx]["roles"].append("worker")

    app.state.kernel_loaded_artifacts = loaded
    return loaded


__all__ = [
    "HttpProxyApp",
    "load_workspace_artifacts_into_app",
    "register_manifest_roles",
]
