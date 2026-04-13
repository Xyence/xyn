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
from core.mcp.xyn_mcp_server import TOOL_NAMES, _build_tool_surface, create_xyn_mcp_http_app, register_xyn_tools
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
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/control", urls)

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

        applications = adapter.list_applications()
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
        self.assertIn("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1", urls)

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
            urls.count("http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/control/actions"),
            4,
        )
        action_bodies = [call.kwargs.get("json") for call in mock_request.call_args_list if call.kwargs.get("json")]
        operations = [str(body.get("operation")) for body in action_bodies if isinstance(body, dict) and body.get("operation")]
        self.assertEqual(operations, ["stage_apply", "prepare_preview", "validate", "commit"])

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
        self.assertEqual(urls[0], "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/control/actions")
        self.assertEqual(urls[1], "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/promotion-evidence")
        self.assertEqual(urls[2], "http://xyn.local:8001/xyn/api/applications/app-1/change-sessions/sess-1/control/actions")

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

        mock_request.side_effect = [
            create,
            stage,
            runtime_list,
            runtime_get,
            runtime_logs,
            runtime_artifacts,
            preview,
            validate,
            commit,
            promote,
        ]

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
        guardrails = (stage_result.get("response") or {}).get("guardrails") or {}
        self.assertIn("xyn_orchestrator/xyn_api.py", guardrails.get("affected_files") or [])

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
    def test_runtime_run_missing_and_forbidden_access(self, mock_request: mock.Mock) -> None:
        def _not_found() -> mock.Mock:
            response = mock.Mock()
            response.status_code = 404
            response.json.return_value = {"detail": "Run not found"}
            return response

        forbidden = mock.Mock()
        forbidden.status_code = 403
        forbidden.json.return_value = {"detail": "Forbidden"}

        mock_request.side_effect = [_not_found(), _not_found(), _not_found(), _not_found(), forbidden]
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
            result = adapter.list_applications()
        finally:
            reset_request_bearer_token(token)

        self.assertTrue(result["ok"])
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer request-token-abc")

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
        self.assertIn("list_change_efforts", enabled_tools)
        self.assertEqual(disabled_tools, set())
        parity = surface.get("parity") if isinstance(surface.get("parity"), dict) else {}
        list_effort = parity.get("list_change_efforts") if isinstance(parity.get("list_change_efforts"), dict) else {}
        self.assertEqual(list_effort.get("route_exists"), False)

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
        self.assertIn("list_change_efforts", enabled)
        self.assertEqual(disabled, set())

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
        result = adapter.list_applications()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 401)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        self.assertEqual(body.get("error"), "unauthorized")
        self.assertEqual(body.get("blocked_reason"), "interactive_login_redirect")

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
