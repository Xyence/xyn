from __future__ import annotations

import os
from typing import Any, Callable, Dict

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from core.mcp.xyn_api_adapter import XynApiAdapter, XynApiAdapterConfig


TOOL_NAMES = [
    "inspect_change_session_control",
    "run_change_session_control_action",
    "get_change_session_promotion_evidence",
    "get_release_target_deployment_plan",
    "create_release_target_deployment_preparation_evidence",
    "get_release_target_deployment_preparation_evidence",
    "create_release_target_execution_preparation_handoff",
    "get_release_target_execution_preparation_handoff",
    "consume_release_target_execution_preparation",
    "run_release_target_execution_step",
    "get_release_target_execution_step_history",
]


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
    mcp_server = create_xyn_mcp_server(adapter)
    # Prefer explicit streamable HTTP app construction (works across mcp versions).
    if hasattr(mcp_server, "streamable_http_app"):
        mcp_app = mcp_server.streamable_http_app()
    else:
        # Back-compat fallback for older FastMCP variants.
        mcp_app = mcp_server.run(transport="streamable-http", return_app=True)

    async def healthz(_request):
        return JSONResponse(
            {
                "status": "ok",
                "service": "xyn-mcp-adapter",
                "tool_count": len(TOOL_NAMES),
                "tools": TOOL_NAMES,
            }
        )

    return Starlette(
        routes=[
            Mount("/mcp", app=mcp_app),
            Route("/healthz", endpoint=healthz, methods=["GET"]),
        ]
    )

def main() -> None:
    host = str(os.getenv("XYN_MCP_HOST", "0.0.0.0")).strip() or "0.0.0.0"
    port = int(str(os.getenv("XYN_MCP_PORT", "8011")).strip() or "8011")
    server = create_xyn_mcp_server()
    # mcp>=1.18 requires run(streamable-http) so internal session task groups are initialized.
    server.settings.host = host
    server.settings.port = port
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
