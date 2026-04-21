from __future__ import annotations

import inspect
import io
import zipfile
from typing import Any, Dict
from unittest import TestCase, mock

import httpx

from core.mcp.xyn_api_adapter import (
    XynApiAdapter,
    XynApiAdapterConfig,
    reset_request_bearer_token,
    set_request_bearer_token,
)
from core.mcp.xyn_mcp_server import (
    TOOL_NAMES,
    _assert_critical_planner_tools_available,
    _build_tool_surface,
    create_xyn_mcp_http_app,
    register_xyn_tools,
)
from starlette.testclient import TestClient


class FakeMcpServer:
    def __init__(self) -> None:
        self.tools: Dict[str, Dict[str, Any]] = {}

    def add_tool(self, fn, name=None, description=None, **_kwargs):
        self.tools[str(name)] = {"fn": fn, "description": str(description or "")}


class XynMcpAdapterTests(TestCase):
    @staticmethod
    def _build_zip_bytes(files: Dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, payload in files.items():
                archive.writestr(path, payload)
        return buffer.getvalue()

    def test_register_xyn_tools_registers_expected_surface(self) -> None:
        adapter = mock.Mock()
        server = FakeMcpServer()

        register_xyn_tools(server, adapter)

        self.assertEqual(sorted(server.tools.keys()), sorted(TOOL_NAMES))

    @mock.patch.dict(
        "os.environ",
        {
            "XYN_MCP_XYN_CONTROL_API_BASE_URL": "http://xyn.local:8001",
            "XYN_MCP_WORKSPACE_ID": "",
            "XYN_WORKSPACE_ID": "legacy-ws",
        },
        clear=False,
    )
    def test_adapter_config_from_env_ignores_legacy_workspace_env(self) -> None:
        config = XynApiAdapterConfig.from_env()
        self.assertEqual(config.default_workspace_id, "")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_artifacts_default_call_no_parameters(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifacts": []}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_artifacts()
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts")
        self.assertEqual(kwargs["params"], {"limit": 100, "offset": 0})
        response_body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(response_body.get("artifacts"), [])
        self.assertEqual(response_body.get("count"), 0)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_artifacts_remains_local_registry_only(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifacts": [{"id": "a1", "title": "local"}]}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_artifacts(limit=10, offset=0)
        self.assertTrue(result["ok"])
        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn("http://xyn.local:8001/xyn/api/artifacts", urls)
        self.assertNotIn("http://xyn.local:8001/xyn/api/artifacts/remote-candidates", urls)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_remote_artifact_candidates_path(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "candidates": [
                {
                    "artifact_slug": "deal-finder",
                    "artifact_type": "application",
                    "title": "Deal Finder",
                    "installed": False,
                    "artifact_origin": "remote_catalog",
                    "source_ref_type": "RemoteArtifactSource",
                    "source_ref_id": "bundle:abc:deal-finder:application",
                    "remote_source": {
                        "manifest_source": "s3://bundle/manifest.json",
                        "package_source": "s3://bundle/package.zip",
                    },
                }
            ]
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_remote_artifact_candidates(
            manifest_source="s3://bundle/manifest.json",
            artifact_slug="deal-finder",
        )
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts/remote-candidates")
        self.assertEqual(kwargs["params"]["manifest_source"], "s3://bundle/manifest.json")
        self.assertEqual(kwargs["params"]["artifact_slug"], "deal-finder")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("count"), 1)
        self.assertEqual((body.get("candidates") or [])[0].get("manifest_source"), "s3://bundle/manifest.json")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_remote_artifact_sources_path(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "sources": [{"source": "s3://bucket/root", "source_type": "s3", "bucket": "bucket", "prefix": "root"}],
            "count": 1,
            "source_mode": "s3",
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_remote_artifact_sources()
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts/remote-sources")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("count"), 1)
        self.assertEqual((body.get("sources") or [])[0].get("source"), "s3://bucket/root")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_search_remote_artifact_catalog_path(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "candidates": [{"artifact_slug": "deal-finder-api", "artifact_type": "application"}],
            "count": 1,
            "total": 1,
            "next_cursor": "",
            "source_roots": ["s3://bucket/root"],
            "errors": [],
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.search_remote_artifact_catalog(query="deal finder", artifact_type="application", limit=25)
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts/remote-catalog")
        self.assertEqual(kwargs["params"]["q"], "deal finder")
        self.assertEqual(kwargs["params"]["artifact_type"], "application")
        self.assertEqual(kwargs["params"]["limit"], 25)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("total"), 1)
        self.assertEqual((body.get("candidates") or [])[0].get("artifact_slug"), "deal-finder-api")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_source_tree_path(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifact": {"id": "a1", "slug": "app.demo"}, "files": []}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_source_tree(artifact_slug="app.demo")
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts/source-tree")
        self.assertEqual(kwargs["params"]["artifact_slug"], "app.demo")
        self.assertTrue(kwargs["params"]["include_files"])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_source_tree_prefers_code_api_base_url(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifact": {"id": "a1", "slug": "app.demo"}, "files": []}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_source_tree(artifact_slug="app.demo")
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["url"], "http://xyn-core:8000/xyn/api/artifacts/source-tree")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_source_tree_falls_back_from_code_api_base_url(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 404
        first.json.return_value = {"detail": "Not Found"}
        second = mock.Mock()
        second.status_code = 404
        second.json.return_value = {"detail": "Not Found"}
        third = mock.Mock()
        third.status_code = 200
        third.json.return_value = {"artifact": {"id": "a1", "slug": "app.demo"}, "files": []}
        mock_request.side_effect = [first, second, third]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_source_tree(artifact_slug="app.demo")
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 3)
        first_kwargs = mock_request.call_args_list[0].kwargs
        second_kwargs = mock_request.call_args_list[1].kwargs
        third_kwargs = mock_request.call_args_list[2].kwargs
        self.assertEqual(first_kwargs["url"], "http://xyn-core:8000/xyn/api/artifacts/source-tree")
        self.assertEqual(second_kwargs["url"], "http://xyn-core:8000/api/v1/artifacts/source-tree")
        self.assertEqual(third_kwargs["url"], "http://core:8000/xyn/api/artifacts/source-tree")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_source_tree_falls_back_when_code_api_upstream_unreachable(self, mock_request: mock.Mock) -> None:
        second = mock.Mock()
        second.status_code = 200
        second.json.return_value = {"artifact": {"id": "a1", "slug": "app.demo"}, "files": []}
        mock_request.side_effect = [httpx.ConnectError("connect failed"), second]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_source_tree(artifact_slug="app.demo")
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 2)
        first_kwargs = mock_request.call_args_list[0].kwargs
        second_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["url"], "http://xyn-core:8000/xyn/api/artifacts/source-tree")
        self.assertEqual(second_kwargs["url"], "http://xyn-core:8000/api/v1/artifacts/source-tree")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_read_artifact_source_file_path(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifact": {"id": "a1"}, "path": "README.md", "content": "hello", "start_line": 1, "end_line": 1}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.read_artifact_source_file(path="README.md", artifact_id="a1", start_line=1, end_line=5)
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts/source-file")
        self.assertEqual(kwargs["params"]["path"], "README.md")
        self.assertEqual(kwargs["params"]["artifact_id"], "a1")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_source_tree_derives_code_api_base_from_control_url(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifact": {"id": "a1", "slug": "app.demo"}, "files": []}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_source_tree(artifact_slug="app.demo")
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["url"], "http://xyn-core:8000/xyn/api/artifacts/source-tree")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_source_tree_passes_bounds(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifact": {"id": "a1", "slug": "app.demo"}, "files": []}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_source_tree(
            artifact_slug="app.demo",
            max_files=100,
            max_depth=3,
            include_files=False,
        )
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["params"]["max_files"], 100)
        self.assertEqual(kwargs["params"]["max_depth"], 3)
        self.assertFalse(kwargs["params"]["include_files"])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_analyze_codebase_supports_mode_param(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"analysis_mode": "python_api"}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.analyze_artifact_codebase(artifact_slug="app.demo", mode="python_api")
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts/analyze-codebase")
        self.assertEqual(kwargs["params"]["artifact_slug"], "app.demo")
        self.assertEqual(kwargs["params"]["mode"], "python_api")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_source_tree_falls_back_from_xyn_api_route_to_api_v1(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 404
        first.json.return_value = {"detail": "Not Found"}
        second = mock.Mock()
        second.status_code = 200
        second.json.return_value = {"artifact": {"id": "a1", "slug": "app.demo"}, "files": []}
        mock_request.side_effect = [first, second]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_source_tree(artifact_slug="app.demo")
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 2)
        first_kwargs = mock_request.call_args_list[0].kwargs
        second_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts/source-tree")
        self.assertEqual(second_kwargs["url"], "http://xyn.local:8001/api/v1/artifacts/source-tree")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_create_change_effort_routes_to_api_v1(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"change_effort": {"id": "eff-1"}}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_change_effort(payload={"workspace_id": "w1", "artifact_slug": "xyn-api"})
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["url"], "http://xyn-core:8000/api/v1/change-efforts")
        self.assertEqual((kwargs.get("json") or {}).get("workspace_id"), "w1")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "w1")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_change_effort_uses_configured_default_workspace_when_missing(self, mock_request: mock.Mock) -> None:
        list_workspaces = mock.Mock()
        list_workspaces.status_code = 200
        list_workspaces.json.return_value = {"workspaces": [{"id": "ws-default"}]}
        create = mock.Mock()
        create.status_code = 200
        create.json.return_value = {"change_effort": {"id": "eff-1"}}
        mock_request.side_effect = [list_workspaces, create]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
                default_workspace_id="ws-default",
            )
        )
        result = adapter.create_change_effort(payload={"artifact_slug": "xyn-api"})
        self.assertTrue(result["ok"])
        create_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual((create_kwargs.get("json") or {}).get("workspace_id"), "ws-default")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "ws-default")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_change_effort_returns_workspace_required_for_multi_workspace_without_default(self, mock_request: mock.Mock) -> None:
        list_workspaces = mock.Mock()
        list_workspaces.status_code = 200
        list_workspaces.json.return_value = {"workspaces": [{"id": "ws-1"}, {"id": "ws-2"}]}
        mock_request.return_value = list_workspaces
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_change_effort(payload={"artifact_slug": "xyn-api"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 400)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("error"), "workspace_required")
        self.assertEqual(len(body.get("candidate_workspaces") or []), 2)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_promote_change_effort_routes_to_api_v1(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"promotion": {"id": "p1"}}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.promote_change_effort(effort_id="eff-1", payload={"to_branch": "develop"})
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "POST")
        self.assertEqual(kwargs["url"], "http://xyn-core:8000/api/v1/change-efforts/eff-1/promote")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_get_artifact_provenance_routes_with_workspace_query(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"artifact_slug": "xyn-api"}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_artifact_provenance(artifact_slug="xyn-api", workspace_id="w1")
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["method"], "GET")
        self.assertEqual(kwargs["url"], "http://xyn-core:8000/api/v1/provenance/xyn-api")
        self.assertEqual(kwargs["params"], {"workspace_id": "w1"})
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "w1")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_change_effort_related_routes_use_code_api_base_url(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        adapter.get_change_effort(effort_id="eff-1")
        adapter.resolve_effort_source(effort_id="eff-1")
        adapter.allocate_effort_branch(effort_id="eff-1", payload={"base_branch": "develop"})
        adapter.allocate_effort_worktree(effort_id="eff-1", payload={"root_path": "/tmp"})
        adapter.declare_release(payload={"workspace_id": "w1"})

        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn("http://xyn-core:8000/api/v1/change-efforts/eff-1", urls)
        self.assertIn("http://xyn-core:8000/api/v1/change-efforts/eff-1/resolve-source", urls)
        self.assertIn("http://xyn-core:8000/api/v1/change-efforts/eff-1/allocate-branch", urls)
        self.assertIn("http://xyn-core:8000/api/v1/change-efforts/eff-1/allocate-worktree", urls)
        self.assertIn("http://xyn-core:8000/api/v1/releases/declare", urls)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_change_effort_resolve_source_normalizes_provenance_fields(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "change_effort": {
                "id": "eff-1",
                "artifact_slug": "xyn-api",
                "repo_key": "xyn-platform",
                "repo_url": "https://github.com/xyence/xyn-platform",
                "repo_subpath": "services/xyn-api/backend",
                "work_branch": "xyn/xyn-api/abc123",
                "target_branch": "develop",
                "worktree_path": "/workspace/.xyn/change-efforts/ws/eff-1",
                "status": "source_resolved",
                "metadata_json": {"application_id": "app-1", "session_id": "sess-1"},
            },
            "source": {
                "kind": "git",
                "repo_key": "xyn-platform",
                "repo_url": "https://github.com/xyence/xyn-platform",
                "commit_sha": "3f93410",
                "monorepo_subpath": "services/xyn-api/backend",
            },
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.resolve_effort_source(effort_id="eff-1")
        self.assertTrue(result["ok"])
        body = result.get("response") or {}
        self.assertEqual(body.get("repo_key"), "xyn-platform")
        self.assertEqual(body.get("commit_sha"), "3f93410")
        self.assertEqual(body.get("source_roots"), ["services/xyn-api/backend"])
        self.assertEqual(body.get("branch_name"), "xyn/xyn-api/abc123")
        self.assertEqual(body.get("allowed_paths"), ["services/xyn-api/backend/**"])
        linked = body.get("linked_change_session") or {}
        self.assertTrue(linked.get("connected"))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_change_effort_branch_and_worktree_allocation_surface_deterministic_context(self, mock_request: mock.Mock) -> None:
        branch = mock.Mock()
        branch.status_code = 200
        branch.json.return_value = {
            "change_effort": {
                "id": "eff-2",
                "artifact_slug": "xyn-api",
                "repo_key": "xyn-platform",
                "repo_subpath": "services/xyn-api/backend",
                "work_branch": "xyn/xyn-api/123456789abc",
                "target_branch": "develop",
                "status": "branch_allocated",
                "metadata_json": {},
            }
        }
        worktree = mock.Mock()
        worktree.status_code = 200
        worktree.json.return_value = {
            "change_effort": {
                "id": "eff-2",
                "artifact_slug": "xyn-api",
                "repo_key": "xyn-platform",
                "repo_subpath": "services/xyn-api/backend",
                "work_branch": "xyn/xyn-api/123456789abc",
                "target_branch": "develop",
                "worktree_path": "/workspace/.xyn/change-efforts/ws/eff-2",
                "status": "worktree_allocated",
                "metadata_json": {},
            }
        }
        mock_request.side_effect = [branch, worktree]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        branch_result = adapter.allocate_effort_branch(effort_id="eff-2", payload={"base_branch": "develop"})
        worktree_result = adapter.allocate_effort_worktree(effort_id="eff-2", payload={"root_path": "/workspace/.xyn/change-efforts"})
        self.assertTrue(branch_result["ok"])
        self.assertEqual((branch_result.get("response") or {}).get("branch_name"), "xyn/xyn-api/123456789abc")
        self.assertTrue(worktree_result["ok"])
        worktree_body = worktree_result.get("response") or {}
        self.assertEqual(worktree_body.get("worktree_path"), "/workspace/.xyn/change-efforts/ws/eff-2")
        self.assertEqual(worktree_body.get("worktree_token"), "eff-2")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_change_effort_resolve_source_structured_errors_for_missing_provenance_and_ambiguity(self, mock_request: mock.Mock) -> None:
        missing = mock.Mock()
        missing.status_code = 409
        missing.json.return_value = {"detail": "artifact provenance is missing source.kind=git"}
        ambiguous = mock.Mock()
        ambiguous.status_code = 409
        ambiguous.json.return_value = {"detail": "ambiguous source mapping for artifact slug"}
        mock_request.side_effect = [missing, ambiguous]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        missing_result = adapter.resolve_effort_source(effort_id="eff-missing")
        ambiguous_result = adapter.resolve_effort_source(effort_id="eff-ambiguous")
        self.assertFalse(missing_result["ok"])
        self.assertEqual((missing_result.get("response") or {}).get("blocked_reason"), "missing_provenance")
        self.assertFalse(ambiguous_result["ok"])
        self.assertEqual((ambiguous_result.get("response") or {}).get("blocked_reason"), "ambiguous_source")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_change_effort_preview_binding_connects_to_change_session_preview_status(self, mock_request: mock.Mock) -> None:
        effort = mock.Mock()
        effort.status_code = 200
        effort.json.return_value = {
            "change_effort": {
                "id": "eff-9",
                "artifact_slug": "xyn-api",
                "metadata_json": {"application_id": "app-1", "session_id": "sess-1"},
                "status": "worktree_allocated",
            }
        }
        session_control = mock.Mock()
        session_control.status_code = 200
        session_control.json.return_value = {
            "status": "preview_ready",
            "preview_urls": ["https://preview.example.com/sess-1"],
            "session_build": {"status": "ready"},
        }
        mock_request.side_effect = [effort, session_control]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_effort_preview_binding(effort_id="eff-9")
        self.assertTrue(result["ok"])
        body = result.get("response") or {}
        self.assertEqual(((body.get("preview_binding") or {}).get("preview_status")), "preview_ready")
        self.assertEqual(((body.get("linked_change_session") or {}).get("application_id")), "app-1")
        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn("http://xyn-core:8000/api/v1/change-efforts/eff-9", urls)
        self.assertIn("http://xyn.local:8001/xyn/api/change-sessions/sess-1/control", urls)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_effort_changed_files_falls_back_to_effort_metadata_when_endpoint_missing(self, mock_request: mock.Mock) -> None:
        missing_endpoint_1 = mock.Mock()
        missing_endpoint_1.status_code = 404
        missing_endpoint_1.json.return_value = {"detail": "Not Found"}
        missing_endpoint_2 = mock.Mock()
        missing_endpoint_2.status_code = 404
        missing_endpoint_2.json.return_value = {"detail": "Not Found"}
        effort = mock.Mock()
        effort.status_code = 200
        effort.json.return_value = {
            "change_effort": {
                "id": "eff-10",
                "artifact_slug": "xyn-api",
                "status": "worktree_allocated",
                "metadata_json": {"changed_files": ["xyn_orchestrator/xyn_api.py"]},
            }
        }
        mock_request.side_effect = [missing_endpoint_1, missing_endpoint_2, effort]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_effort_changed_files(effort_id="eff-10")
        self.assertTrue(result["ok"])
        self.assertEqual((result.get("response") or {}).get("changed_files"), ["xyn_orchestrator/xyn_api.py"])
        self.assertEqual((result.get("response") or {}).get("blocked_reason"), "not_supported")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_application_and_change_session_discovery_routes(self, mock_request: mock.Mock) -> None:
        list_apps = mock.Mock()
        list_apps.status_code = 200
        list_apps.json.return_value = {"applications": [{"id": "app-1", "slug": "xyn-api", "name": "Xyn API", "status": "active"}]}

        get_app = mock.Mock()
        get_app.status_code = 200
        get_app.json.return_value = {"id": "app-1", "slug": "xyn-api", "name": "Xyn API", "status": "active"}

        list_sessions = mock.Mock()
        list_sessions.status_code = 200
        list_sessions.json.return_value = {"change_sessions": [{"id": "sess-1", "application_id": "app-1", "status": "draft"}]}

        get_session = mock.Mock()
        get_session.status_code = 200
        get_session.json.return_value = {"id": "sess-1", "application_id": "app-1", "status": "draft"}

        mock_request.side_effect = [list_apps, get_app, list_sessions, get_session]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        applications = adapter.list_applications(workspace_id="ws-1")
        application = adapter.get_application(application_id="app-1")
        sessions = adapter.list_application_change_sessions(application_id="app-1")
        session = adapter.get_application_change_session(application_id="app-1", session_id="sess-1")

        self.assertTrue(applications["ok"])
        self.assertEqual((applications.get("response") or {}).get("count"), 1)
        self.assertTrue(application["ok"])
        self.assertEqual(((application.get("response") or {}).get("application") or {}).get("id"), "app-1")
        self.assertTrue(sessions["ok"])
        self.assertEqual((sessions.get("response") or {}).get("count"), 1)
        self.assertTrue(session["ok"])
        self.assertEqual(((session.get("response") or {}).get("change_session") or {}).get("id"), "sess-1")

        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn("http://xyn.local:8001/xyn/api/applications", urls)
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1", urls)
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions", urls)
        self.assertIn("http://xyn.local:8001/xyn/api/change-sessions/sess-1", urls)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_passes_workspace_id_query_param(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"applications": []}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.list_applications(workspace_id="ws-123")

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/xyn/api/applications")
        self.assertEqual(kwargs["params"], {"workspace_id": "ws-123"})
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "ws-123")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_falls_back_to_single_accessible_workspace(self, mock_request: mock.Mock) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.json.return_value = {"workspaces": [{"id": "ws-auto"}]}

        applications = mock.Mock()
        applications.status_code = 200
        applications.json.return_value = {"applications": [{"id": "app-1", "name": "Xyn API"}]}

        mock_request.side_effect = [workspaces, applications]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.list_applications()

        self.assertTrue(result["ok"])
        self.assertEqual((result.get("response") or {}).get("count"), 1)
        first_kwargs = mock_request.call_args_list[0].kwargs
        second_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["url"], "http://xyn.local:8001/xyn/api/workspaces")
        self.assertEqual(second_kwargs["url"], "http://xyn.local:8001/xyn/api/applications")
        self.assertEqual(second_kwargs.get("params"), {"workspace_id": "ws-auto"})
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "ws-auto")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_uses_configured_default_workspace(self, mock_request: mock.Mock) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.json.return_value = {"workspaces": [{"id": "ws-default"}]}

        applications = mock.Mock()
        applications.status_code = 200
        applications.json.return_value = {"applications": []}

        mock_request.side_effect = [workspaces, applications]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
                default_workspace_id="ws-default",
            )
        )

        result = adapter.list_applications()
        self.assertTrue(result["ok"])
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "ws-default")
        second_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(second_kwargs["params"], {"workspace_id": "ws-default"})

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_returns_workspace_required_when_multiple_accessible(self, mock_request: mock.Mock) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.json.return_value = {"workspaces": [{"id": "ws-1"}, {"id": "ws-2"}]}
        mock_request.return_value = workspaces
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_applications()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 400)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("error"), "workspace_required")
        self.assertEqual(len(body.get("candidate_workspaces") or []), 2)
        self.assertEqual(len(mock_request.call_args_list), 1)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_resolves_default_user_workspace_by_intent(self, mock_request: mock.Mock) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.json.return_value = {
            "workspaces": [
                {"id": "ws-system", "slug": "platform-builder", "workspace_role": "system_platform"},
                {"id": "ws-user", "slug": "xyence", "workspace_role": "default_user"},
            ]
        }
        applications = mock.Mock()
        applications.status_code = 200
        applications.json.return_value = {"applications": []}
        mock_request.side_effect = [workspaces, applications]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.list_applications()
        self.assertTrue(result["ok"])
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "ws-user")
        second_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(second_kwargs["params"], {"workspace_id": "ws-user"})

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_configured_workspace_rejected_when_not_accessible(self, mock_request: mock.Mock) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.json.return_value = {
            "workspaces": [
                {"id": "ws-allowed", "workspace_role": "default_user"},
                {"id": "ws-system", "workspace_role": "system_platform"},
            ]
        }
        applications = mock.Mock()
        applications.status_code = 200
        applications.json.return_value = {"applications": []}
        mock_request.side_effect = [workspaces, applications]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
                default_workspace_id="ws-denied",
            )
        )
        result = adapter.list_applications()
        self.assertTrue(result["ok"])
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("resolved_workspace_id"), "ws-allowed")
        self.assertEqual(adapter._resolved_default_workspace_id, "ws-allowed")
        self.assertEqual(len(mock_request.call_args_list), 2)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_application_change_session_workflow_create_stage_preview_validate_commit(self, mock_request: mock.Mock) -> None:
        create = mock.Mock()
        create.status_code = 201
        create.json.return_value = {"session_id": "sess-1", "status": "created"}

        stage = mock.Mock()
        stage.status_code = 200
        stage.json.return_value = {"status": "staged", "preview_url": "https://preview.example.com/sess-1"}

        preview = mock.Mock()
        preview.status_code = 200
        preview.json.return_value = {"status": "preview_ready", "preview_urls": ["https://preview.example.com/sess-1"]}

        validate = mock.Mock()
        validate.status_code = 200
        validate.json.return_value = {"status": "validated", "next_allowed_actions": ["commit_application_change_session"]}

        commit = mock.Mock()
        commit.status_code = 200
        commit.json.return_value = {"status": "committed", "commit_sha": "abcdef0123456789"}

        mock_request.side_effect = [create, stage, preview, validate, commit]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        create_result = adapter.create_application_change_session(application_id="app-1", payload={"title": "Session 1"})
        stage_result = adapter.stage_apply_application_change_session(application_id="app-1", session_id="sess-1")
        preview_result = adapter.prepare_preview_application_change_session(application_id="app-1", session_id="sess-1")
        validate_result = adapter.validate_application_change_session(application_id="app-1", session_id="sess-1")
        commit_result = adapter.commit_application_change_session(application_id="app-1", session_id="sess-1")

        self.assertTrue(create_result["ok"])
        self.assertEqual((create_result.get("response") or {}).get("current_status"), "created")
        self.assertTrue(stage_result["ok"])
        self.assertEqual((stage_result.get("response") or {}).get("preview_urls"), ["https://preview.example.com/sess-1"])
        self.assertTrue(preview_result["ok"])
        self.assertEqual((preview_result.get("response") or {}).get("current_status"), "preview_ready")
        self.assertTrue(validate_result["ok"])
        self.assertIn("commit_application_change_session", (validate_result.get("response") or {}).get("next_allowed_actions") or [])
        self.assertTrue(commit_result["ok"])
        self.assertEqual((commit_result.get("response") or {}).get("commit_shas"), ["abcdef0123456789"])

        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions", urls)
        self.assertEqual(
            urls.count("http://xyn.local:8001/xyn/api/change-sessions/sess-1/control/actions"),
            4,
        )
        action_bodies = [call.kwargs.get("json") for call in mock_request.call_args_list if call.kwargs.get("json")]
        operations = [str(body.get("operation")) for body in action_bodies if isinstance(body, dict) and body.get("operation")]
        self.assertEqual(operations, ["stage_apply", "prepare_preview", "validate", "commit"])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_get_application_change_session_plan_prefers_post_plan_route(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"status": "draft", "session_id": "sess-1"}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_application_change_session_plan(application_id="app-1", session_id="sess-1")

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs.get("method"), "POST")
        self.assertEqual(
            kwargs.get("url"),
            "http://xyn.local:8001/xyn/api/change-sessions/sess-1/plan",
        )
        self.assertEqual(kwargs.get("json"), {})

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_change_session_status_reconciles_stale_queued_when_runtime_completed(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": "queued",
            "runtime_runs": [
                {"id": "run-1", "status": "completed"},
            ],
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_application_change_session_plan(application_id="app-1", session_id="sess-1")
        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("current_status") or ""), "completed")
        historical = body.get("historical_status") if isinstance(body.get("historical_status"), dict) else {}
        self.assertEqual(str(historical.get("control_reported_status") or ""), "queued")
        self.assertTrue(bool(historical.get("status_reconciled")))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_get_change_session_commits_falls_back_to_control_commit_evidence(self, mock_request: mock.Mock) -> None:
        missing = mock.Mock()
        missing.status_code = 404
        missing.json.return_value = {"detail": "Not Found"}
        missing.headers = {"content-type": "application/json"}

        control = mock.Mock()
        control.status_code = 200
        control.json.return_value = {
            "status": "staged",
            "commit_sha": "abc123def456",
            "changed_files": ["backend/xyn_orchestrator/xyn_api.py"],
        }
        control.headers = {"content-type": "application/json"}

        mock_request.side_effect = [missing, control]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_application_change_session_commits(application_id="app-1", session_id="sess-1")
        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertIn("abc123def456", body.get("commit_shas") or [])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_application_change_session_workflow_promote_evidence_rollback(self, mock_request: mock.Mock) -> None:
        promote = mock.Mock()
        promote.status_code = 200
        promote.json.return_value = {
            "status": "promoted",
            "promotion_evidence_id": "pe-1",
            "merge_commit_sha": "0123456789abcdef",
        }

        evidence = mock.Mock()
        evidence.status_code = 200
        evidence.json.return_value = {"items": [{"id": "pe-1", "type": "promotion_evidence"}], "status": "available"}

        rollback = mock.Mock()
        rollback.status_code = 200
        rollback.json.return_value = {"status": "rolled_back", "evidence_id": "rb-1"}

        mock_request.side_effect = [promote, evidence, rollback]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        promote_result = adapter.promote_application_change_session(application_id="app-1", session_id="sess-1")
        evidence_result = adapter.get_application_change_session_promotion_evidence(application_id="app-1", session_id="sess-1")
        rollback_result = adapter.rollback_application_change_session(application_id="app-1", session_id="sess-1")

        self.assertTrue(promote_result["ok"])
        self.assertEqual((promote_result.get("response") or {}).get("promotion_evidence_ids"), ["pe-1"])
        self.assertEqual((promote_result.get("response") or {}).get("commit_shas"), ["0123456789abcdef"])

        self.assertTrue(evidence_result["ok"])
        self.assertEqual((evidence_result.get("response") or {}).get("promotion_evidence_ids"), ["pe-1"])

        self.assertTrue(rollback_result["ok"])
        self.assertEqual((rollback_result.get("response") or {}).get("promotion_evidence_ids"), ["rb-1"])

        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertEqual(urls[0], "http://xyn.local:8001/xyn/api/change-sessions/sess-1/control/actions")
        self.assertEqual(urls[1], "http://xyn.local:8001/xyn/api/change-sessions/sess-1/promotion-evidence")
        self.assertEqual(urls[2], "http://xyn.local:8001/xyn/api/change-sessions/sess-1/control/actions")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_promotion_evidence_falls_back_to_control_when_route_unavailable(self, mock_request: mock.Mock) -> None:
        missing = mock.Mock()
        missing.status_code = 404
        missing.json.return_value = {"detail": "Not Found"}
        missing.headers = {"content-type": "application/json"}

        control = mock.Mock()
        control.status_code = 200
        control.json.return_value = {
            "status": "promoted",
            "promotion_evidence_id": "pe-99",
            "merge_commit_sha": "feedfacefeedface",
        }
        control.headers = {"content-type": "application/json"}

        mock_request.side_effect = [missing, control]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_application_change_session_promotion_evidence(application_id="app-1", session_id="sess-1")
        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertIn("pe-99", body.get("promotion_evidence_ids") or [])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_compact_preview_status_isolated_preview_success(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": "preview_ready",
            "preview_urls": ["https://preview.example.com/sess-1"],
            "isolated_session_preview_requested": True,
            "session_build": {"status": "ready", "reason": ""},
            "compose_project": "xyn-preview-sess-1",
            "runtime_target_ids": ["rt-1"],
            "artifacts": [{"artifact_id": "a1", "artifact_slug": "xyn-api", "ready": True, "status": "ready"}],
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_application_change_session_preview_status(application_id="app-1", session_id="sess-1")
        self.assertTrue(result["ok"])
        preview = ((result.get("response") or {}).get("preview") or {})
        self.assertEqual(preview.get("preview_status"), "preview_ready")
        self.assertEqual(preview.get("primary_url"), "https://preview.example.com/sess-1")
        self.assertTrue(preview.get("isolated_session_preview_requested"))
        self.assertEqual(((preview.get("session_build") or {}).get("status")), "ready")
        self.assertEqual(preview.get("compose_project"), "xyn-preview-sess-1")
        self.assertEqual(preview.get("runtime_target_ids"), ["rt-1"])
        readiness = preview.get("artifact_readiness") or {}
        self.assertEqual(readiness.get("ready_count"), 1)
        self.assertFalse(preview.get("used_existing_runtime_fallback"))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_compact_preview_status_fallback_reuse_success(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": "preview_ready",
            "preview_url": "https://preview.example.com/reused",
            "session_build": {"status": "ready"},
            "used_existing_runtime": True,
            "fallback_reason": "isolated_provision_skipped",
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_application_change_session_preview_status(application_id="app-1", session_id="sess-1")
        self.assertTrue(result["ok"])
        preview = ((result.get("response") or {}).get("preview") or {})
        self.assertTrue(preview.get("used_existing_runtime_fallback"))
        self.assertEqual(preview.get("existing_runtime_fallback_reason"), "isolated_provision_skipped")
        self.assertEqual(preview.get("primary_url"), "https://preview.example.com/reused")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_compact_preview_status_provision_failure_normalized(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": "preview_failed",
            "session_build": {"status": "failed", "reason": "preview provision failed: compose up error"},
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_application_change_session_preview_status(application_id="app-1", session_id="sess-1")
        self.assertTrue(result["ok"])
        reason = (((result.get("response") or {}).get("preview") or {}).get("session_build") or {}).get("reason")
        self.assertEqual(reason, "session_preview_provision_failed")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_compact_preview_status_runtime_health_failure_normalized(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "status": "preview_failed",
            "session_build": {"status": "failed", "reason": "preview environment unavailable: runtime health check failed"},
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_application_change_session_preview_status(application_id="app-1", session_id="sess-1")
        self.assertTrue(result["ok"])
        reason = (((result.get("response") or {}).get("preview") or {}).get("session_build") or {}).get("reason")
        self.assertEqual(reason, "preview_environment_unavailable")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_decomposition_campaign_end_to_end_mocked_flow(self, mock_request: mock.Mock) -> None:
        create = mock.Mock()
        create.status_code = 201
        create.json.return_value = {
            "session_id": "sess-decomp-1",
            "status": "created",
            "decomposition_campaign": {
                "kind": "xyn_api_decomposition",
                "target_source_files": ["xyn_orchestrator/xyn_api.py"],
                "extraction_seams": ["api.v1.applications"],
                "required_test_suites": ["backend_fastapi_routes", "orchestrator_unit"],
            },
        }
        stage = mock.Mock()
        stage.status_code = 200
        stage.json.return_value = {
            "status": "staged",
            "changed_files": [
                "xyn_orchestrator/xyn_api.py",
                "xyn_orchestrator/api/applications.py",
            ],
            "test_recommendations": ["backend_fastapi_routes", "orchestrator_unit"],
        }
        runtime_list = mock.Mock()
        runtime_list.status_code = 200
        runtime_list.json.return_value = {
            "runtime_runs": [
                {"id": "run-77", "status": "completed", "worker_type": "codex", "summary": "apply patch complete"}
            ]
        }
        runtime_get = mock.Mock()
        runtime_get.status_code = 200
        runtime_get.json.return_value = {
            "id": "run-77",
            "status": "completed",
            "worker_type": "codex",
            "summary": "run completed",
            "outputs": {"patch": "artifact://patch.diff", "report": "artifact://report.md"},
        }
        runtime_logs = mock.Mock()
        runtime_logs.status_code = 200
        runtime_logs.json.return_value = {"steps": [{"id": "step-1", "name": "pytest", "status": "completed", "summary": "all pass"}]}
        runtime_artifacts = mock.Mock()
        runtime_artifacts.status_code = 200
        runtime_artifacts.json.return_value = {"artifacts": [{"id": "a-patch", "name": "patch.diff", "kind": "patch"}]}
        preview = mock.Mock()
        preview.status_code = 200
        preview.json.return_value = {
            "status": "preview_ready",
            "preview_urls": ["https://preview.example.com/sess-decomp-1"],
            "isolated_session_preview_requested": True,
            "session_build": {"status": "ready", "reason": ""},
            "runtime_target_ids": ["rt-7"],
        }
        validate = mock.Mock()
        validate.status_code = 200
        validate.json.return_value = {"status": "validated"}
        commit = mock.Mock()
        commit.status_code = 200
        commit.json.return_value = {
            "status": "committed",
            "commit_sha": "deadbeefdeadbeef",
            "changed_files": [
                "xyn_orchestrator/xyn_api.py",
                "xyn_orchestrator/api/applications.py",
            ],
        }
        promote = mock.Mock()
        promote.status_code = 200
        promote.json.return_value = {
            "status": "promoted",
            "merge_commit_sha": "feedfacefeedface",
            "promotion_evidence_id": "pe-77",
        }

        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            if method == "POST" and url.endswith("/xyn/api/applications/app-1/change-sessions"):
                return create
            if method == "POST" and url.endswith("/xyn/api/change-sessions/sess-decomp-1/control/actions"):
                payload = kwargs.get("json") if isinstance(kwargs.get("json"), dict) else {}
                op = str(payload.get("operation") or "").strip().lower()
                if op == "stage_apply":
                    return stage
                if op == "prepare_preview":
                    return preview
                if op == "validate":
                    return validate
                if op == "commit":
                    return commit
                if op == "promote":
                    return promote
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-decomp-1/runtime-runs"):
                return runtime_list
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-decomp-1/runtime-runs/run-77"):
                return runtime_get
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-decomp-1/runtime-runs/run-77/logs"):
                return runtime_logs
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-decomp-1/runtime-runs/run-77/artifacts"):
                return runtime_artifacts
            fallback = mock.Mock()
            fallback.status_code = 404
            fallback.json.return_value = {"detail": "Not Found"}
            return fallback

        mock_request.side_effect = _fake_request

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        create_result = adapter.create_decomposition_campaign(
            application_id="app-1",
            target_source_files=["xyn_orchestrator/xyn_api.py"],
            extraction_seams=["api.v1.applications"],
            moved_handlers_modules=["xyn_orchestrator/api/applications.py"],
            required_test_suites=["backend_fastapi_routes", "orchestrator_unit"],
        )
        stage_result = adapter.stage_apply_application_change_session(application_id="app-1", session_id="sess-decomp-1")
        runs_result = adapter.list_runtime_runs(application_id="app-1", session_id="sess-decomp-1")
        run_result = adapter.get_runtime_run(run_id="run-77", application_id="app-1", session_id="sess-decomp-1")
        logs_result = adapter.get_runtime_run_logs(run_id="run-77", application_id="app-1", session_id="sess-decomp-1")
        artifacts_result = adapter.get_runtime_run_artifacts(run_id="run-77", application_id="app-1", session_id="sess-decomp-1")
        preview_result = adapter.prepare_preview_application_change_session(application_id="app-1", session_id="sess-decomp-1")
        validate_result = adapter.validate_application_change_session(application_id="app-1", session_id="sess-decomp-1")
        commit_result = adapter.commit_application_change_session(application_id="app-1", session_id="sess-decomp-1")
        promote_result = adapter.promote_application_change_session(application_id="app-1", session_id="sess-decomp-1")

        self.assertTrue(create_result["ok"])
        campaign = (create_result.get("response") or {}).get("decomposition_campaign") or {}
        self.assertEqual(campaign.get("kind"), "xyn_api_decomposition")
        self.assertEqual(campaign.get("target_source_files"), ["xyn_orchestrator/xyn_api.py"])

        self.assertTrue(stage_result["ok"])
        stage_body = stage_result.get("response") if isinstance(stage_result.get("response"), dict) else {}
        self.assertIn(str(stage_body.get("current_status") or ""), {"staged", "ok", "completed"})

        self.assertTrue(runs_result["ok"])
        self.assertEqual((runs_result.get("response") or {}).get("count"), 1)
        self.assertTrue(run_result["ok"])
        self.assertEqual((run_result.get("response") or {}).get("current_status"), "completed")
        self.assertTrue(logs_result["ok"])
        self.assertTrue(artifacts_result["ok"])
        self.assertTrue(preview_result["ok"])
        self.assertEqual((((preview_result.get("response") or {}).get("preview") or {}).get("preview_status")), "preview_ready")
        self.assertTrue(validate_result["ok"])
        self.assertTrue(commit_result["ok"])
        self.assertIn("deadbeefdeadbeef", (commit_result.get("response") or {}).get("commit_shas") or [])
        self.assertIn("xyn_orchestrator/xyn_api.py", (commit_result.get("response") or {}).get("changed_files") or [])
        self.assertTrue(promote_result["ok"])
        self.assertIn("feedfacefeedface", (promote_result.get("response") or {}).get("commit_shas") or [])

        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions", urls)
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-decomp-1/runtime-runs", urls)
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-decomp-1/runtime-runs/run-77", urls)

        action_bodies = [call.kwargs.get("json") for call in mock_request.call_args_list if isinstance(call.kwargs.get("json"), dict)]
        operations = [str(body.get("operation")) for body in action_bodies if body.get("operation")]
        self.assertEqual(operations, ["stage_apply", "prepare_preview", "validate", "commit", "promote"])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_inspection_success(self, mock_request: mock.Mock) -> None:
        list_runs = mock.Mock()
        list_runs.status_code = 200
        list_runs.json.return_value = {
            "runtime_runs": [
                {
                    "id": "run-1",
                    "status": "completed",
                    "worker_type": "codex",
                    "summary": "run completed",
                    "repo_key": "xyn-platform",
                    "branch": "feature/x",
                    "target_branch": "develop",
                }
            ]
        }

        get_run = mock.Mock()
        get_run.status_code = 200
        get_run.json.return_value = {
            "id": "run-1",
            "status": "completed",
            "worker_type": "codex",
            "summary": "run completed",
            "repo_key": "xyn-platform",
            "branch": "feature/x",
            "target_branch": "develop",
        }

        get_logs = mock.Mock()
        get_logs.status_code = 200
        get_logs.json.return_value = {"logs": [{"step_id": "s1", "name": "test", "status": "completed", "summary": "ok"}]}

        get_artifacts = mock.Mock()
        get_artifacts.status_code = 200
        get_artifacts.json.return_value = {"artifacts": [{"id": "a1", "name": "report.md", "kind": "report"}]}

        get_commands = mock.Mock()
        get_commands.status_code = 200
        get_commands.json.return_value = {"commands": [{"id": "c1", "command": "pytest -q", "status": "completed"}]}

        mock_request.side_effect = [list_runs, get_run, get_logs, get_artifacts, get_commands]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        runs = adapter.list_runtime_runs(application_id="app-1", session_id="sess-1")
        run = adapter.get_runtime_run(run_id="run-1", application_id="app-1", session_id="sess-1")
        logs = adapter.get_runtime_run_logs(run_id="run-1", application_id="app-1", session_id="sess-1")
        artifacts = adapter.get_runtime_run_artifacts(run_id="run-1", application_id="app-1", session_id="sess-1")
        commands = adapter.get_runtime_run_commands(run_id="run-1", application_id="app-1", session_id="sess-1")

        self.assertTrue(runs["ok"])
        self.assertEqual((runs.get("response") or {}).get("count"), 1)
        self.assertTrue(run["ok"])
        self.assertEqual((run.get("response") or {}).get("current_status"), "completed")
        repo_target = (run.get("response") or {}).get("repo_target") or {}
        self.assertEqual(repo_target.get("repo_key"), "xyn-platform")
        self.assertEqual(repo_target.get("branch"), "feature/x")
        self.assertTrue(logs["ok"])
        self.assertEqual((logs.get("response") or {}).get("count"), 1)
        self.assertTrue(artifacts["ok"])
        self.assertEqual((artifacts.get("response") or {}).get("count"), 1)
        self.assertTrue(commands["ok"])
        self.assertEqual((commands.get("response") or {}).get("count"), 1)

        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn(
            "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/runtime-runs",
            urls,
        )
        self.assertIn(
            "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/runtime-runs/run-1",
            urls,
        )

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_supports_artifact_scope(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 201
        response.json.return_value = {
            "created": True,
            "application_id": "app-artifact-1",
            "artifact_id": "art-1",
            "artifact_slug": "xyn-api",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-artifact-1",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "session": {
                "id": "sess-1",
                "application_id": "app-artifact-1",
                "scope_type": "artifact",
                "scope": {
                    "scope_type": "artifact",
                    "application_id": "app-artifact-1",
                    "artifact_id": "art-1",
                    "artifact_slug": "xyn-api",
                    "workspace_id": "ws-1",
                },
            },
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            artifact_id="art-1",
            artifact_slug="xyn-api",
            workspace_id="ws-1",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertTrue(result.get("ok"))
        body = result.get("response") or {}
        self.assertEqual(body.get("scope_type"), "artifact")
        scope = body.get("scope") or {}
        self.assertEqual(scope.get("artifact_id"), "art-1")
        self.assertEqual(scope.get("artifact_slug"), "xyn-api")
        self.assertEqual(scope.get("workspace_id"), "ws-1")
        called = mock_request.call_args.kwargs
        self.assertEqual(called.get("method"), "POST")
        self.assertEqual(called.get("url"), "http://xyn.local:8001/xyn/api/change-sessions")
        payload = called.get("json") or {}
        self.assertEqual(payload.get("artifact_id"), "art-1")
        self.assertEqual(payload.get("artifact_slug"), "xyn-api")
        self.assertEqual(payload.get("workspace_id"), "ws-1")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_application_change_session_forwards_artifact_source(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 201
        response.json.return_value = {"session": {"id": "sess-1"}, "application_id": "app-1"}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        artifact_source = {"manifest_source": "s3://bundle/manifest.json", "artifact_slug": "deal-finder"}
        result = adapter.create_application_change_session(
            application_id="app-1",
            artifact_source=artifact_source,
            payload={"title": "Session 1"},
        )
        self.assertTrue(result.get("ok"))
        called = mock_request.call_args.kwargs
        self.assertEqual(called.get("url"), "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions")
        request_payload = called.get("json") or {}
        self.assertEqual(request_payload.get("artifact_source"), artifact_source)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_forwards_artifact_source(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 201
        response.json.return_value = {
            "created": True,
            "application_id": "app-artifact-1",
            "scope_type": "artifact",
            "scope": {"scope_type": "artifact", "workspace_id": "ws-1", "artifact_slug": "deal-finder"},
            "session": {"id": "sess-1", "application_id": "app-artifact-1"},
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        artifact_source = {
            "manifest_source": "s3://bundle/manifest.json",
            "artifact_slug": "deal-finder",
            "artifact_type": "application",
        }
        result = adapter.create_decomposition_campaign(
            artifact_slug="deal-finder",
            workspace_id="ws-1",
            artifact_source=artifact_source,
            target_source_files=["services/deal_finder/main.py"],
        )
        self.assertTrue(result.get("ok"))
        called = mock_request.call_args.kwargs
        payload = called.get("json") or {}
        self.assertEqual(payload.get("artifact_source"), artifact_source)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_application_change_session_explicit_remote_target_auto_resolves_initial_prompt(
        self, mock_request: mock.Mock
    ) -> None:
        create_response = {
            "session": {"id": "sess-1"},
            "application_id": "app-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-1",
                "artifact_slug": "deal-finder",
                "workspace_id": "ws-1",
            },
            "control": {
                "session": {
                    "planning": {
                        "pending_prompt": {
                            "id": "prompt-1",
                            "expected_response_kind": "option_set",
                            "response_schema": {
                                "type": "object",
                                "required": ["selected_option_id"],
                                "properties": {"selected_option_id": {"type": "string"}},
                            },
                            "option_set": {
                                "options": [
                                    {"id": "opt-deal-finder", "label": "Deal Finder", "artifact_slug": "deal-finder"},
                                    {"id": "opt-xyn-api", "label": "xyn-api", "artifact_slug": "xyn-api"},
                                ]
                            },
                        }
                    }
                }
            },
        }
        pending_control = {
            "application_id": "app-1",
            "session_id": "sess-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-1",
                "artifact_slug": "deal-finder",
                "workspace_id": "ws-1",
            },
            "control": create_response["control"],
        }
        resolved_control = {
            "application_id": "app-1",
            "session_id": "sess-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-1",
                "artifact_slug": "deal-finder",
                "workspace_id": "ws-1",
            },
            "control": {"session": {"planning": {}}},
        }
        action_payloads: list[dict[str, Any]] = []
        control_reads = {"count": 0}

        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "POST" and url.endswith("/xyn/api/applications/app-1/change-sessions"):
                response.status_code = 201
                response.json.return_value = create_response
                return response
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control"):
                control_reads["count"] += 1
                response.status_code = 200
                response.json.return_value = pending_control if control_reads["count"] <= 2 else resolved_control
                return response
            if method == "POST" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control/actions"):
                action_payloads.append(dict(kwargs.get("json") or {}))
                response.status_code = 200
                response.json.return_value = {"ok": True}
                return response
            raise AssertionError(f"Unexpected request: {method} {url}")

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.create_application_change_session(
            application_id="app-1",
            artifact_source={"artifact_slug": "deal-finder", "manifest_source": "s3://bundle/manifest.json"},
            payload={"title": "Deal Finder session"},
        )

        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        planner_prompt = body.get("planner_prompt") if isinstance(body.get("planner_prompt"), dict) else {}
        self.assertFalse(bool(planner_prompt.get("pending")))
        auto_resolution = body.get("auto_prompt_resolution") if isinstance(body.get("auto_prompt_resolution"), dict) else {}
        self.assertTrue(auto_resolution.get("applied"))
        self.assertEqual(str(auto_resolution.get("selected_option_id") or ""), "opt-deal-finder")
        self.assertEqual(len(action_payloads), 1)
        submitted = action_payloads[0]
        self.assertEqual(submitted.get("operation"), "respond_to_planner_prompt")
        self.assertEqual(((submitted.get("response") or {}).get("selected_option_id")), "opt-deal-finder")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_change_session_with_scope_explicit_local_target_auto_resolves_initial_prompt(
        self, mock_request: mock.Mock
    ) -> None:
        create_response = {
            "session": {"id": "sess-local-1"},
            "application_id": "app-local",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-local",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "control": {
                "session": {
                    "planning": {
                        "pending_prompt": {
                            "id": "prompt-local-1",
                            "expected_response_kind": "option_set",
                            "response_schema": {
                                "type": "object",
                                "required": ["selected_option_id"],
                                "properties": {"selected_option_id": {"type": "string"}},
                            },
                            "option_set": {
                                "options": [
                                    {"id": "opt-xyn-api", "label": "xyn-api", "artifact_slug": "xyn-api"},
                                    {"id": "opt-xyn-ui", "label": "xyn-ui", "artifact_slug": "xyn-ui"},
                                ]
                            },
                        }
                    }
                }
            },
        }
        pending_control = {
            "application_id": "app-local",
            "session_id": "sess-local-1",
            "scope_type": "artifact",
            "scope": create_response["scope"],
            "control": create_response["control"],
        }
        resolved_control = {
            "application_id": "app-local",
            "session_id": "sess-local-1",
            "scope_type": "artifact",
            "scope": create_response["scope"],
            "control": {"session": {"planning": {}}},
        }
        action_payloads: list[dict[str, Any]] = []
        control_reads = {"count": 0}

        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "POST" and url.endswith("/xyn/api/change-sessions"):
                response.status_code = 201
                response.json.return_value = create_response
                return response
            if method == "GET" and url.endswith("/xyn/api/applications/app-local/change-sessions/sess-local-1/control"):
                control_reads["count"] += 1
                response.status_code = 200
                response.json.return_value = pending_control if control_reads["count"] <= 2 else resolved_control
                return response
            if method == "POST" and url.endswith("/xyn/api/applications/app-local/change-sessions/sess-local-1/control/actions"):
                action_payloads.append(dict(kwargs.get("json") or {}))
                response.status_code = 200
                response.json.return_value = {"ok": True}
                return response
            raise AssertionError(f"Unexpected request: {method} {url}")

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.create_change_session_with_scope(
            artifact_slug="xyn-api",
            workspace_id="ws-1",
            payload={"title": "Core API update"},
        )

        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        planner_prompt = body.get("planner_prompt") if isinstance(body.get("planner_prompt"), dict) else {}
        self.assertFalse(bool(planner_prompt.get("pending")))
        self.assertEqual(len(action_payloads), 1)
        submitted = action_payloads[0]
        self.assertEqual(((submitted.get("response") or {}).get("selected_option_id")), "opt-xyn-api")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_application_change_session_without_explicit_target_keeps_pending_prompt(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 201
        response.headers = {}
        response.text = ""
        response.json.return_value = {
            "session": {"id": "sess-amb-1"},
            "application_id": "app-1",
            "control": {
                "session": {
                    "planning": {
                        "pending_prompt": {
                            "id": "prompt-amb-1",
                            "expected_response_kind": "option_set",
                            "option_set": {"options": [{"id": "opt-a", "label": "Artifact A"}]},
                        }
                    }
                }
            },
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.create_application_change_session(application_id="app-1", payload={"title": "General planning"})

        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        planner_prompt = body.get("planner_prompt") if isinstance(body.get("planner_prompt"), dict) else {}
        self.assertTrue(bool(planner_prompt.get("pending")))
        self.assertEqual(mock_request.call_count, 1)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_change_session_with_explicit_target_and_exploration_request_keeps_prompt(
        self, mock_request: mock.Mock
    ) -> None:
        response = mock.Mock()
        response.status_code = 201
        response.headers = {}
        response.text = ""
        response.json.return_value = {
            "session": {"id": "sess-exp-1"},
            "application_id": "app-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-1",
                "artifact_slug": "deal-finder",
                "workspace_id": "ws-1",
            },
            "control": {
                "session": {
                    "planning": {
                        "pending_prompt": {
                            "id": "prompt-exp-1",
                            "expected_response_kind": "option_set",
                            "option_set": {
                                "options": [
                                    {"id": "opt-deal-finder", "label": "Deal Finder", "artifact_slug": "deal-finder"},
                                    {"id": "opt-xyn-ui", "label": "xyn-ui", "artifact_slug": "xyn-ui"},
                                ]
                            },
                        }
                    }
                }
            },
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.create_change_session_with_scope(
            artifact_slug="deal-finder",
            workspace_id="ws-1",
            payload={"explore_artifacts": True},
        )

        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        planner_prompt = body.get("planner_prompt") if isinstance(body.get("planner_prompt"), dict) else {}
        self.assertTrue(bool(planner_prompt.get("pending")))
        self.assertEqual(mock_request.call_count, 1)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_artifact_scope_classifies_missing_artifact(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 404
        response.json.return_value = {
            "error": "artifact not found",
            "blocked_reason": "artifact_not_found",
            "error_classification": "artifact_not_found",
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            artifact_slug="xyn-api",
            workspace_id="ws-1",
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_classification"), "artifact_not_found")
        body = result.get("response") or {}
        self.assertEqual(body.get("blocked_reason"), "artifact_not_found")
        self.assertEqual(body.get("scope_type"), "artifact")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_prefers_artifact_not_found_over_binding_rotated_classification(
        self, mock_request: mock.Mock
    ) -> None:
        response = mock.Mock()
        response.status_code = 404
        response.headers = {}
        response.json.return_value = {
            "error": "artifact not found",
            "blocked_reason": "artifact_not_found",
            "detail": "Artifact not found in workspace",
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            artifact_id="art-404",
            workspace_id="ws-1",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(str(result.get("error_classification") or ""), "artifact_not_found")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("blocked_reason") or ""), "artifact_not_found")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_application_and_artifact_uses_unified_scope_endpoint(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 409
        response.json.return_value = {
            "error": "application/artifact scope mismatch",
            "blocked_reason": "contract_mismatch",
            "error_classification": "contract_mismatch",
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            application_id="app-1",
            artifact_slug="xyn-api",
            workspace_id="ws-1",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("error_classification"), "contract_mismatch")
        called = mock_request.call_args.kwargs
        self.assertEqual(called.get("url"), "http://xyn.local:8001/xyn/api/change-sessions")
        payload = called.get("json") or {}
        self.assertEqual(payload.get("application_id"), "app-1")
        self.assertEqual(payload.get("artifact_slug"), "xyn-api")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_without_explicit_artifact_allows_backend_inference(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 201
        response.json.return_value = {
            "created": True,
            "application_id": "app-artifact-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-artifact-1",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "session": {"id": "sess-1", "application_id": "app-artifact-1"},
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            workspace_id="ws-1",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertTrue(result.get("ok"))
        called = mock_request.call_args.kwargs
        self.assertEqual(called.get("url"), "http://xyn.local:8001/xyn/api/change-sessions")
        payload = called.get("json") or {}
        self.assertEqual(payload.get("workspace_id"), "ws-1")
        self.assertNotIn("artifact_id", payload)
        self.assertNotIn("artifact_slug", payload)

    @mock.patch("core.mcp.xyn_api_adapter.XynApiAdapter._resolve_workspace_for_request")
    @mock.patch("core.mcp.xyn_api_adapter.XynApiAdapter._resolve_artifact_record")
    @mock.patch("core.mcp.xyn_api_adapter.XynApiAdapter.get_application")
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_recovers_when_artifact_id_is_passed_as_application_id(
        self,
        mock_request: mock.Mock,
        mock_get_application: mock.Mock,
        mock_resolve_artifact: mock.Mock,
        mock_resolve_workspace: mock.Mock,
    ) -> None:
        mock_get_application.return_value = {
            "ok": False,
            "status_code": 404,
            "response": {"error": "application not found"},
        }
        mock_resolve_artifact.return_value = {"id": "art-1", "slug": "xyn-api", "title": "xyn-api"}
        mock_resolve_workspace.return_value = {"ok": True, "workspace_id": "ws-1"}
        create = mock.Mock()
        create.status_code = 201
        create.headers = {}
        create.json.return_value = {
            "created": True,
            "application_id": "app-artifact-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-artifact-1",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "session": {"id": "sess-1", "application_id": "app-artifact-1"},
        }
        mock_request.return_value = create
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            application_id="art-1",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertTrue(result.get("ok"))
        create_call = mock_request.call_args.kwargs
        self.assertEqual(create_call.get("url"), "http://xyn.local:8001/xyn/api/change-sessions")
        payload = create_call.get("json") or {}
        self.assertEqual(payload.get("artifact_id"), "art-1")
        self.assertEqual(payload.get("artifact_slug"), "xyn-api")
        self.assertEqual(payload.get("workspace_id"), "ws-1")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_resolves_workspace_when_omitted_for_artifact_scope(
        self, mock_request: mock.Mock
    ) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.headers = {}
        workspaces.json.return_value = {
            "workspaces": [{"id": "ws-1", "slug": "development", "name": "Development"}]
        }
        create = mock.Mock()
        create.status_code = 201
        create.headers = {}
        create.json.return_value = {
            "created": True,
            "application_id": "app-artifact-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-artifact-1",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "session": {"id": "sess-1", "application_id": "app-artifact-1"},
        }
        mock_request.side_effect = [workspaces, create]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            artifact_slug="xyn-api",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(mock_request.call_count, 2)
        first_call = mock_request.call_args_list[0].kwargs
        second_call = mock_request.call_args_list[1].kwargs
        self.assertEqual(first_call.get("method"), "GET")
        self.assertEqual(first_call.get("url"), "http://xyn.local:8001/xyn/api/workspaces")
        self.assertEqual(second_call.get("method"), "POST")
        self.assertEqual(second_call.get("url"), "http://xyn.local:8001/xyn/api/change-sessions")
        payload = second_call.get("json") or {}
        self.assertEqual(payload.get("workspace_id"), "ws-1")
        self.assertEqual(payload.get("artifact_slug"), "xyn-api")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_uses_system_workspace_for_system_artifact_intent(
        self, mock_request: mock.Mock
    ) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.headers = {}
        workspaces.json.return_value = {
            "workspaces": [
                {"id": "ws-user", "slug": "xyence", "workspace_role": "default_user"},
                {"id": "ws-system", "slug": "platform-builder", "workspace_role": "system_platform"},
            ]
        }
        create = mock.Mock()
        create.status_code = 201
        create.headers = {}
        create.json.return_value = {
            "created": True,
            "application_id": "app-artifact-1",
            "scope_type": "artifact",
            "scope": {"scope_type": "artifact", "workspace_id": "ws-system", "artifact_slug": "xyn-api"},
            "session": {"id": "sess-1", "application_id": "app-artifact-1"},
        }
        mock_request.side_effect = [workspaces, create]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            artifact_slug="xyn-api",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertTrue(result.get("ok"))
        second_call = mock_request.call_args_list[1].kwargs
        payload = second_call.get("json") or {}
        self.assertEqual(payload.get("workspace_id"), "ws-system")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_decomposition_campaign_returns_structured_scope_resolution_error_when_workspace_unresolved(
        self, mock_request: mock.Mock
    ) -> None:
        workspaces = mock.Mock()
        workspaces.status_code = 200
        workspaces.headers = {}
        workspaces.json.return_value = {
            "workspaces": [
                {"id": "ws-1", "slug": "development", "name": "Development"},
                {"id": "ws-2", "slug": "staging", "name": "Staging"},
            ]
        }
        mock_request.return_value = workspaces
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_decomposition_campaign(
            artifact_slug="xyn-api",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(int(result.get("status_code") or 0), 400)
        self.assertEqual(str(result.get("error_classification") or ""), "scope_resolution_failed")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("blocked_reason") or ""), "scope_resolution_failed")
        self.assertEqual(str(body.get("error") or ""), "workspace_required")
        self.assertEqual(
            [str((row or {}).get("id") or "") for row in (body.get("candidate_workspaces") or [])],
            ["ws-1", "ws-2"],
        )
        self.assertEqual(mock_request.call_count, 1)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_inspect_change_session_control_supports_session_scoped_route_when_application_unknown(
        self, mock_request: mock.Mock
    ) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "control": {"session": {"id": "sess-1", "status": "draft"}},
            "next_allowed_actions": ["stage_apply_application_change_session"],
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.inspect_change_session_control(application_id="", session_id="sess-1")
        self.assertTrue(result.get("ok"))
        called = mock_request.call_args.kwargs
        self.assertEqual(called.get("url"), "http://xyn.local:8001/xyn/api/change-sessions/sess-1/control")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_decide_checkpoint_supports_session_scoped_route_when_application_unknown(self, mock_request: mock.Mock) -> None:
        decision = mock.Mock()
        decision.status_code = 200
        decision.json.return_value = {"recorded": True}
        mock_request.return_value = decision
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.decide_change_session_checkpoint(
            application_id="",
            session_id="sess-1",
            checkpoint_id="cp-1",
            decision="approved",
        )
        self.assertTrue(result.get("ok"))
        called = mock_request.call_args.kwargs
        self.assertEqual(
            called.get("url"),
            "http://xyn.local:8001/xyn/api/change-sessions/sess-1/checkpoints/cp-1/decision",
        )

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_artifact_scoped_control_read_preserves_scope_after_route_fallback(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 404
        first.json.return_value = {"detail": "Not Found"}
        second = mock.Mock()
        second.status_code = 404
        second.json.return_value = {"detail": "Not Found"}
        third = mock.Mock()
        third.status_code = 200
        third.json.return_value = {
            "session_id": "sess-1",
            "application_id": "app-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-1",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "next_allowed_actions": ["stage_apply_application_change_session"],
            "control": {"session": {"id": "sess-1", "status": "draft"}},
        }
        mock_request.side_effect = [first, second, third]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.inspect_change_session_control(application_id="stale-app", session_id="sess-1")
        self.assertTrue(result.get("ok"))
        body = result.get("response") or {}
        self.assertEqual(body.get("scope_type"), "artifact")
        scope = body.get("scope") or {}
        self.assertEqual(scope.get("artifact_id"), "art-1")
        self.assertEqual(scope.get("artifact_slug"), "xyn-api")
        self.assertEqual(body.get("session_id"), "sess-1")
        self.assertEqual(body.get("next_allowed_actions"), ["stage_apply_application_change_session"])
        urls = [call.kwargs.get("url") for call in mock_request.call_args_list]
        self.assertIn("http://xyn.local:8001/xyn/api/applications/stale-app/change-sessions/sess-1/control", urls)
        self.assertIn("http://xyn.local:8001/xyn/api/change-sessions/sess-1/control", urls)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_artifact_identity_parity_across_registry_source_and_decomposition(self, mock_request: mock.Mock) -> None:
        listed = mock.Mock()
        listed.status_code = 200
        listed.json.return_value = {"artifacts": [{"id": "art-1", "slug": "xyn-api", "title": "xyn-api"}]}
        tree = mock.Mock()
        tree.status_code = 200
        tree.json.return_value = {
            "artifact": {"id": "art-1", "slug": "xyn-api"},
            "source_mode": "resolved_source",
            "files": [{"path": "backend/xyn_orchestrator/xyn_api.py"}],
        }
        create = mock.Mock()
        create.status_code = 201
        create.json.return_value = {
            "application_id": "app-1",
            "session": {"id": "sess-1"},
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-1",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
        }
        mock_request.side_effect = [listed, tree, create]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        artifacts = adapter.list_artifacts()
        artifact_id = ((artifacts.get("response") or {}).get("artifacts") or [{}])[0].get("id")
        self.assertEqual(artifact_id, "art-1")
        source_tree = adapter.get_artifact_source_tree(artifact_id=str(artifact_id))
        self.assertTrue(source_tree.get("ok"))
        campaign = adapter.create_decomposition_campaign(
            artifact_id=str(artifact_id),
            artifact_slug="xyn-api",
            workspace_id="ws-1",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        self.assertTrue(campaign.get("ok"))
        scope = (campaign.get("response") or {}).get("scope") or {}
        self.assertEqual(scope.get("artifact_id"), "art-1")
        self.assertEqual(scope.get("artifact_slug"), "xyn-api")
        create_call = mock_request.call_args_list[-1].kwargs
        payload = create_call.get("json") or {}
        self.assertEqual(payload.get("artifact_id"), "art-1")
        self.assertEqual(payload.get("artifact_slug"), "xyn-api")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_artifact_scoped_stage_apply_without_application_id_uses_session_control_route(
        self, mock_request: mock.Mock
    ) -> None:
        response = mock.Mock()
        response.status_code = 202
        response.json.return_value = {
            "session_id": "sess-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "next_allowed_actions": ["list_runtime_runs"],
            "status": "in_progress",
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.stage_apply_application_change_session(
            session_id="sess-1",
            payload={"dispatch_runtime": True},
        )
        self.assertTrue(result.get("ok"))
        body = result.get("response") or {}
        self.assertEqual(body.get("scope_type"), "artifact")
        scope = body.get("scope") or {}
        self.assertEqual(scope.get("artifact_slug"), "xyn-api")
        called = mock_request.call_args.kwargs
        self.assertEqual(called.get("url"), "http://xyn.local:8001/xyn/api/change-sessions/sess-1/control/actions")

    def test_tool_contract_allows_artifact_scoped_session_operations(self) -> None:
        adapter = mock.Mock()
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        server.tools["create_decomposition_campaign"]["fn"](
            artifact_slug="xyn-api",
            workspace_id="ws-1",
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
        )
        adapter.create_decomposition_campaign.assert_called_with(
            application_id="",
            artifact_id="",
            artifact_slug="xyn-api",
            workspace_id="ws-1",
            artifact_source=None,
            target_source_files=["backend/xyn_orchestrator/xyn_api.py"],
            extraction_seams=None,
            moved_handlers_modules=None,
            required_test_suites=None,
            payload=None,
        )

        server.tools["stage_apply_application_change_session"]["fn"](session_id="sess-1", payload={"dispatch_runtime": True})
        adapter.stage_apply_application_change_session.assert_called_with(
            application_id="",
            session_id="sess-1",
            payload={"dispatch_runtime": True},
        )

    def test_tool_contract_includes_remote_artifact_discovery_tools_and_artifact_source_schema(self) -> None:
        adapter = mock.Mock()
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        self.assertIn("list_remote_artifact_sources", server.tools)
        self.assertIn("search_remote_artifact_catalog", server.tools)
        self.assertIn("list_remote_artifact_candidates", server.tools)
        app_create_signature = inspect.signature(server.tools["create_application_change_session"]["fn"])
        self.assertIn("artifact_source", app_create_signature.parameters)
        create_signature = inspect.signature(server.tools["create_decomposition_campaign"]["fn"])
        self.assertIn("artifact_source", create_signature.parameters)
        source_signature = inspect.signature(server.tools["list_remote_artifact_sources"]["fn"])
        self.assertEqual(len(source_signature.parameters), 0)
        search_signature = inspect.signature(server.tools["search_remote_artifact_catalog"]["fn"])
        self.assertIn("query", search_signature.parameters)
        self.assertIn("artifact_type", search_signature.parameters)
        list_signature = inspect.signature(server.tools["list_remote_artifact_candidates"]["fn"])
        self.assertIn("manifest_source", list_signature.parameters)
        self.assertIn("package_source", list_signature.parameters)

    def test_tool_contract_remote_candidate_to_create_campaign_forwards_artifact_source(self) -> None:
        adapter = mock.Mock()
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        server.tools["list_remote_artifact_sources"]["fn"]()
        adapter.list_remote_artifact_sources.assert_called_with()

        server.tools["search_remote_artifact_catalog"]["fn"](
            query="deal finder",
            artifact_type="application",
            limit=20,
        )
        adapter.search_remote_artifact_catalog.assert_called_with(
            query="deal finder",
            artifact_slug="",
            artifact_type="application",
            source_root="",
            limit=20,
            cursor="",
        )

        server.tools["list_remote_artifact_candidates"]["fn"](
            manifest_source="s3://bundle/manifest.json",
            artifact_slug="deal-finder",
            artifact_type="application",
        )
        adapter.list_remote_artifact_candidates.assert_called_with(
            manifest_source="s3://bundle/manifest.json",
            package_source="",
            artifact_slug="deal-finder",
            artifact_type="application",
        )

        source = {
            "manifest_source": "s3://bundle/manifest.json",
            "artifact_slug": "deal-finder",
            "artifact_type": "application",
        }
        server.tools["create_decomposition_campaign"]["fn"](
            artifact_slug="deal-finder",
            workspace_id="ws-1",
            artifact_source=source,
            target_source_files=["services/deal_finder/main.py"],
        )
        adapter.create_decomposition_campaign.assert_called_with(
            application_id="",
            artifact_id="",
            artifact_slug="deal-finder",
            workspace_id="ws-1",
            artifact_source=source,
            target_source_files=["services/deal_finder/main.py"],
            extraction_seams=None,
            moved_handlers_modules=None,
            required_test_suites=None,
            payload=None,
        )

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_decomposition_observability_works_without_application_id(self, mock_request: mock.Mock) -> None:
        control = mock.Mock()
        control.status_code = 200
        control.json.return_value = {
            "session_id": "sess-1",
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "application_id": "app-1",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "next_allowed_actions": ["stage_apply_application_change_session"],
        }
        metrics = mock.Mock()
        metrics.status_code = 200
        metrics.json.return_value = {"metrics": [{"path": "xyn_orchestrator/xyn_api.py", "loc": 1000}], "count": 1}
        analysis = mock.Mock()
        analysis.status_code = 200
        analysis.json.return_value = {
            "source_mode": "resolved_source",
            "route_inventory": {"count": 10},
            "route_inventory_delta": {"changed": 0},
            "summary": {"largest_module": "xyn_orchestrator/xyn_api.py"},
            "warnings": [],
        }
        mock_request.side_effect = [control, metrics, analysis]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_decomposition_observability(session_id="sess-1")
        self.assertTrue(result.get("ok"))
        body = result.get("response") or {}
        self.assertEqual(body.get("scope_type"), "artifact")
        self.assertEqual((body.get("scope") or {}).get("artifact_id"), "art-1")
        self.assertEqual(body.get("artifact_id"), "art-1")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_inspection_failure_surfaces_logs_and_errors(self, mock_request: mock.Mock) -> None:
        get_run = mock.Mock()
        get_run.status_code = 200
        get_run.json.return_value = {
            "id": "run-2",
            "status": "failed",
            "worker_type": "codex",
            "failure_reason": "tests_failed",
            "error": {"message": "pytest failed"},
        }

        get_logs = mock.Mock()
        get_logs.status_code = 200
        get_logs.json.return_value = {
            "steps": [
                {
                    "id": "s2",
                    "name": "pytest",
                    "status": "failed",
                    "summary": "2 tests failed",
                    "error": {"detail": "assertion error"},
                }
            ]
        }

        mock_request.side_effect = [get_run, get_logs]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        run = adapter.get_runtime_run(run_id="run-2")
        logs = adapter.get_runtime_run_logs(run_id="run-2")

        self.assertTrue(run["ok"])
        self.assertEqual((run.get("response") or {}).get("current_status"), "failed")
        self.assertEqual((run.get("response") or {}).get("failure_reason"), "tests_failed")
        self.assertEqual(((run.get("response") or {}).get("error") or {}).get("message"), "pytest failed")
        self.assertTrue(logs["ok"])
        first_log = ((logs.get("response") or {}).get("logs") or [{}])[0]
        self.assertEqual(first_log.get("status"), "failed")
        self.assertEqual(first_log.get("summary"), "2 tests failed")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_binding_preserves_api_prefix(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            if url.endswith("/xyn/api/runs/run-42"):
                response.status_code = 200
                response.json.return_value = {"id": "run-42", "status": "running"}
                response.headers = {"content-type": "application/json"}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            response.headers = {"content-type": "application/json"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_runtime_run(run_id="run-42")
        self.assertTrue(result.get("ok"))
        called_urls = [str(call.kwargs.get("url") or "") for call in mock_request.call_args_list]
        self.assertIn("https://xyn.xyence.io/xyn/api/runs/run-42", called_urls)
        self.assertNotIn("https://xyn.xyence.io/runs/run-42", called_urls)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_retry_preserves_api_namespace(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 404
        first.json.return_value = {"detail": "Not Found"}
        first.headers = {"content-type": "application/json"}

        second = mock.Mock()
        second.status_code = 200
        second.json.return_value = {"id": "run-77", "status": "completed"}
        second.headers = {"content-type": "application/json"}

        mock_request.side_effect = [first, second]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_runtime_run(run_id="run-77")
        self.assertTrue(result.get("ok"))
        called_urls = [str(call.kwargs.get("url") or "") for call in mock_request.call_args_list]
        self.assertEqual(called_urls[0], "http://xyn.local:8001/xyn/api/runtime-runs/run-77")
        self.assertEqual(called_urls[1], "http://xyn.local:8001/xyn/api/runs/run-77")
        self.assertNotIn("http://xyn.local:8001/runs/run-77", called_urls)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_single_endpoint_reconciles_from_list_when_single_is_missing(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {"content-type": "application/json"}
            if method == "GET" and url.endswith("/api/v1/runs/run-99"):
                response.status_code = 404
                response.json.return_value = {"detail": "Run not found"}
                return response
            if method == "GET" and url.endswith("/api/v1/runs"):
                response.status_code = 200
                response.json.return_value = {
                    "items": [
                        {"id": "run-99", "status": "completed", "summary": "completed from listing"},
                    ]
                }
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_runtime_run(run_id="run-99")
        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("current_status") or ""), "completed")
        self.assertEqual(str(body.get("source") or ""), "list_runtime_runs_fallback")
        warnings = body.get("warnings") if isinstance(body.get("warnings"), list) else []
        self.assertIn("run_status_resolved_from_list_runtime_runs_fallback", warnings)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_logs_use_code_api_base_for_consistent_namespace(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"steps": [{"id": "s1", "status": "completed"}]}
        response.headers = {"content-type": "application/json"}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_runtime_run_logs(run_id="run-1")
        self.assertTrue(result.get("ok"))
        called_urls = [str(call.kwargs.get("url") or "") for call in mock_request.call_args_list]
        self.assertTrue(any(url.startswith("http://core:8000/") for url in called_urls))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_html_response_is_contract_mismatch(self, mock_request: mock.Mock) -> None:
        def _html_response() -> mock.Mock:
            response = mock.Mock()
            response.status_code = 200
            response.json.side_effect = ValueError("not json")
            response.text = "<!doctype html><html><body>SPA shell</body></html>"
            response.headers = {"content-type": "text/html; charset=utf-8"}
            return response

        mock_request.side_effect = [_html_response(), _html_response(), _html_response()]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_runtime_run(run_id="run-html")
        self.assertFalse(result.get("ok"))
        self.assertEqual(str(result.get("error_classification") or ""), "contract_mismatch")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("blocked_reason") or ""), "contract_mismatch")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_json_response_parses_normally(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"id": "run-ok", "status": "completed"}
        response.headers = {"content-type": "application/json"}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.get_runtime_run(run_id="run-ok")
        self.assertTrue(result.get("ok"))
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("run_id") or ""), "run-ok")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_runtime_run_missing_and_forbidden_access(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            if "run-denied" in url:
                response.status_code = 403
                response.json.return_value = {"detail": "Forbidden"}
            else:
                response.status_code = 404
                response.json.return_value = {"detail": "Run not found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        missing = adapter.get_runtime_run(run_id="run-missing")
        denied = adapter.get_runtime_run(run_id="run-denied")

        self.assertFalse(missing["ok"])
        self.assertEqual((missing.get("response") or {}).get("blocked_reason"), "not_found")
        self.assertFalse(denied["ok"])
        self.assertEqual((denied.get("response") or {}).get("blocked_reason"), "permission_denied")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_artifacts_accepts_api_v1_items_shape(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "items": [
                {
                    "id": "a1",
                    "name": "app.net-inventory",
                    "kind": "bundle",
                    "status": "local",
                    "metadata": {"generated_artifact_slug": "app.net-inventory"},
                }
            ],
            "next_cursor": None,
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_artifacts(limit=10, offset=0)
        self.assertTrue(result["ok"])
        artifacts = (result.get("response") or {}).get("artifacts") or []
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["slug"], "app.net-inventory")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_artifacts_normalizes_control_plane_title_shape(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "artifacts": [
                {
                    "id": "row-1",
                    "artifact_id": "5787f1dd-e10e-4f93-b028-d49521d7fcdb",
                    "artifact_type": "module",
                    "title": "xyn-api",
                    "status": "published",
                }
            ]
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_artifacts(limit=10, offset=0)
        self.assertTrue(result["ok"])
        artifacts = (result.get("response") or {}).get("artifacts") or []
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["id"], "5787f1dd-e10e-4f93-b028-d49521d7fcdb")
        self.assertEqual(artifacts[0]["slug"], "xyn-api")

    def test_adapter_get_artifact_source_tree_falls_back_on_400(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.object(
            adapter,
            "_request_with_fallback_paths",
            return_value={"ok": False, "status_code": 400, "path": "/api/v1/artifacts/source-tree"},
        ), mock.patch.object(
            adapter,
            "_artifact_files_via_export_package",
            return_value={
                "artifact_id": "a1",
                "artifact_slug": "xyn-api",
                "files": {"README.md": b"hello\n"},
                "source_mode": "packaged_fallback",
                "source_origin": "packaged_fallback",
                "resolution_branch": "packaged_fallback",
                "resolution_details": {},
                "provenance": {},
                "resolved_source_roots": [],
                "warnings": ["fallback"],
            },
        ):
            result = adapter.get_artifact_source_tree(artifact_slug="xyn-api")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        response = result.get("response") if isinstance(result.get("response"), dict) else {}
        files = response.get("files") if isinstance(response.get("files"), list) else []
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].get("path"), "README.md")

    def test_adapter_read_artifact_source_file_returns_candidate_paths_on_near_match(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.object(
            adapter,
            "_request_with_fallback_paths",
            return_value={"ok": False, "status_code": 404, "path": "/api/v1/artifacts/source-file"},
        ), mock.patch.object(
            adapter,
            "_artifact_files_via_export_package",
            return_value={
                "artifact_id": "a1",
                "artifact_slug": "xyn-api",
                "files": {
                    "apps/a/xyn_api.py": b"def a():\n    return 1\n",
                    "apps/b/xyn_api.py": b"def b():\n    return 2\n",
                },
                "source_mode": "resolved_source",
                "source_origin": "mirror",
                "resolution_branch": "provenance_backed",
                "resolution_details": {},
                "provenance": {},
                "resolved_source_roots": ["/workspace/xyn-platform/services/xyn-api/backend"],
                "warnings": [],
            },
        ):
            result = adapter.read_artifact_source_file(artifact_slug="xyn-api", path="xyn_api.py")

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 404)
        response = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(response.get("error"), "file not found")
        self.assertEqual(
            sorted(response.get("candidate_paths") or []),
            sorted(["apps/a/xyn_api.py", "apps/b/xyn_api.py"]),
        )

    def test_adapter_get_artifact_source_tree_falls_back_on_403(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.object(
            adapter,
            "_request_with_fallback_paths",
            return_value={"ok": False, "status_code": 403, "path": "/api/v1/artifacts/source-tree"},
        ), mock.patch.object(
            adapter,
            "_artifact_files_via_export_package",
            return_value={
                "artifact_id": "a1",
                "artifact_slug": "xyn-api",
                "files": {"README.md": b"hello\n"},
                "source_mode": "packaged_fallback",
                "source_origin": "packaged_fallback",
                "resolution_branch": "packaged_fallback",
                "resolution_details": {},
                "provenance": {},
                "resolved_source_roots": [],
                "warnings": ["fallback"],
            },
        ):
            result = adapter.get_artifact_source_tree(artifact_slug="xyn-api")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)

    def test_adapter_get_artifact_source_tree_returns_slug_ambiguity_error(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.object(
            adapter,
            "_request_with_fallback_paths",
            return_value={"ok": False, "status_code": 404, "path": "/api/v1/artifacts/source-tree"},
        ), mock.patch.object(
            adapter,
            "_artifact_files_via_export_package",
            return_value={
                "_resolution_error": "artifact_slug_ambiguous",
                "artifact_slug": "xyn-api",
                "matches": [
                    {"id": "a1", "slug": "xyn-api", "title": "xyn-api"},
                    {"id": "a2", "slug": "xyn-api", "title": "xyn-api"},
                ],
            },
        ):
            result = adapter.get_artifact_source_tree(artifact_slug="xyn-api")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 409)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("error"), "artifact_slug_ambiguous")
        self.assertEqual(body.get("artifact_slug"), "xyn-api")
        self.assertEqual(len(body.get("candidates") or []), 2)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_artifacts_retries_without_offset_on_400(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 400
        first.json.return_value = {"detail": "offset not supported"}
        second = mock.Mock()
        second.status_code = 200
        second.json.return_value = {"artifacts": []}
        mock_request.side_effect = [first, second]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_artifacts(limit=25, offset=0)
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 2)
        first_kwargs = mock_request.call_args_list[0].kwargs
        second_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(first_kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts")
        self.assertEqual(first_kwargs["params"], {"limit": 25, "offset": 0})
        self.assertEqual(second_kwargs["url"], "http://xyn.local:8001/xyn/api/artifacts")
        self.assertEqual(second_kwargs["params"], {"limit": 25})

    def test_adapter_list_artifacts_invalid_pagination_parameters(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        bad_limit = adapter.list_artifacts(limit=0, offset=0)
        self.assertFalse(bad_limit["ok"])
        self.assertEqual(bad_limit["status_code"], 400)
        self.assertEqual((bad_limit.get("response") or {}).get("error"), "invalid_pagination")
        bad_offset = adapter.list_artifacts(limit=10, offset=-1)
        self.assertFalse(bad_offset["ok"])
        self.assertEqual(bad_offset["status_code"], 400)
        self.assertEqual((bad_offset.get("response") or {}).get("error"), "invalid_pagination")

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

    def test_list_change_efforts_tool_is_registered_and_invokable(self) -> None:
        adapter = mock.Mock()
        adapter.list_change_efforts.return_value = {
            "ok": False,
            "status_code": 404,
            "response": {"error": "not_supported", "blocked_reason": "not_supported"},
        }
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        self.assertIn("list_change_efforts", server.tools)
        tool = server.tools["list_change_efforts"]["fn"]
        result = tool(workspace_id="ws-1", artifact_slug="xyn-api", status="open", limit=25)
        adapter.list_change_efforts.assert_called_once_with(
            workspace_id="ws-1",
            artifact_slug="xyn-api",
            status="open",
            limit=25,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 404)

    def test_list_applications_tool_supports_optional_workspace_id(self) -> None:
        adapter = mock.Mock()
        adapter.list_applications.return_value = {"ok": True, "status_code": 200, "response": {"applications": []}}
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        tool = server.tools["list_applications"]["fn"]
        result = tool(workspace_id="ws-42")

        adapter.list_applications.assert_called_once_with(workspace_id="ws-42")
        self.assertTrue(result["ok"])

    def test_discovery_tool_calls_underlying_adapter(self) -> None:
        adapter = mock.Mock()
        adapter.list_blueprints.return_value = {"ok": True, "status_code": 200, "response": {"blueprints": []}}
        adapter.create_blueprint.return_value = {"ok": True, "status_code": 200, "response": {"id": "bp-1"}}
        adapter.list_release_targets.return_value = {"ok": True, "status_code": 200, "response": {"release_targets": []}}
        adapter.create_release_target.return_value = {"ok": True, "status_code": 200, "response": {"id": "rt-1"}}
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        list_blueprints_tool = server.tools["list_blueprints"]["fn"]
        list_blueprints_result = list_blueprints_tool()
        adapter.list_blueprints.assert_called_once_with()
        self.assertTrue(list_blueprints_result["ok"])
        self.assertEqual(list_blueprints_result["status_code"], 200)
        self.assertEqual(list_blueprints_result["response"]["blueprints"], [])

        create_blueprint_tool = server.tools["create_blueprint"]["fn"]
        create_blueprint_result = create_blueprint_tool(payload={"name": "Xyn Self Hosted Sibling", "namespace": "xyn"})
        adapter.create_blueprint.assert_called_once_with(payload={"name": "Xyn Self Hosted Sibling", "namespace": "xyn"})
        self.assertTrue(create_blueprint_result["ok"])

        tool = server.tools["list_release_targets"]["fn"]
        result = tool()

        adapter.list_release_targets.assert_called_once_with()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["response"]["release_targets"], [])

        create_tool = server.tools["create_release_target"]["fn"]
        create_result = create_tool(payload={"name": "xyn-sibling"})
        adapter.create_release_target.assert_called_once_with(payload={"name": "xyn-sibling"})
        self.assertTrue(create_result["ok"])

    def test_change_effort_and_release_tools_call_underlying_adapter(self) -> None:
        adapter = mock.Mock()
        adapter.create_change_effort.return_value = {"ok": True, "status_code": 200, "response": {"change_effort": {"id": "eff-1"}}}
        adapter.get_change_effort.return_value = {"ok": True, "status_code": 200, "response": {"change_effort": {"id": "eff-1"}}}
        adapter.resolve_effort_source.return_value = {"ok": True, "status_code": 200, "response": {"source": {}}}
        adapter.allocate_effort_branch.return_value = {"ok": True, "status_code": 200, "response": {"change_effort": {"work_branch": "xyn/xyn-api/e1"}}}
        adapter.allocate_effort_worktree.return_value = {"ok": True, "status_code": 200, "response": {"change_effort": {"worktree_path": "/tmp/e1"}}}
        adapter.promote_change_effort.return_value = {"ok": True, "status_code": 200, "response": {"promotion": {"id": "p1"}}}
        adapter.declare_release.return_value = {"ok": True, "status_code": 200, "response": {"release": {"id": "r1"}}}
        adapter.get_artifact_provenance.return_value = {"ok": True, "status_code": 200, "response": {"artifact_slug": "xyn-api"}}
        adapter.create_campaign.return_value = {"ok": True, "status_code": 201, "response": {"campaign": {"id": "cmp-1"}}}
        adapter.update_campaign.return_value = {"ok": True, "status_code": 200, "response": {"campaign": {"id": "cmp-1"}}}
        adapter.create_data_source.return_value = {"ok": True, "status_code": 201, "response": {"data_source": {"id": "ds-1"}}}
        adapter.list_data_sources.return_value = {"ok": True, "status_code": 200, "response": {"sources": [{"id": "ds-1"}]}}
        adapter.get_data_source.return_value = {"ok": True, "status_code": 200, "response": {"data_source": {"id": "ds-1"}}}
        adapter.update_data_source.return_value = {"ok": True, "status_code": 200, "response": {"data_source": {"id": "ds-1"}}}
        adapter.activate_data_source.return_value = {"ok": True, "status_code": 200, "response": {"data_source": {"id": "ds-1"}}}
        adapter.pause_data_source.return_value = {"ok": True, "status_code": 200, "response": {"data_source": {"id": "ds-1"}}}
        adapter.delete_data_source.return_value = {"ok": True, "status_code": 200, "response": {"status": "deleted"}}
        adapter.create_notification_rule.return_value = {
            "ok": True,
            "status_code": 201,
            "response": {"notification_rule": {"id": "nr-1"}},
        }
        adapter.update_notification_rule.return_value = {
            "ok": True,
            "status_code": 200,
            "response": {"notification_rule": {"id": "nr-1"}},
        }
        adapter.assess_change_session_readiness.return_value = {"ok": True, "status_code": 200, "response": {"assessment_state": "ready"}}
        server = FakeMcpServer()
        register_xyn_tools(server, adapter)

        server.tools["create_change_effort"]["fn"](payload={"workspace_id": "w1", "artifact_slug": "xyn-api"})
        adapter.create_change_effort.assert_called_once_with(payload={"workspace_id": "w1", "artifact_slug": "xyn-api"})
        server.tools["get_change_effort"]["fn"](effort_id="eff-1")
        adapter.get_change_effort.assert_called_once_with(effort_id="eff-1")
        server.tools["resolve_effort_source"]["fn"](effort_id="eff-1")
        adapter.resolve_effort_source.assert_called_once_with(effort_id="eff-1")
        server.tools["allocate_effort_branch"]["fn"](effort_id="eff-1", payload={"base_branch": "develop"})
        adapter.allocate_effort_branch.assert_called_once_with(effort_id="eff-1", payload={"base_branch": "develop"})
        server.tools["allocate_effort_worktree"]["fn"](effort_id="eff-1", payload={"root_path": "/tmp"})
        adapter.allocate_effort_worktree.assert_called_once_with(effort_id="eff-1", payload={"root_path": "/tmp"})
        server.tools["promote_change_effort"]["fn"](effort_id="eff-1", payload={"to_branch": "develop"})
        adapter.promote_change_effort.assert_called_once_with(effort_id="eff-1", payload={"to_branch": "develop"})
        server.tools["declare_release"]["fn"](payload={"workspace_id": "w1"})
        adapter.declare_release.assert_called_once_with(payload={"workspace_id": "w1"})
        server.tools["get_artifact_provenance"]["fn"](artifact_slug="xyn-api", workspace_id="w1")
        adapter.get_artifact_provenance.assert_called_once_with(artifact_slug="xyn-api", workspace_id="w1")
        server.tools["create_campaign"]["fn"](workspace_id="w1", name="Retail Expansion")
        adapter.create_campaign.assert_called_once_with(
            workspace_id="w1",
            name="Retail Expansion",
            campaign_type="generic",
            status="draft",
            description="",
            metadata=None,
            payload=None,
        )
        server.tools["update_campaign"]["fn"](campaign_id="cmp-1", workspace_id="w1", payload={"status": "active"})
        adapter.update_campaign.assert_called_once_with(campaign_id="cmp-1", workspace_id="w1", payload={"status": "active"})
        server.tools["create_data_source"]["fn"](workspace_id="w1", key="county-records", name="County Records")
        adapter.create_data_source.assert_called_once_with(
            workspace_id="w1",
            key="county-records",
            name="County Records",
            source_type="generic",
            source_mode="manual",
            refresh_cadence_seconds=0,
            payload=None,
        )
        server.tools["update_data_source"]["fn"](source_id="ds-1", workspace_id="w1", payload={"refresh_cadence_seconds": 86400})
        adapter.update_data_source.assert_called_once_with(
            source_id="ds-1",
            workspace_id="w1",
            payload={"refresh_cadence_seconds": 86400},
        )
        server.tools["list_data_sources"]["fn"](workspace_id="w1")
        adapter.list_data_sources.assert_called_once_with(workspace_id="w1")
        server.tools["get_data_source"]["fn"](source_id="ds-1", workspace_id="w1")
        adapter.get_data_source.assert_called_once_with(source_id="ds-1", workspace_id="w1")
        server.tools["activate_data_source"]["fn"](source_id="ds-1", workspace_id="w1")
        adapter.activate_data_source.assert_called_once_with(source_id="ds-1", workspace_id="w1")
        server.tools["pause_data_source"]["fn"](source_id="ds-1", workspace_id="w1")
        adapter.pause_data_source.assert_called_once_with(source_id="ds-1", workspace_id="w1")
        server.tools["delete_data_source"]["fn"](source_id="ds-1", workspace_id="w1")
        adapter.delete_data_source.assert_called_once_with(source_id="ds-1", workspace_id="w1")
        server.tools["create_notification_rule"]["fn"](
            workspace_id="w1",
            address="alerts@example.com",
            channel="slack",
            event="campaign",
        )
        adapter.create_notification_rule.assert_called_once_with(
            workspace_id="w1",
            address="alerts@example.com",
            channel="slack",
            event="campaign",
            enabled=True,
            is_primary=False,
            payload=None,
        )
        server.tools["update_notification_rule"]["fn"](target_id="nr-1", workspace_id="w1", enabled=False)
        adapter.update_notification_rule.assert_called_once_with(
            target_id="nr-1",
            workspace_id="w1",
            enabled=False,
            payload=None,
        )
        server.tools["list_change_session_pending_checkpoints"]["fn"](application_id="app-1", session_id="sess-1")
        adapter.list_change_session_pending_checkpoints.assert_called_once_with(application_id="app-1", session_id="sess-1")
        server.tools["decide_change_session_checkpoint"]["fn"](
            application_id="app-1",
            session_id="sess-1",
            checkpoint_id="cp-1",
            decision="approved",
            notes="ok",
        )
        adapter.decide_change_session_checkpoint.assert_called_once_with(
            application_id="app-1",
            session_id="sess-1",
            checkpoint_id="cp-1",
            decision="approved",
            notes="ok",
        )
        server.tools["assess_change_session_readiness"]["fn"](application_id="app-1", session_id="sess-1")
        adapter.assess_change_session_readiness.assert_called_once_with(application_id="app-1", session_id="sess-1")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_assess_change_session_readiness_identifies_pending_prompt_and_schema(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.headers = {}
        response.json.return_value = {
            "control": {
                "session": {
                    "planning": {
                        "pending_prompt": {
                            "id": "prompt-1",
                            "message": "Choose initial artifact",
                            "expected_response_kind": "option_set",
                            "allows_multiple": False,
                            "response_schema": {
                                "type": "object",
                                "required": ["selected_option_id"],
                                "properties": {"selected_option_id": {"type": "string"}},
                            },
                            "response_examples": [{"selected_option_id": "opt-xyn-api"}],
                            "option_set": {
                                "options": [
                                    {"id": "opt-xyn-api", "label": "xyn-api"},
                                    {"id": "opt-workbench", "label": "Workbench"},
                                ]
                            },
                        }
                    }
                }
            },
            "next_allowed_actions": ["run_change_session_control_action"],
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        result = adapter.assess_change_session_readiness(application_id="app-1", session_id="sess-1")

        self.assertTrue(result["ok"])
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("assessment_state"), "planner_prompt_pending")
        self.assertTrue(body.get("tools_discoverable"))
        readability = body.get("session_readability") if isinstance(body.get("session_readability"), dict) else {}
        self.assertTrue(readability.get("readable"))
        self.assertGreater(float(readability.get("last_successful_read_timestamp") or 0.0), 0.0)
        planner_prompt = body.get("planner_prompt") if isinstance(body.get("planner_prompt"), dict) else {}
        self.assertTrue(planner_prompt.get("pending"))
        self.assertEqual(planner_prompt.get("expected_response_kind"), "option_set")
        self.assertEqual(
            (planner_prompt.get("response_schema") or {}).get("required"),
            ["selected_option_id"],
        )
        self.assertIn("opt-xyn-api", planner_prompt.get("canonical_option_identifiers") or [])
        self.assertEqual(str(planner_prompt.get("prompt_id") or ""), "prompt-1")
        self.assertEqual(str(planner_prompt.get("kind") or ""), "option_set")
        options = planner_prompt.get("options") if isinstance(planner_prompt.get("options"), list) else []
        self.assertTrue(any(str(item.get("id") or "") == "opt-xyn-api" for item in options if isinstance(item, dict)))
        answer_schema = planner_prompt.get("answer_payload_schema") if isinstance(planner_prompt.get("answer_payload_schema"), dict) else {}
        self.assertEqual(answer_schema.get("required"), ["prompt_id", "response"])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_assess_change_session_readiness_distinguishes_auth_failure_from_binding_rotation(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if "sess-auth" in url:
                if "/xyn/api/" in url:
                    response.status_code = 401
                    response.json.return_value = {"error": "not authenticated"}
                    return response
                response.status_code = 404
                response.json.return_value = {"detail": "Not Found"}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        auth_result = adapter.assess_change_session_readiness(application_id="app-1", session_id="sess-auth")
        contract_result = adapter.assess_change_session_readiness(application_id="app-1", session_id="sess-contract")

        auth_body = auth_result.get("response") if isinstance(auth_result.get("response"), dict) else {}
        contract_body = contract_result.get("response") if isinstance(contract_result.get("response"), dict) else {}
        self.assertEqual(auth_body.get("last_known_error_classification"), "auth_expired")
        self.assertEqual(auth_body.get("assessment_state"), "auth_expired")
        self.assertEqual(contract_body.get("last_known_error_classification"), "binding_rotated")
        self.assertEqual(contract_body.get("assessment_state"), "binding_rotated")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_assess_change_session_readiness_classifies_session_control_500_as_backend_server_failure(
        self, mock_request: mock.Mock
    ) -> None:
        response = mock.Mock()
        response.status_code = 500
        response.headers = {}
        response.json.side_effect = ValueError("not json")
        response.text = "<html>Internal Server Error</html>"
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        result = adapter.assess_change_session_readiness(application_id="app-1", session_id="sess-500")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertFalse(result.get("ok"))
        self.assertEqual(body.get("assessment_state"), "backend_server_error")
        self.assertEqual(body.get("last_known_error_classification"), "backend_server_error")
        self.assertEqual((body.get("auth_session") or {}).get("state"), "unknown")
        readability = body.get("session_readability") if isinstance(body.get("session_readability"), dict) else {}
        self.assertFalse(bool(readability.get("readable")))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_change_session_pending_checkpoints_extracts_planning_checkpoints(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {
            "control": {
                "session": {
                    "planning": {
                        "pending_checkpoints": [
                            {
                                "id": "cp-1",
                                "checkpoint_key": "plan_scope_confirmed",
                                "label": "Approve planning scope before stage apply",
                                "status": "pending",
                                "required_before": "stage",
                                "payload": {"description": "Confirm scope."},
                            }
                        ]
                    }
                }
            }
        }
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        result = adapter.list_change_session_pending_checkpoints(application_id="app-1", session_id="sess-1")

        self.assertTrue(result["ok"])
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("count"), 1)
        pending = body.get("pending_checkpoints") if isinstance(body.get("pending_checkpoints"), list) else []
        self.assertEqual((pending[0] if pending else {}).get("checkpoint_key"), "plan_scope_confirmed")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_run_change_session_control_action_decide_checkpoint_uses_checkpoint_decision_endpoint(
        self, mock_request: mock.Mock
    ) -> None:
        inspect_response = mock.Mock()
        inspect_response.status_code = 200
        inspect_response.json.return_value = {
            "control": {
                "session": {
                    "planning": {
                        "pending_checkpoints": [
                            {"id": "cp-123", "checkpoint_key": "plan_scope_confirmed", "status": "pending"}
                        ]
                    }
                }
            }
        }
        decide_response = mock.Mock()
        decide_response.status_code = 200
        decide_response.json.return_value = {"recorded": True}
        mock_request.side_effect = [inspect_response, decide_response]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="decide_checkpoint",
            action_payload={"decision": "approved", "notes": "approved"},
        )

        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 2)
        inspect_kwargs = mock_request.call_args_list[0].kwargs
        decide_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(inspect_kwargs["url"], "http://xyn.local:8001/xyn/api/change-sessions/sess-1/control")
        self.assertEqual(
            decide_kwargs["url"],
            "http://xyn.local:8001/xyn/api/change-sessions/sess-1/checkpoints/cp-123/decision",
        )
        self.assertEqual(decide_kwargs["json"]["decision"], "approved")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_passes_base_url_auth_and_path(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"status": "ok"}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
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
            "http://xyn.local:8001/xyn/api/change-sessions/sess-1/control",
        )
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-1")
        self.assertEqual(kwargs["headers"]["X-Internal-Token"], "int-1")
        self.assertEqual(kwargs["headers"]["Cookie"], "sessionid=abc")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_uses_request_scoped_bearer_when_config_bearer_missing(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"release_targets": []}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        token = set_request_bearer_token("request-token-123")
        try:
            result = adapter.list_release_targets()
        finally:
            reset_request_bearer_token(token)

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer request-token-123")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_prefers_request_scoped_bearer_over_static_bearer(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"applications": []}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="static-upstream-token",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        token = set_request_bearer_token("request-token-abc")
        try:
            result = adapter.list_applications(workspace_id="ws-1")
        finally:
            reset_request_bearer_token(token)

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer request-token-abc")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_prefers_request_bearer_for_xyn_local_api_by_default(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"applications": []}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                bearer_token="static-upstream-token",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        token = set_request_bearer_token("request-token-local")
        try:
            result = adapter.list_applications(workspace_id="ws-1")
        finally:
            reset_request_bearer_token(token)

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer request-token-local")

    @mock.patch.dict("os.environ", {"XYN_MCP_FORCE_STATIC_BEARER_FOR_LOCAL_API": "true"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_can_force_static_bearer_for_xyn_local_api(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"applications": []}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                bearer_token="static-upstream-token",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
            )
        )

        token = set_request_bearer_token("request-token-local")
        try:
            result = adapter.list_applications(workspace_id="ws-1")
        finally:
            reset_request_bearer_token(token)

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer static-upstream-token")

    @mock.patch.object(XynApiAdapter, "_request")
    def test_tool_surface_is_not_cookie_gated_and_hides_unsupported_list_change_efforts(
        self, mock_request: mock.Mock
    ) -> None:
        def _fake_request(*_args, **kwargs):
            path = str(kwargs.get("path") or "")
            if path == "/api/v1/change-efforts":
                return {"ok": False, "status_code": 405, "response": {"detail": "Method Not Allowed"}}
            if path == "/api/v1/runs":
                return {"ok": True, "status_code": 200, "response": {"items": []}}
            return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        surface = _build_tool_surface(adapter)
        enabled_tools = set(surface.get("enabled_tools") or [])
        disabled_tools = set(surface.get("disabled_tools") or [])
        self.assertIn("list_applications", enabled_tools)
        self.assertIn("list_runtime_runs", enabled_tools)
        self.assertIn("list_change_efforts", disabled_tools)
        self.assertNotIn("list_change_efforts", enabled_tools)
        parity = surface.get("parity") if isinstance(surface.get("parity"), dict) else {}
        list_effort = parity.get("list_change_efforts") if isinstance(parity.get("list_change_efforts"), dict) else {}
        self.assertEqual(list_effort.get("route_exists"), False)

    @mock.patch.object(XynApiAdapter, "_request")
    def test_tool_surface_disables_list_change_efforts_on_404(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            path = str(kwargs.get("path") or "")
            if path == "/api/v1/change-efforts":
                return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}
            if path == "/api/v1/runs":
                return {"ok": True, "status_code": 200, "response": {"items": []}}
            return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        surface = _build_tool_surface(adapter)
        enabled = set(surface.get("enabled_tools") or [])
        disabled = set(surface.get("disabled_tools") or [])
        self.assertIn("list_change_efforts", disabled)
        self.assertNotIn("list_change_efforts", enabled)
        parity = surface.get("parity") if isinstance(surface.get("parity"), dict) else {}
        effort_probe = parity.get("list_change_efforts") if isinstance(parity.get("list_change_efforts"), dict) else {}
        self.assertEqual(effort_probe.get("status_code"), 404)
        self.assertEqual(effort_probe.get("route_exists"), False)

    @mock.patch.object(XynApiAdapter, "_request")
    def test_tool_surface_disables_list_change_efforts_on_405(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            path = str(kwargs.get("path") or "")
            if path == "/api/v1/change-efforts":
                return {"ok": False, "status_code": 405, "response": {"detail": "Method Not Allowed"}}
            if path == "/api/v1/runs":
                return {"ok": True, "status_code": 200, "response": {"items": []}}
            return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        surface = _build_tool_surface(adapter)
        enabled = set(surface.get("enabled_tools") or [])
        disabled = set(surface.get("disabled_tools") or [])
        self.assertIn("list_change_efforts", disabled)
        self.assertNotIn("list_change_efforts", enabled)
        parity = surface.get("parity") if isinstance(surface.get("parity"), dict) else {}
        effort_probe = parity.get("list_change_efforts") if isinstance(parity.get("list_change_efforts"), dict) else {}
        self.assertEqual(effort_probe.get("status_code"), 405)
        self.assertEqual(effort_probe.get("route_exists"), False)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_application_change_session_calls_use_request_scoped_auth_context(self, mock_request: mock.Mock) -> None:
        create = mock.Mock()
        create.status_code = 201
        create.json.return_value = {"session_id": "sess-1", "status": "created"}
        get_session = mock.Mock()
        get_session.status_code = 200
        get_session.json.return_value = {"id": "sess-1", "application_id": "app-1", "status": "created"}
        mock_request.side_effect = [create, get_session]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="sessionid=abc",
                timeout_seconds=10.0,
            )
        )
        token = set_request_bearer_token("oauth-request-token")
        try:
            create_result = adapter.create_application_change_session(application_id="app-1", payload={"title": "demo"})
            get_result = adapter.get_application_change_session(application_id="app-1", session_id="sess-1")
        finally:
            reset_request_bearer_token(token)

        self.assertTrue(create_result["ok"])
        self.assertTrue(get_result["ok"])
        for call in mock_request.call_args_list:
            headers = call.kwargs.get("headers") or {}
            self.assertEqual(headers.get("Authorization"), "Bearer oauth-request-token")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_runtime_runs_uses_authenticated_request_context(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"items": [{"id": "run-1", "status": "completed"}]}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="sessionid=abc",
                timeout_seconds=10.0,
            )
        )
        token = set_request_bearer_token("oauth-request-token")
        try:
            result = adapter.list_runtime_runs(application_id="app-1", session_id="sess-1")
        finally:
            reset_request_bearer_token(token)
        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer oauth-request-token")

    @mock.patch.object(XynApiAdapter, "_request")
    def test_tool_surface_is_consistent_when_cookie_present(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            path = str(kwargs.get("path") or "")
            if path == "/api/v1/change-efforts":
                return {"ok": False, "status_code": 405, "response": {"detail": "Method Not Allowed"}}
            if path == "/api/v1/runs":
                return {"ok": True, "status_code": 200, "response": {"items": []}}
            return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="sessionid=abc",
                timeout_seconds=10.0,
            )
        )
        surface = _build_tool_surface(adapter)
        enabled = set(surface.get("enabled_tools") or [])
        disabled = set(surface.get("disabled_tools") or [])
        self.assertIn("list_applications", enabled)
        self.assertIn("list_runtime_runs", enabled)
        self.assertIn("list_change_efforts", disabled)

    @mock.patch.object(XynApiAdapter, "_request")
    def test_tool_surface_keeps_runtime_runs_enabled_on_transient_probe_errors(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            path = str(kwargs.get("path") or "")
            if path == "/api/v1/change-efforts":
                return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}
            if path == "/api/v1/runs":
                return {"ok": False, "status_code": 503, "response": {"error": "upstream_unreachable"}}
            return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        surface = _build_tool_surface(adapter)
        enabled = set(surface.get("enabled_tools") or [])
        disabled = set(surface.get("disabled_tools") or [])
        self.assertIn("list_artifacts", enabled)
        self.assertIn("list_applications", enabled)
        self.assertIn("list_runtime_runs", enabled)
        self.assertNotIn("list_runtime_runs", disabled)
        self.assertIn("list_change_efforts", disabled)

    @mock.patch.object(XynApiAdapter, "_request")
    def test_tool_surface_disables_list_change_efforts_on_transient_5xx(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            path = str(kwargs.get("path") or "")
            if path == "/api/v1/change-efforts":
                return {"ok": False, "status_code": 503, "response": {"error": "upstream_unreachable"}}
            if path == "/api/v1/runs":
                return {"ok": False, "status_code": 503, "response": {"error": "upstream_unreachable"}}
            return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        surface = _build_tool_surface(adapter)
        enabled = set(surface.get("enabled_tools") or [])
        disabled = set(surface.get("disabled_tools") or [])
        self.assertIn("list_runtime_runs", enabled)
        self.assertIn("list_change_efforts", disabled)
        self.assertNotIn("list_change_efforts", enabled)

    @mock.patch.object(XynApiAdapter, "_request")
    def test_tool_surface_keeps_control_action_tool_enabled_on_method_not_allowed_probe(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            path = str(kwargs.get("path") or "")
            if path == "/api/v1/change-efforts":
                return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}
            if path == "/api/v1/runs":
                return {"ok": True, "status_code": 200, "response": {"items": []}}
            if path.endswith("/control/actions"):
                return {"ok": False, "status_code": 405, "response": {"detail": "Method Not Allowed"}}
            return {"ok": False, "status_code": 404, "response": {"detail": "Not Found"}}

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        surface = _build_tool_surface(adapter)
        enabled = set(surface.get("enabled_tools") or [])
        self.assertIn("run_change_session_control_action", enabled)
        _assert_critical_planner_tools_available(surface)

    def test_critical_planner_tool_assertion_fails_when_control_action_missing(self) -> None:
        with self.assertRaises(RuntimeError):
            _assert_critical_planner_tools_available(
                {
                    "enabled_tools": ["inspect_change_session_control"],
                }
            )

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_normalizes_api_redirect_to_json_401(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 302
        response.headers = {"location": "/auth/login?next=/xyn/api/applications"}
        response.json.side_effect = ValueError("not json")
        response.text = ""
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_applications(workspace_id="ws-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 401)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("error"), "unauthorized")
        self.assertEqual(body.get("blocked_reason"), "interactive_login_redirect")

    @mock.patch.dict("os.environ", {"XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_applications_retries_control_base_urls_when_primary_unreachable(
        self, mock_request: mock.Mock
    ) -> None:
        first_exc = httpx.ConnectError("[Errno -3] Temporary failure in name resolution")
        second = mock.Mock()
        second.status_code = 200
        second.headers = {}
        second.json.return_value = {"applications": [{"application_id": "app-1", "slug": "Xyn", "name": "Xyn"}]}
        mock_request.side_effect = [first_exc, second]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_applications(workspace_id="ws-1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(mock_request.call_count, 2)
        first_url = mock_request.call_args_list[0].kwargs.get("url")
        second_url = mock_request.call_args_list[1].kwargs.get("url")
        self.assertEqual(first_url, "http://xyn-local-api:8000/xyn/api/applications")
        self.assertEqual(second_url, "http://xyn-api:8000/xyn/api/applications")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_uses_api_v1_fallback_path_when_xyn_prefix_missing(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 404
        first.headers = {}
        first.json.return_value = {"detail": "Not Found"}
        second = mock.Mock()
        second.status_code = 200
        second.headers = {}
        second.json.return_value = {"applications": [{"application_id": "app-1", "slug": "Xyn", "name": "Xyn"}]}
        mock_request.side_effect = [first, second]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://seed.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.list_applications(workspace_id="ws-1")
        self.assertTrue(result["ok"])
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(mock_request.call_args_list[0].kwargs.get("url"), "https://seed.xyence.io/xyn/api/applications")
        self.assertEqual(mock_request.call_args_list[1].kwargs.get("url"), "https://seed.xyence.io/api/v1/applications")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_does_not_treat_non_surface_payload_as_empty_tool_surface(
        self, mock_request: mock.Mock
    ) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control"):
                response.status_code = 200
                response.json.return_value = {
                    "control": {"session": {"planning": {}}},
                    "next_allowed_actions": ["run_change_session_control_action"],
                }
                return response
            if method == "GET" and url.endswith("/xyn/api/applications"):
                response.status_code = 200
                response.json.return_value = {"applications": [{"application_id": "app-1", "name": "Xyn API"}]}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        prime = adapter.inspect_change_session_control(application_id="app-1", session_id="sess-1")
        self.assertTrue(prime.get("ok"))

        result = adapter.list_applications(workspace_id="ws-1")
        self.assertTrue(result.get("ok"))
        self.assertEqual(int(result.get("status_code") or 0), 200)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(int(body.get("count") or 0), 1)
        self.assertNotEqual(str(result.get("error_classification") or ""), "empty_tool_surface")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_normalizes_plain_404_to_planner_route_unavailable(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 404
        response.headers = {}
        response.json.side_effect = ValueError("not json")
        response.text = "404 page not found"
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_applications(workspace_id="ws-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 404)
        self.assertEqual(str(result.get("error_classification") or ""), "contract_mismatch")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("error"), "planner_route_unavailable")
        self.assertEqual(body.get("blocked_reason"), "planner_route_unavailable")
        self.assertIn("/xyn/api/applications", body.get("attempted_paths") or [])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_list_applications_preserves_auth_error_when_fallback_path_404s(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 401
        first.headers = {}
        first.json.return_value = {"error": "not authenticated"}
        second = mock.Mock()
        second.status_code = 404
        second.headers = {}
        second.json.return_value = {"detail": "Not Found"}
        mock_request.side_effect = [first, second]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_applications(workspace_id="ws-1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 401)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("error"), "not authenticated")

    @mock.patch.dict("os.environ", {"XYN_SEED_URL": "https://seed.xyence.io", "XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_control_action_uses_same_planner_base_as_control_read(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "GET" and "/control" in url and "/control/actions" not in url:
                if url.startswith("https://seed.xyence.io/"):
                    response.status_code = 200
                    response.json.return_value = {"control": {"status": "ok"}}
                    return response
                response.status_code = 404
                response.json.return_value = {"detail": "Not Found"}
                return response
            if method == "POST" and "/control/actions" in url:
                if url.startswith("https://seed.xyence.io/"):
                    response.status_code = 200
                    response.json.return_value = {"status": "ok"}
                    return response
                response.status_code = 404
                response.json.return_value = {"detail": "Not Found"}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        read_result = adapter.inspect_change_session_control(
            application_id="app-1",
            session_id="sess-1",
        )
        self.assertTrue(read_result.get("ok"))
        self.assertEqual(read_result.get("base_url"), "https://seed.xyence.io")

        write_result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="stage_apply",
            action_payload={"dispatch_runtime": True},
        )
        self.assertTrue(write_result.get("ok"))
        self.assertEqual(write_result.get("base_url"), "https://seed.xyence.io")
        post_calls = [call for call in mock_request.call_args_list if str(call.kwargs.get("method") or "").upper() == "POST"]
        self.assertTrue(post_calls)
        self.assertTrue(str(post_calls[0].kwargs.get("url") or "").startswith("https://seed.xyence.io/"))

    @mock.patch.dict("os.environ", {"XYN_SEED_URL": "https://seed.xyence.io", "XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_binding_rotation_preserves_follow_up_change_session_reads(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions"):
                if url.startswith("https://xyn.xyence.io/"):
                    response.status_code = 404
                    response.json.return_value = {"detail": "Not Found"}
                    return response
                response.status_code = 200
                response.json.return_value = {"change_sessions": [{"id": "sess-1", "application_id": "app-1", "status": "open"}]}
                return response
            if method == "GET" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1"):
                if url.startswith("https://seed.xyence.io/"):
                    response.status_code = 200
                    response.json.return_value = {"id": "sess-1", "application_id": "app-1", "status": "open"}
                    return response
                response.status_code = 404
                response.json.return_value = {"detail": "Not Found"}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        list_result = adapter.list_application_change_sessions(application_id="app-1")
        get_result = adapter.get_application_change_session(application_id="app-1", session_id="sess-1")

        self.assertTrue(list_result.get("ok"))
        self.assertEqual((list_result.get("continuity") or {}).get("retry_reason"), "binding_rotated")
        self.assertTrue(get_result.get("ok"))
        calls = [str(call.kwargs.get("url") or "") for call in mock_request.call_args_list]
        # Once rebound, follow-up read should use seed upstream first.
        self.assertTrue(calls[-1].startswith("https://seed.xyence.io/"))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_control_action_auth_refresh_preserves_session_targeting(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 401
        first.headers = {}
        first.json.return_value = {"error": "not authenticated"}
        second = mock.Mock()
        second.status_code = 200
        second.headers = {}
        second.json.return_value = {"status": "ok"}
        mock_request.side_effect = [first, second]
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="static-token",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        token = set_request_bearer_token("request-token")
        try:
            result = adapter.run_change_session_control_action(
                application_id="app-1",
                session_id="sess-1",
                operation="stage_apply",
                action_payload={"dispatch_runtime": True, "idempotency_key": "idem-1"},
            )
        finally:
            reset_request_bearer_token(token)
        self.assertTrue(result.get("ok"))
        self.assertEqual((result.get("continuity") or {}).get("retry_reason"), "auth_expired")
        first_kwargs = mock_request.call_args_list[0].kwargs
        second_kwargs = mock_request.call_args_list[1].kwargs
        self.assertEqual(
            first_kwargs["url"],
            "http://xyn.local:8001/xyn/api/change-sessions/sess-1/control/actions",
        )
        self.assertEqual(first_kwargs["url"], second_kwargs["url"])
        self.assertEqual((first_kwargs.get("headers") or {}).get("Authorization"), "Bearer request-token")
        self.assertEqual((second_kwargs.get("headers") or {}).get("Authorization"), "Bearer static-token")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_create_change_session_does_not_retry_transport_errors(self, mock_request: mock.Mock) -> None:
        mock_request.side_effect = httpx.ConnectError("dial tcp timeout")
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.create_application_change_session(application_id="app-1", payload={"title": "demo"})
        self.assertFalse(result.get("ok"))
        self.assertEqual(int(result.get("status_code") or 0), 503)
        self.assertEqual(str(result.get("error_classification") or ""), "transient_transport_failure")
        # No reissue across fallback bindings for session-creating calls.
        self.assertEqual(mock_request.call_count, 1)

    @mock.patch.dict("os.environ", {"XYN_SEED_URL": "https://seed.xyence.io", "XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_change_session_plan_reads_remain_session_stable_across_binding_rotation(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "POST" and url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/plan"):
                if url.startswith("https://xyn.xyence.io/"):
                    response.status_code = 404
                    response.json.return_value = {"detail": "Not Found"}
                    return response
                response.status_code = 200
                response.json.return_value = {"status": "draft", "session_id": "sess-1", "application_id": "app-1"}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_application_change_session_plan(application_id="app-1", session_id="sess-1")
        self.assertTrue(result.get("ok"))
        self.assertEqual((result.get("continuity") or {}).get("retry_reason"), "binding_rotated")
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        handle = body.get("change_session_handle") if isinstance(body.get("change_session_handle"), dict) else {}
        self.assertEqual(str(handle.get("application_id") or ""), "app-1")
        self.assertEqual(str(handle.get("session_id") or ""), "sess-1")
        self.assertTrue(str(handle.get("last_known_binding_id") or ""))

    @mock.patch.dict("os.environ", {"XYN_SEED_URL": "https://seed.xyence.io", "XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_mutating_control_action_does_not_retry_unknown_delivery_failures(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 503
        response.headers = {}
        response.json.return_value = {"error": "upstream_unreachable", "detail": "timeout"}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="stage_apply",
            action_payload={"dispatch_runtime": True},
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(str(result.get("action_delivery_state") or ""), "unknown")
        self.assertEqual(str(result.get("error_classification") or ""), "transient_transport_failure")
        # Unknown delivery must not be retried automatically for mutating actions.
        self.assertEqual(mock_request.call_count, 1)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_backend_validation_error_classification_for_400(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 400
        response.headers = {}
        response.json.return_value = {"error": "validation_error", "detail": "response is required"}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="respond_to_planner_prompt",
            action_payload={},
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(str(result.get("error_classification") or ""), "backend_validation_error")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_respond_to_planner_prompt_requires_prompt_id_and_response(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            if method == "GET" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control")
            ):
                response.status_code = 200
                response.json.return_value = {
                    "control": {
                        "session": {
                            "planning": {
                                "pending_prompt": {
                                    "id": "prompt-1",
                                    "expected_response_kind": "option_set",
                                    "response_schema": {
                                        "type": "object",
                                        "required": ["selected_option_id"],
                                        "properties": {"selected_option_id": {"type": "string"}},
                                    },
                                    "option_set": {"options": [{"id": "opt-xyn-api", "label": "xyn-api"}]},
                                }
                            }
                        }
                    }
                }
                return response
            response.status_code = 500
            response.json.return_value = {"error": "unexpected"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="respond_to_planner_prompt",
            action_payload={},
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(int(result.get("status_code") or 0), 400)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("error") or ""), "invalid_prompt_response")
        self.assertEqual(str(body.get("detail") or ""), "prompt_id is required")
        self.assertEqual(str(result.get("error_classification") or ""), "backend_validation_error")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_respond_to_planner_prompt_rejects_superseded_prompt_id(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            if method == "GET" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control")
            ):
                response.status_code = 200
                response.json.return_value = {
                    "control": {
                        "session": {
                            "planning": {
                                "pending_prompt": {
                                    "id": "prompt-new",
                                    "response_schema": {"type": "object"},
                                }
                            }
                        }
                    }
                }
                return response
            response.status_code = 500
            response.json.return_value = {"error": "unexpected"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="respond_to_planner_prompt",
            action_payload={"prompt_id": "prompt-old", "response": {"selected_option_id": "opt-xyn-api"}},
        )
        self.assertFalse(result.get("ok"))
        self.assertEqual(int(result.get("status_code") or 0), 409)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(str(body.get("error") or ""), "planner_prompt_superseded")
        self.assertEqual(str(body.get("current_prompt_id") or ""), "prompt-new")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_respond_to_planner_prompt_accepts_legacy_selected_option_id_shape(self, mock_request: mock.Mock) -> None:
        seen_payloads: list[dict[str, Any]] = []

        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            if method == "GET" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control")
            ):
                response.status_code = 200
                response.json.return_value = {
                    "control": {
                        "session": {
                            "planning": {
                                "pending_prompt": {
                                    "id": "prompt-1",
                                    "response_schema": {
                                        "type": "object",
                                        "required": ["selected_option_id"],
                                        "properties": {"selected_option_id": {"type": "string"}},
                                    },
                                    "option_set": {"options": [{"id": "opt-xyn-api", "label": "xyn-api"}]},
                                }
                            }
                        }
                    }
                }
                return response
            if method == "POST" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control/actions")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control/actions")
            ):
                seen_payloads.append(dict(kwargs.get("json") or {}))
                response.status_code = 200
                response.json.return_value = {"status": "ok"}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="respond_to_planner_prompt",
            action_payload={"selected_option_id": "opt-xyn-api"},
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual(len(seen_payloads), 1)
        self.assertEqual(seen_payloads[0].get("prompt_id"), "prompt-1")
        self.assertEqual(
            (seen_payloads[0].get("response") if isinstance(seen_payloads[0].get("response"), dict) else {}).get(
                "selected_option_id"
            ),
            "opt-xyn-api",
        )

    @mock.patch.dict("os.environ", {"XYN_SEED_URL": "https://seed.xyence.io", "XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_respond_to_planner_prompt_rebinds_and_submits_after_binding_rotation(self, mock_request: mock.Mock) -> None:
        calls: list[tuple[str, str]] = []

        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            calls.append((method, url))
            response = mock.Mock()
            response.headers = {}
            if method == "GET" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control")
            ):
                if url.startswith("https://xyn.xyence.io/"):
                    response.status_code = 404
                    response.json.return_value = {"detail": "Not Found"}
                    return response
                response.status_code = 200
                response.json.return_value = {
                    "control": {
                        "session": {
                            "planning": {
                                "pending_prompt": {
                                    "id": "prompt-2",
                                    "response_schema": {"type": "object", "required": ["selected_option_id"]},
                                    "option_set": {"options": [{"id": "opt-xyn-api", "label": "xyn-api"}]},
                                }
                            }
                        }
                    }
                }
                return response
            if method == "POST" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control/actions")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control/actions")
            ):
                response.status_code = 200
                response.json.return_value = {"status": "ok"}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="respond_to_planner_prompt",
            action_payload={"prompt_id": "prompt-2", "response": {"selected_option_id": "opt-xyn-api"}},
        )
        self.assertTrue(result.get("ok"))
        post_urls = [url for method, url in calls if method == "POST"]
        self.assertTrue(any(url.startswith("https://seed.xyence.io/") for url in post_urls))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_backend_server_error_classification_for_500_html_body(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 500
        response.headers = {}
        response.json.side_effect = ValueError("not json")
        response.text = "<!doctype html><html><body>Internal Server Error</body></html>"
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_application_change_session(application_id="app-1", session_id="sess-1")
        self.assertFalse(result.get("ok"))
        self.assertEqual(str(result.get("error_classification") or ""), "backend_server_error")

    @mock.patch.dict("os.environ", {"XYN_SEED_URL": "https://seed.xyence.io", "XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_empty_surface_triggers_binding_reresolution(self, mock_request: mock.Mock) -> None:
        seen_control_calls = {"xyn": 0}

        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "GET" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control")
            ):
                if url.startswith("https://xyn.xyence.io/"):
                    seen_control_calls["xyn"] += 1
                    next_actions = (
                        ["run_change_session_control_action"]
                        if seen_control_calls["xyn"] == 1
                        else []
                    )
                    response.status_code = 200
                    response.json.return_value = {
                        "control": {"session": {"planning": {}}},
                        "next_allowed_actions": next_actions,
                    }
                    return response
                response.status_code = 200
                response.json.return_value = {
                    "control": {"session": {"planning": {}}},
                    "next_allowed_actions": ["run_change_session_control_action"],
                }
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        # Prime binding state with non-empty surface.
        first = adapter.inspect_change_session_control(application_id="app-1", session_id="sess-1")
        self.assertTrue(first.get("ok"))
        self.assertEqual(str(first.get("base_url") or ""), "https://xyn.xyence.io")

        result = adapter.inspect_change_session_control(application_id="app-1", session_id="sess-1")
        self.assertTrue(result.get("ok"))
        self.assertEqual(str(result.get("base_url") or ""), "https://seed.xyence.io")
        state = result.get("binding_state") if isinstance(result.get("binding_state"), dict) else {}
        self.assertEqual(str(state.get("base_url") or ""), "https://seed.xyence.io")
        self.assertGreaterEqual(int(state.get("tool_surface_count") or 0), 1)
        urls = [str(call.kwargs.get("url") or "") for call in mock_request.call_args_list]
        self.assertTrue(any(url.startswith("https://xyn.xyence.io/") for url in urls))
        self.assertTrue(any(url.startswith("https://seed.xyence.io/") for url in urls))

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_stale_path_classified_as_binding_rotated(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 404
        response.headers = {}
        response.json.return_value = {"detail": "Not Found"}
        mock_request.return_value = response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.inspect_change_session_control(application_id="app-1", session_id="sess-1")
        self.assertFalse(result.get("ok"))
        self.assertEqual(str(result.get("error_classification") or ""), "binding_rotated")

    @mock.patch.dict("os.environ", {"XYN_SEED_URL": "https://seed.xyence.io", "XYN_PUBLIC_BASE_URL": "https://xyn.xyence.io"}, clear=False)
    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_control_action_rebinds_once_after_binding_rotation_without_losing_context(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            method = str(kwargs.get("method") or "").upper()
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if method == "POST" and (
                url.endswith("/xyn/api/change-sessions/sess-1/control/actions")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control/actions")
            ):
                if url.startswith("https://xyn.xyence.io/"):
                    response.status_code = 404
                    response.json.return_value = {"detail": "Not Found"}
                    return response
                response.status_code = 200
                response.json.return_value = {"status": "ok", "next_allowed_actions": ["inspect_change_session_control"]}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="https://xyn.xyence.io",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.run_change_session_control_action(
            application_id="app-1",
            session_id="sess-1",
            operation="inspect",
            action_payload={"idempotency_key": "idem-1"},
        )
        self.assertTrue(result.get("ok"))
        self.assertEqual((result.get("continuity") or {}).get("retry_reason"), "binding_rotated")
        post_urls = [
            str(call.kwargs.get("url") or "")
            for call in mock_request.call_args_list
            if str(call.kwargs.get("method") or "").upper() == "POST"
        ]
        self.assertGreaterEqual(len(post_urls), 2)
        self.assertTrue(post_urls[0].startswith("https://xyn.xyence.io/"))
        self.assertTrue(post_urls[-1].startswith("https://seed.xyence.io/"))
        self.assertTrue(
            all(
                url.endswith("/xyn/api/change-sessions/sess-1/control/actions")
                or url.endswith("/xyn/api/applications/app-1/change-sessions/sess-1/control/actions")
                or url.endswith("/api/v1/change-sessions/sess-1/control/actions")
                or url.endswith("/api/v1/applications/app-1/change-sessions/sess-1/control/actions")
                for url in post_urls
            )
        )

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_artifacts_falls_back_after_control_plane_redirect(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 302
        first.headers = {"location": "/auth/login?next=/xyn/api/artifacts"}
        first.json.side_effect = ValueError("not json")
        first.text = ""

        second = mock.Mock()
        second.status_code = 302
        second.headers = {"location": "/auth/login?next=/xyn/api/artifacts"}
        second.json.side_effect = ValueError("not json")
        second.text = ""

        third = mock.Mock()
        third.status_code = 200
        third.headers = {}
        third.json.return_value = {"artifacts": [{"id": "a1", "title": "xyn-api", "artifact_type": "module"}]}

        mock_request.side_effect = [first, second, third]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        result = adapter.list_artifacts(limit=10, offset=0)
        self.assertTrue(result["ok"])
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("count"), 1)
        artifacts = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
        self.assertEqual(artifacts[0].get("id"), "a1")
        self.assertEqual(mock_request.call_args_list[0].kwargs.get("url"), "http://xyn.local:8001/xyn/api/artifacts")
        self.assertEqual(mock_request.call_args_list[1].kwargs.get("url"), "http://xyn.local:8001/xyn/api/artifacts")
        self.assertEqual(mock_request.call_args_list[2].kwargs.get("url"), "http://xyn.local:8001/api/v1/artifacts")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_runtime_runs_retries_with_static_bearer_after_request_token_redirect(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 302
        first.headers = {"location": "/accounts/login/?next=/xyn/api/runs"}
        first.json.side_effect = ValueError("not json")
        first.text = ""

        second = mock.Mock()
        second.status_code = 200
        second.headers = {}
        second.json.return_value = {"items": [{"id": "run-1", "status": "completed"}]}

        mock_request.side_effect = [first, second]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="static-token",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        token = set_request_bearer_token("request-token")
        try:
            result = adapter.list_runtime_runs()
        finally:
            reset_request_bearer_token(token)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        first_headers = mock_request.call_args_list[0].kwargs.get("headers") or {}
        second_headers = mock_request.call_args_list[1].kwargs.get("headers") or {}
        self.assertEqual(first_headers.get("Authorization"), "Bearer request-token")
        self.assertEqual(second_headers.get("Authorization"), "Bearer static-token")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_sets_upstream_host_headers_when_configured(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 200
        response.json.return_value = {"release_targets": []}
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=11.0,
                upstream_host_header="xyn.xyence.io",
                upstream_forwarded_proto="https",
            )
        )

        result = adapter.list_release_targets()

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Host"], "xyn.xyence.io")
        self.assertEqual(kwargs["headers"]["X-Forwarded-Host"], "xyn.xyence.io")
        self.assertEqual(kwargs["headers"]["X-Forwarded-Proto"], "https")

    def test_healthz_surfaces_effective_xyn_api_base_and_auth_presence(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
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
        self.assertEqual(payload.get("xyn_control_api_base_url"), "http://localhost")
        self.assertEqual(payload.get("xyn_code_api_base_url"), "http://localhost")
        auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
        self.assertTrue(bool(auth.get("has_bearer_token")))
        self.assertFalse(bool(auth.get("has_internal_token")))
        upstream = payload.get("upstream_health") if isinstance(payload.get("upstream_health"), dict) else {}
        probes = upstream.get("probes") if isinstance(upstream.get("probes"), dict) else {}
        self.assertIn("code_artifacts_api", probes)
        self.assertIn("control_workflow_api", probes)
        self.assertIn("response_is_json", probes.get("code_artifacts_api") or {})

    def test_healthz_remains_unauthenticated_when_mcp_auth_enabled(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.dict("os.environ", {"XYN_MCP_AUTH_MODE": "token", "XYN_MCP_AUTH_BEARER_TOKEN": "top-secret"}, clear=False):
            app = create_xyn_mcp_http_app(adapter)
            with TestClient(app) as client:
                response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)

    def test_deal_finder_healthz_profile_and_tool_surface(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        app = create_xyn_mcp_http_app(adapter)
        with TestClient(app) as client:
            response = client.get("/deal-finder/healthz")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("mcp_profile"), "root")
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else []
        self.assertIn("create_data_source", tools)
        self.assertIn("list_runtime_runs", tools)
        self.assertIn("create_change_effort", tools)

    def test_mcp_route_rejects_missing_bearer_when_token_mode_enabled(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.dict("os.environ", {"XYN_MCP_AUTH_MODE": "token", "XYN_MCP_AUTH_BEARER_TOKEN": "top-secret"}, clear=False):
            app = create_xyn_mcp_http_app(adapter)
            with TestClient(app) as client:
                response = client.get("/mcp", headers={"Accept": "text/event-stream"})
        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload.get("error"), "unauthorized")

    def test_deal_finder_mcp_route_rejects_missing_bearer_when_token_mode_enabled(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.dict("os.environ", {"XYN_MCP_AUTH_MODE": "token", "XYN_MCP_AUTH_BEARER_TOKEN": "top-secret"}, clear=False):
            app = create_xyn_mcp_http_app(adapter)
            with TestClient(app) as client:
                response = client.get("/deal-finder/mcp", headers={"Accept": "text/event-stream"})
        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload.get("error"), "unauthorized")

    def test_mcp_route_allows_valid_bearer_when_token_mode_enabled(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.dict("os.environ", {"XYN_MCP_AUTH_MODE": "token", "XYN_MCP_AUTH_BEARER_TOKEN": "top-secret"}, clear=False):
            app = create_xyn_mcp_http_app(adapter)
            with TestClient(app) as client:
                response = client.get(
                    "/mcp",
                    headers={
                        "Accept": "text/event-stream",
                        "Authorization": "Bearer top-secret",
                    },
                )
        self.assertNotEqual(response.status_code, 401)

    @mock.patch("core.mcp.xyn_mcp_server.httpx.request")
    def test_mcp_route_oidc_mode_rejects_invalid_token(self, mock_request: mock.Mock) -> None:
        def _fake_request(*_args, **kwargs):
            url = str(kwargs.get("url") or "")
            response = mock.Mock()
            response.headers = {}
            response.text = ""
            if url == "https://issuer.example.com/.well-known/openid-configuration":
                response.status_code = 200
                response.json.return_value = {"userinfo_endpoint": "https://issuer.example.com/userinfo"}
                return response
            if url == "https://issuer.example.com/userinfo":
                response.status_code = 401
                response.json.return_value = {}
                return response
            response.status_code = 404
            response.json.return_value = {"detail": "Not Found"}
            return response

        mock_request.side_effect = _fake_request
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.dict(
            "os.environ",
            {
                "XYN_MCP_AUTH_MODE": "oidc",
                "OIDC_ISSUER": "https://issuer.example.com",
                "OIDC_CLIENT_ID": "client-id",
            },
            clear=False,
        ):
            app = create_xyn_mcp_http_app(adapter)
            with TestClient(app) as client:
                response = client.get("/mcp", headers={"Accept": "text/event-stream", "Authorization": "Bearer bad"})
        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(payload.get("error"), "unauthorized")
        self.assertIn("WWW-Authenticate", response.headers)

    def test_oidc_well_known_oauth_protected_resource_route_is_available(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.dict(
            "os.environ",
            {
                "XYN_MCP_AUTH_MODE": "oidc",
                "OIDC_ISSUER": "https://issuer.example.com",
                "OIDC_CLIENT_ID": "client-id",
            },
            clear=False,
        ):
            app = create_xyn_mcp_http_app(adapter)
            with TestClient(app) as client:
                response = client.get("/.well-known/oauth-protected-resource", headers={"Host": "mcp.example.com"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("resource"), "http://mcp.example.com/mcp")
        self.assertEqual(payload.get("authorization_servers"), ["https://issuer.example.com"])

    def test_deal_finder_oidc_well_known_oauth_protected_resource_route_is_available(self) -> None:
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        with mock.patch.dict(
            "os.environ",
            {
                "XYN_MCP_AUTH_MODE": "oidc",
                "OIDC_ISSUER": "https://issuer.example.com",
                "OIDC_CLIENT_ID": "client-id",
            },
            clear=False,
        ):
            app = create_xyn_mcp_http_app(adapter)
            with TestClient(app) as client:
                response = client.get("/deal-finder/.well-known/oauth-protected-resource", headers={"Host": "mcp.example.com"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("resource"), "http://mcp.example.com/deal-finder/mcp")
        self.assertEqual(payload.get("authorization_servers"), ["https://issuer.example.com"])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_adapter_list_discovery_endpoints_return_empty_lists_without_errors(self, mock_request: mock.Mock) -> None:
        responses = []
        for body in ({"blueprints": []}, {"release_targets": []}, {"artifacts": []}, {"providers": []}):
            response = mock.Mock()
            response.status_code = 200
            response.json.return_value = body
            responses.append(response)
        mock_request.side_effect = responses

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        blueprints = adapter.list_blueprints()
        release_targets = adapter.list_release_targets()
        artifacts = adapter.list_artifacts(limit=10, offset=0)
        providers = adapter.list_deployment_providers()

        self.assertTrue(blueprints["ok"])
        self.assertEqual(blueprints["response"]["blueprints"], [])
        self.assertTrue(release_targets["ok"])
        self.assertEqual(release_targets["response"]["release_targets"], [])
        self.assertTrue(artifacts["ok"])
        self.assertEqual(artifacts["response"]["artifacts"], [])
        self.assertTrue(providers["ok"])
        self.assertEqual(providers["response"]["providers"], [])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_release_target_404_adds_actionable_warning(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 404
        response.json.side_effect = ValueError("not json")
        response.text = ""
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        target_id = "4939b572-4604-42eb-b133-279580527906"
        detail = adapter.get_release_target(target_id=target_id)
        plan = adapter.get_release_target_deployment_plan(target_id=target_id)
        create_prep = adapter.create_release_target_deployment_preparation_evidence(target_id=target_id)
        get_prep = adapter.get_release_target_deployment_preparation_evidence(target_id=target_id)
        create_handoff = adapter.create_release_target_execution_preparation_handoff(target_id=target_id)
        get_handoff = adapter.get_release_target_execution_preparation_handoff(target_id=target_id)
        approve_handoff = adapter.approve_release_target_execution_preparation(target_id=target_id)
        consume = adapter.consume_release_target_execution_preparation(target_id=target_id)
        run_step = adapter.run_release_target_execution_step(target_id=target_id)
        approve_step = adapter.approve_release_target_execution_step(target_id=target_id)
        get_step_history = adapter.get_release_target_execution_step_history(target_id=target_id)

        self.assertFalse(detail["ok"])
        self.assertEqual(detail["status_code"], 404)
        self.assertEqual(detail["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(detail["response"]["recommended_action"], "refresh_release_targets_and_retry")
        self.assertIn("list_release_targets", detail["response"]["next_allowed_actions"])
        self.assertEqual(detail["response"]["target_id"], target_id)
        self.assertTrue(detail["response"]["warnings"])

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["status_code"], 404)
        self.assertEqual(plan["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(plan["response"]["target_id"], target_id)

        self.assertFalse(create_prep["ok"])
        self.assertEqual(create_prep["status_code"], 404)
        self.assertEqual(create_prep["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(create_prep["response"]["target_id"], target_id)

        self.assertFalse(get_prep["ok"])
        self.assertEqual(get_prep["status_code"], 404)
        self.assertEqual(get_prep["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(get_prep["response"]["target_id"], target_id)

        self.assertFalse(create_handoff["ok"])
        self.assertEqual(create_handoff["status_code"], 404)
        self.assertEqual(create_handoff["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(create_handoff["response"]["target_id"], target_id)

        self.assertFalse(get_handoff["ok"])
        self.assertEqual(get_handoff["status_code"], 404)
        self.assertEqual(get_handoff["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(get_handoff["response"]["target_id"], target_id)

        self.assertFalse(approve_handoff["ok"])
        self.assertEqual(approve_handoff["status_code"], 404)
        self.assertEqual(approve_handoff["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(approve_handoff["response"]["target_id"], target_id)

        self.assertFalse(consume["ok"])
        self.assertEqual(consume["status_code"], 404)
        self.assertEqual(consume["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(consume["response"]["target_id"], target_id)

        self.assertFalse(run_step["ok"])
        self.assertEqual(run_step["status_code"], 404)
        self.assertEqual(run_step["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(run_step["response"]["target_id"], target_id)

        self.assertFalse(approve_step["ok"])
        self.assertEqual(approve_step["status_code"], 404)
        self.assertEqual(approve_step["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(approve_step["response"]["target_id"], target_id)

        self.assertFalse(get_step_history["ok"])
        self.assertEqual(get_step_history["status_code"], 404)
        self.assertEqual(get_step_history["response"]["blocked_reason"], "release_target_not_found")
        self.assertEqual(get_step_history["response"]["target_id"], target_id)

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_artifact_source_tree_404_adds_actionable_warning(self, mock_request: mock.Mock) -> None:
        response = mock.Mock()
        response.status_code = 404
        response.json.side_effect = ValueError("not json")
        response.text = ""
        mock_request.return_value = response

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://localhost",
                code_api_base_url="http://core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )
        artifact_id = "5787f1dd-e10e-4f93-b028-d49521d7fcdb"
        detail = adapter.get_artifact_source_tree(artifact_id=artifact_id)

        self.assertFalse(detail["ok"])
        self.assertEqual(detail["status_code"], 404)
        self.assertEqual(detail["response"]["blocked_reason"], "artifact_not_found")
        self.assertEqual(detail["response"]["recommended_action"], "refresh_artifacts_and_retry")
        self.assertIn("list_artifacts", detail["response"]["next_allowed_actions"])
        self.assertEqual(detail["response"]["artifact_id"], artifact_id)
        self.assertTrue(detail["response"]["warnings"])

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_artifact_source_tree_falls_back_to_control_export_package(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 404
        first.json.return_value = {"detail": "Not Found"}
        second = mock.Mock()
        second.status_code = 404
        second.json.return_value = {"detail": "Not Found"}
        third = mock.Mock()
        third.status_code = 404
        third.json.return_value = {"detail": "Not Found"}
        listing = mock.Mock()
        listing.status_code = 200
        listing.json.return_value = {"artifacts": [{"id": "a1", "slug": "app.demo", "title": "Demo App"}]}
        export = mock.Mock()
        export.status_code = 200
        export.json.side_effect = ValueError("not json")
        export.text = ""
        export.content = self._build_zip_bytes({"src/main.py": b"print('ok')\n"})
        mock_request.side_effect = [first, second, third, listing, export]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.get_artifact_source_tree(artifact_slug="app.demo")

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        files = ((result.get("response") or {}).get("files") or [])
        self.assertTrue(any(isinstance(row, dict) and row.get("path") == "src/main.py" for row in files))
        self.assertEqual(mock_request.call_args_list[-1].kwargs["url"], "http://xyn-local-api:8000/xyn/api/artifacts/a1/export-package")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_read_source_file_falls_back_to_control_export_package(self, mock_request: mock.Mock) -> None:
        first = mock.Mock()
        first.status_code = 404
        first.json.return_value = {"detail": "Not Found"}
        second = mock.Mock()
        second.status_code = 404
        second.json.return_value = {"detail": "Not Found"}
        third = mock.Mock()
        third.status_code = 404
        third.json.return_value = {"detail": "Not Found"}
        listing = mock.Mock()
        listing.status_code = 200
        listing.json.return_value = {"artifacts": [{"id": "a1", "slug": "app.demo", "title": "Demo App"}]}
        export = mock.Mock()
        export.status_code = 200
        export.json.side_effect = ValueError("not json")
        export.text = ""
        export.content = self._build_zip_bytes({"README.md": b"line1\nline2\nline3\n"})
        mock_request.side_effect = [first, second, third, listing, export]

        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn-local-api:8000",
                code_api_base_url="http://xyn-core:8000",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        result = adapter.read_artifact_source_file(path="README.md", artifact_slug="app.demo", start_line=2, end_line=2)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        payload = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(payload.get("path"), "README.md")
        self.assertEqual(payload.get("content"), "line2")

    @mock.patch("core.mcp.xyn_api_adapter.httpx.request")
    def test_readiness_and_checkpoint_listing_preserve_artifact_scope_without_application_id(
        self, mock_request: mock.Mock
    ) -> None:
        control_response = mock.Mock()
        control_response.status_code = 200
        control_response.headers = {}
        control_response.json.return_value = {
            "control": {
                "session": {
                    "id": "sess-art-1",
                    "planning": {
                        "pending_checkpoints": [
                            {
                                "id": "cp-1",
                                "checkpoint_key": "planner_scope_review",
                                "label": "Scope review",
                                "status": "pending",
                                "required_before": "stage_apply",
                            }
                        ]
                    },
                }
            },
            "scope_type": "artifact",
            "scope": {
                "scope_type": "artifact",
                "artifact_id": "art-1",
                "artifact_slug": "xyn-api",
                "workspace_id": "ws-1",
            },
            "session_id": "sess-art-1",
            "next_allowed_actions": ["decide_change_session_checkpoint"],
        }
        mock_request.return_value = control_response
        adapter = XynApiAdapter(
            XynApiAdapterConfig(
                control_api_base_url="http://xyn.local:8001",
                bearer_token="",
                internal_token="",
                cookie="",
                timeout_seconds=10.0,
            )
        )

        readiness = adapter.assess_change_session_readiness(session_id="sess-art-1")
        self.assertTrue(readiness.get("ok"))
        readiness_ctx = ((readiness.get("response") or {}).get("context") or {})
        self.assertEqual(readiness_ctx.get("session_id"), "sess-art-1")
        self.assertEqual(readiness_ctx.get("scope_type"), "artifact")
        self.assertEqual(((readiness_ctx.get("scope") or {}).get("artifact_slug")), "xyn-api")

        pending = adapter.list_change_session_pending_checkpoints(session_id="sess-art-1")
        self.assertTrue(pending.get("ok"))
        pending_body = pending.get("response") if isinstance(pending.get("response"), dict) else {}
        self.assertEqual(pending_body.get("session_id"), "sess-art-1")
        self.assertEqual(pending_body.get("scope_type"), "artifact")
        self.assertEqual(((pending_body.get("scope") or {}).get("artifact_id")), "art-1")
