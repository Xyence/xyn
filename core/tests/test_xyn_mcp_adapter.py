from __future__ import annotations

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
from core.mcp.xyn_mcp_server import TOOL_NAMES, create_xyn_mcp_http_app, register_xyn_tools
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
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/api/v1/artifacts/source-tree")
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
        self.assertEqual(kwargs["url"], "http://xyn-core:8000/api/v1/artifacts/source-tree")

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
        self.assertEqual(first_kwargs["url"], "http://xyn-core:8000/api/v1/artifacts/source-tree")
        self.assertEqual(second_kwargs["url"], "http://core:8000/api/v1/artifacts/source-tree")
        self.assertEqual(third_kwargs["url"], "http://xyn-local-api:8000/api/v1/artifacts/source-tree")

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
        self.assertEqual(first_kwargs["url"], "http://xyn-core:8000/api/v1/artifacts/source-tree")
        self.assertEqual(second_kwargs["url"], "http://core:8000/api/v1/artifacts/source-tree")

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
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/api/v1/artifacts/source-file")
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
        self.assertEqual(kwargs["url"], "http://xyn-core:8000/api/v1/artifacts/source-tree")

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
        self.assertEqual(kwargs["url"], "http://xyn.local:8001/api/v1/artifacts/analyze-codebase")
        self.assertEqual(kwargs["params"]["artifact_slug"], "app.demo")
        self.assertEqual(kwargs["params"]["mode"], "python_api")

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
            "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/control",
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
        self.assertTrue(bool(auth.get("has_cookie")))

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
        discovery_response = mock.Mock()
        discovery_response.status_code = 200
        discovery_response.json.return_value = {"userinfo_endpoint": "https://issuer.example.com/userinfo"}
        userinfo_response = mock.Mock()
        userinfo_response.status_code = 401
        userinfo_response.json.return_value = {}
        mock_request.side_effect = [discovery_response, userinfo_response]
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
