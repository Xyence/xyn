from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

import httpx
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from core.mcp.xyn_api_adapter import XynApiAdapter, XynApiAdapterConfig


TOOL_NAMES = [
    "list_blueprints",
    "get_blueprint",
    "create_blueprint",
    "list_release_targets",
    "get_release_target",
    "create_release_target",
    "list_artifacts",
    "get_artifact",
    "list_deployment_providers",
    "get_provider_capabilities",
    "inspect_change_session_control",
    "run_change_session_control_action",
    "get_change_session_promotion_evidence",
    "get_release_target_deployment_plan",
    "create_release_target_deployment_preparation_evidence",
    "get_release_target_deployment_preparation_evidence",
    "create_release_target_execution_preparation_handoff",
    "get_release_target_execution_preparation_handoff",
    "approve_release_target_execution_preparation",
    "consume_release_target_execution_preparation",
    "approve_release_target_execution_step",
    "run_release_target_execution_step",
    "get_release_target_execution_step_history",
]


@dataclass(frozen=True)
class McpAuthConfig:
    mode: str
    bearer_token: str
    oidc_issuer: str
    oidc_client_id: str

    @staticmethod
    def from_env() -> "McpAuthConfig":
        mode = str(os.getenv("XYN_MCP_AUTH_MODE", "none")).strip().lower() or "none"
        if mode not in {"none", "token", "oidc"}:
            mode = "none"
        return McpAuthConfig(
            mode=mode,
            bearer_token=str(os.getenv("XYN_MCP_AUTH_BEARER_TOKEN", "")).strip(),
            oidc_issuer=str(os.getenv("OIDC_ISSUER", "")).strip()
            or str(os.getenv("XYN_OIDC_ISSUER", "")).strip(),
            oidc_client_id=str(os.getenv("OIDC_CLIENT_ID", "")).strip()
            or str(os.getenv("XYN_OIDC_CLIENT_ID", "")).strip(),
        )


def _register_tool(mcp_server: Any, *, name: str, description: str, fn: Callable[..., Dict[str, Any]]) -> None:
    if hasattr(mcp_server, "add_tool"):
        mcp_server.add_tool(fn, name=name, description=description)
        return
    if hasattr(mcp_server, "tool"):
        decorator = mcp_server.tool(name=name, description=description)
        decorator(fn)
        return
    raise RuntimeError("MCP server does not expose add_tool/tool registration API")


