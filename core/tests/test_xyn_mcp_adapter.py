from __future__ import annotations

from typing import Any, Dict
from unittest import TestCase, mock

from core.mcp.xyn_api_adapter import XynApiAdapter, XynApiAdapterConfig
from core.mcp.xyn_mcp_server import TOOL_NAMES, create_xyn_mcp_http_app, register_xyn_tools
from starlette.testclient import TestClient


class FakeMcpServer:
    def __init__(self) -> None:
        self.tools: Dict[str, Dict[str, Any]] = {}

    def add_tool(self, fn, name=None, description=None, **_kwargs):
        self.tools[str(name)] = {"fn": fn, "description": str(description or "")}


class XynMcpAdapterTests(TestCase):
    def test_register_xyn_tools_registers_expected_surface(self) -> None:
        adapter = mock.Mock()
        server = FakeMcpServer()

        register_xyn_tools(server, adapter)

        self.assertEqual(sorted(server.tools.keys()), sorted(TOOL_NAMES))

    def test_registered_tool_calls_underlying_adapter_without_workflow_invention(self) -> None:
        adapter = mock.Mock()
        adapter.run_release_target_execution_step.return_value = {"ok": False, "status_code": 409, "response": {"status": "blocked"}}
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        tool = server.tools["run_release_target_execution_step"]["fn"]
        result = tool(target_id="rt-1", payload={"approve_execution_step": False})

        adapter.run_release_target_execution_step.assert_called_once_with(
            target_id="rt-1",
            payload={"approve_execution_step": False},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 409)
        self.assertEqual(result["response"]["status"], "blocked")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_passes_base_url_auth_and_path(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"status": "ok"}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                api_base_url="http://xyn.local:8001",
                bearer_token="token-1",
                internal_token="int-1",
                cookie="sessionid=abc",
                timeout_seconds=11.0,
            )
        )

        result = adapter.inspect_change_session_control(application_id="app-1", session_id="sess-1")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        mock_request.assert_called_once()
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(
            kwargs["url"],
            "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/control",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-1")
        self.assertEqual(kwargs["headers"]["X-Internal-Token"], "int-1")
        self.assertEqual(kwargs["headers"]["Cookie"], "sessionid=abc")

    def test_healthz_surfaces_effective_xyn_api_base_and_auth_presence(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                api_base_url="http://localhost",
                bearer_token="token-1",
                internal_token="",
                cookie="sessionid=abc",
                timeout_seconds=10.0,
            )
        )
        app = create_xyn_mcp_http_app(adapter)
        with TestClient(app) as client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("xyn_api_base_url"), "http://localhost")
        auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
        self.assertTrue(bool(auth.get("has_bearer_token")))
        self.assertFalse(bool(auth.get("has_internal_token")))
        self.assertTrue(bool(auth.get("has_cookie")))