def register_xyn_tools(mcp_server: Any, adapter: XynApiAdapter) -> None:
    _register_tool(
        mcp_server,
        name="list_blueprints",
        description="List available blueprints for release-target binding.",
        fn=lambda: adapter.list_blueprints(),
    )
    _register_tool(
        mcp_server,
        name="get_blueprint",
        description="Get one blueprint by id.",
        fn=lambda blueprint_id: adapter.get_blueprint(blueprint_id=blueprint_id),
    )
    _register_tool(
        mcp_server,
        name="create_blueprint",
        description="Create or update a blueprint using existing blueprint API payload fields.",
        fn=lambda payload=None: adapter.create_blueprint(payload=payload),
    )
    _register_tool(
        mcp_server,
        name="list_release_targets",
        description="List discoverable release targets with provider and configuration summaries.",
        fn=lambda: adapter.list_release_targets(),
    )
    _register_tool(
        mcp_server,
        name="get_release_target",
        description="Get one release target by id.",
        fn=lambda target_id: adapter.get_release_target(target_id=target_id),
    )
    _register_tool(
        mcp_server,
        name="create_release_target",
        description="Create a release target using the existing release-target API payload contract.",
        fn=lambda payload=None: adapter.create_release_target(payload=payload),
    )
    _register_tool(
        mcp_server,
        name="list_artifacts",
        description="List discoverable artifacts using existing artifact registry models.",
        fn=lambda limit=100, offset=0: adapter.list_artifacts(limit=limit, offset=offset),
    )
    _register_tool(
        mcp_server,
        name="get_artifact",
        description="Get one artifact by id.",
        fn=lambda artifact_id: adapter.get_artifact(artifact_id=artifact_id),
    )
    _register_tool(
        mcp_server,
        name="list_deployment_providers",
        description="List deployment provider/module capabilities available to release-target workflows.",
        fn=lambda: adapter.list_deployment_providers(),
    )
    _register_tool(
        mcp_server,
        name="get_provider_capabilities",
        description="Get deployment provider/module capability details by provider key.",
        fn=lambda provider_key: adapter.get_provider_capabilities(provider_key=provider_key),
    )

    _register_tool(
        mcp_server,
        name="inspect_change_session_control",
        description="Inspect canonical control status for a solution change session.",
        fn=lambda application_id, session_id: adapter.inspect_change_session_control(
            application_id=application_id,
            session_id=session_id,
        ),
    )
    _register_tool(
        mcp_server,
        name="run_change_session_control_action",
        description="Execute a canonical control action for a change session.",
        fn=lambda application_id, session_id, operation, action_payload=None: adapter.run_change_session_control_action(
            application_id=application_id,
            session_id=session_id,
            operation=operation,
            action_payload=action_payload,
        ),
    )
    _register_tool(
        mcp_server,
        name="get_change_session_promotion_evidence",
        description="Fetch durable promotion/rollback evidence for a change session.",
        fn=lambda application_id, session_id: adapter.get_change_session_promotion_evidence(
            application_id=application_id,
            session_id=session_id,
        ),
    )
    _register_tool(
        mcp_server,
        name="get_release_target_deployment_plan",
        description="Fetch non-destructive seam-driven deployment plan for a release target.",
        fn=lambda target_id: adapter.get_release_target_deployment_plan(target_id=target_id),
    )
    _register_tool(
        mcp_server,
        name="create_release_target_deployment_preparation_evidence",
        description="Create deployment-preparation evidence for a release target.",
        fn=lambda target_id, payload=None: adapter.create_release_target_deployment_preparation_evidence(
            target_id=target_id,
            payload=payload,
        ),
    )
    _register_tool(
        mcp_server,
        name="get_release_target_deployment_preparation_evidence",
        description="Read deployment-preparation evidence history for a release target.",
        fn=lambda target_id, limit=10: adapter.get_release_target_deployment_preparation_evidence(
            target_id=target_id,
            limit=limit,
        ),
    )
    _register_tool(
        mcp_server,
        name="create_release_target_execution_preparation_handoff",
        description="Create execution-preparation handoff from deployment-preparation evidence.",
        fn=lambda target_id, payload=None: adapter.create_release_target_execution_preparation_handoff(
            target_id=target_id,
            payload=payload,
        ),
    )
    _register_tool(
        mcp_server,
        name="get_release_target_execution_preparation_handoff",
        description="Read execution-preparation handoff history for a release target.",
        fn=lambda target_id, limit=10: adapter.get_release_target_execution_preparation_handoff(
            target_id=target_id,
            limit=limit,
        ),
    )
    _register_tool(
        mcp_server,
        name="approve_release_target_execution_preparation",
        description="Approve execution-preparation handoff for a release target.",
        fn=lambda target_id, payload=None: adapter.approve_release_target_execution_preparation(
            target_id=target_id,
            payload=payload,
        ),
    )
    _register_tool(
        mcp_server,
        name="consume_release_target_execution_preparation",
        description="Consume execution-preparation handoff into prepared execution evidence.",
        fn=lambda target_id, payload=None: adapter.consume_release_target_execution_preparation(
            target_id=target_id,
            payload=payload,
        ),
    )
    _register_tool(
        mcp_server,
        name="run_release_target_execution_step",
        description="Run one explicitly approved bounded execution step for a release target.",
        fn=lambda target_id, payload=None: adapter.run_release_target_execution_step(
            target_id=target_id,
            payload=payload,
        ),
    )
    _register_tool(
        mcp_server,
        name="approve_release_target_execution_step",
        description="Approve one prepared execution step for a release target.",
        fn=lambda target_id, payload=None: adapter.approve_release_target_execution_step(
            target_id=target_id,
            payload=payload,
        ),
    )
    _register_tool(
        mcp_server,
        name="get_release_target_execution_step_history",
        description="Read execution-step evidence history for a release target.",
        fn=lambda target_id, limit=10: adapter.get_release_target_execution_step_history(
            target_id=target_id,
            limit=limit,
        ),
    )


def create_xyn_mcp_server(adapter: XynApiAdapter | None = None) -> Any:
    from mcp.server.fastmcp import FastMCP

    configured_adapter = adapter or XynApiAdapter(XynApiAdapterConfig.from_env())
    server = FastMCP("xyn-control-adapter")
    register_xyn_tools(server, configured_adapter)
    return server


def create_xyn_mcp_http_app(adapter: XynApiAdapter | None = None) -> Starlette:
    configured_adapter = adapter or XynApiAdapter(XynApiAdapterConfig.from_env())
    auth_config = McpAuthConfig.from_env()
    mcp_server = create_xyn_mcp_server(configured_adapter)
    # Prefer explicit streamable HTTP app construction (works across mcp versions).
    if hasattr(mcp_server, "streamable_http_app"):
        app = mcp_server.streamable_http_app()
    else:
        # Back-compat fallback for older FastMCP variants.
        app = mcp_server.run(transport="streamable-http", return_app=True)

    async def healthz(_request):
        return JSONResponse(
            {
                "status": "ok",
                "service": "xyn-mcp-adapter",
                "tool_count": len(TOOL_NAMES),
                "tools": TOOL_NAMES,
                "xyn_api_base_url": configured_adapter.config.api_base_url,
                "auth": {
                    "has_bearer_token": bool(configured_adapter.config.bearer_token),
                    "has_internal_token": bool(configured_adapter.config.internal_token),
                    "has_cookie": bool(configured_adapter.config.cookie),
                    "mcp_auth_mode": auth_config.mode,
                    "mcp_auth_token_configured": bool(auth_config.bearer_token),
                    "mcp_auth_oidc_configured": bool(auth_config.oidc_issuer and auth_config.oidc_client_id),
                },
            }
        )

    def _unauthorized(message: str) -> JSONResponse:
        return JSONResponse({"error": "unauthorized", "message": message}, status_code=401)

    def _extract_bearer_token(header_value: str) -> Optional[str]:
        raw = str(header_value or "").strip()
        if not raw:
            return None
        prefix, sep, remainder = raw.partition(" ")
        if not sep or prefix.lower() != "bearer":
            return None
        token = remainder.strip()
        return token or None

    async def _validate_oidc_bearer(token: str) -> Tuple[bool, str]:
        if not auth_config.oidc_issuer or not auth_config.oidc_client_id:
            return False, "OIDC auth mode requires OIDC_ISSUER and OIDC_CLIENT_ID"
        try:
            response = httpx.request(
                method="GET",
                url=f"{configured_adapter.config.api_base_url}/xyn/api/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=min(float(configured_adapter.config.timeout_seconds), 10.0),
            )
        except Exception:
            return False, "OIDC token validation failed: unable to reach Xyn API auth verifier"
        if response.status_code == 200:
            return True, ""
        return False, "Invalid OIDC bearer token"

    async def _mcp_auth_guard(request, call_next):
        path = str(request.url.path or "")
        if path == "/healthz" or not path.startswith("/mcp"):
            return await call_next(request)
        if auth_config.mode == "none":
            return await call_next(request)
        token = _extract_bearer_token(request.headers.get("Authorization", ""))
        if not token:
            return _unauthorized("Missing Authorization: Bearer <token> header")
        if auth_config.mode == "token":
            if not auth_config.bearer_token:
                return _unauthorized("MCP auth token mode is enabled but XYN_MCP_AUTH_BEARER_TOKEN is not configured")
            if not secrets.compare_digest(token, auth_config.bearer_token):
                return _unauthorized("Invalid bearer token")
            return await call_next(request)
        ok, message = await _validate_oidc_bearer(token)
        if not ok:
            return _unauthorized(message)
        return await call_next(request)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_mcp_auth_guard)

    # Add diagnostics route directly on the same MCP Starlette app so lifespan/task-group init stays intact.
    app.add_route("/healthz", healthz, methods=["GET"])
    return app

def main() -> None:
    bind_host = str(os.getenv("XYN_MCP_BIND_HOST", "")).strip()
    if not bind_host:
        legacy_host = str(os.getenv("XYN_MCP_HOST", "")).strip()
        if legacy_host in {"0.0.0.0", "127.0.0.1", "localhost", "::", "::1"}:
            bind_host = legacy_host
    host = bind_host or "0.0.0.0"
    port = int(str(os.getenv("XYN_MCP_PORT", "8011")).strip() or "8011")
    import uvicorn

    app = create_xyn_mcp_http_app()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
