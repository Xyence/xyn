from __future__ import annotations

import os
import secrets
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, List

import httpx
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from core.mcp.xyn_api_adapter import (
    XynApiAdapter,
    XynApiAdapterConfig,
    reset_request_bearer_token,
    set_request_bearer_token,
)


TOOL_NAMES = [
    "list_applications",
    "get_application",
    "list_application_change_sessions",
    "get_application_change_session",
    "create_application_change_session",
    "create_decomposition_campaign",
    "get_decomposition_campaign",
    "inspect_decomposition_guardrails",
    "get_decomposition_observability",
    "get_application_change_session_plan",
    "stage_apply_application_change_session",
    "prepare_preview_application_change_session",
    "get_application_change_session_preview_status",
    "validate_application_change_session",
    "commit_application_change_session",
    "promote_application_change_session",
    "rollback_application_change_session",
    "get_application_change_session_commits",
    "get_application_change_session_promotion_evidence",
    "list_runtime_runs",
    "get_runtime_run",
    "get_runtime_run_logs",
    "get_runtime_run_artifacts",
    "get_runtime_run_commands",
    "cancel_runtime_run",
    "rerun_runtime_run",
    "get_dev_task_by_id",
    "list_dev_tasks_for_change_session",
    "list_blueprints",
    "get_blueprint",
    "create_blueprint",
    "list_release_targets",
    "get_release_target",
    "create_release_target",
    "list_artifacts",
    "list_remote_artifact_sources",
    "search_remote_artifact_catalog",
    "list_remote_artifact_candidates",
    "get_artifact",
    "get_artifact_source_tree",
    "read_artifact_source_file",
    "search_artifact_source",
    "analyze_artifact_codebase",
    "analyze_python_api_artifact",
    "get_artifact_module_metrics",
    "list_deployment_providers",
    "get_provider_capabilities",
    "inspect_change_session_control",
    "assess_change_session_readiness",
    "list_change_session_pending_checkpoints",
    "decide_change_session_checkpoint",
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
    "create_change_effort",
    "list_change_efforts",
    "get_change_effort",
    "resolve_effort_source",
    "allocate_effort_branch",
    "allocate_effort_worktree",
    "get_effort_diff",
    "get_effort_changed_files",
    "get_effort_git_status",
    "get_effort_preview_binding",
    "promote_change_effort",
    "declare_release",
    "get_artifact_provenance",
    "create_campaign",
    "update_campaign",
    "create_data_source",
    "list_data_sources",
    "get_data_source",
    "update_data_source",
    "activate_data_source",
    "pause_data_source",
    "delete_data_source",
    "create_notification_rule",
    "update_notification_rule",
]

_TOOL_ROUTE_PROBES: Dict[str, tuple[str, str, str]] = {
    "list_change_efforts": ("GET", "/api/v1/change-efforts", "code"),
    "list_runtime_runs": ("GET", "/api/v1/runs", "code"),
}
_UPSTREAM_HEALTH_PROBES: Dict[str, tuple[str, str, str]] = {
    "code_artifacts_api": ("GET", "/api/v1/artifacts", "code"),
    "control_workflow_api": ("GET", "/xyn/api/applications", "control"),
}
_UNSUPPORTED_ROUTE_STATUS_CODES = {404, 405}
_CRITICAL_PLANNER_TOOLS = {"inspect_change_session_control", "run_change_session_control_action"}


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

    @property
    def oidc_well_known_config_url(self) -> str:
        issuer = self.oidc_issuer.rstrip("/")
        if not issuer:
            return ""
        return f"{issuer}/.well-known/openid-configuration"


@dataclass(frozen=True)
class McpRuntimeIdentity:
    environment: str
    deployment_id: str
    build_sha: str
    image_tag: str
    release_target: str

    @staticmethod
    def from_env() -> "McpRuntimeIdentity":
        return McpRuntimeIdentity(
            environment=str(os.getenv("XYN_ENV", "")).strip() or str(os.getenv("ENVIRONMENT", "")).strip(),
            deployment_id=str(os.getenv("XYN_MCP_DEPLOYMENT_ID", "")).strip()
            or str(os.getenv("XYN_DEPLOYMENT_ID", "")).strip(),
            build_sha=str(os.getenv("XYN_MCP_BUILD_SHA", "")).strip()
            or str(os.getenv("GIT_SHA", "")).strip()
            or str(os.getenv("XYN_BUILD_SHA", "")).strip(),
            image_tag=str(os.getenv("XYN_MCP_IMAGE_TAG", "")).strip() or str(os.getenv("IMAGE_TAG", "")).strip(),
            release_target=str(os.getenv("XYN_MCP_RELEASE_TARGET", "")).strip()
            or str(os.getenv("XYN_RELEASE_TARGET", "")).strip(),
        )


@dataclass(frozen=True)
class McpEndpointBinding:
    name: str
    mcp_path_prefix: str
    resource_path: str
    health_path: str
    oauth_protected_resource_path: str
    rewrite_to_prefix: str
    app_scope: str
    profile_name: str
    environment: str = ""
    deployment_namespace: str = ""


def _normalize_path(value: str, *, default: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    if not raw.startswith("/"):
        raw = f"/{raw}"
    return raw.rstrip("/") or "/"


def _default_endpoint_bindings(profile_name: str) -> List[McpEndpointBinding]:
    root = McpEndpointBinding(
        name="root",
        mcp_path_prefix="/mcp",
        resource_path="/mcp",
        health_path="/healthz",
        oauth_protected_resource_path="/.well-known/oauth-protected-resource",
        rewrite_to_prefix="/mcp",
        app_scope="root",
        profile_name=profile_name,
    )
    if str(os.getenv("XYN_MCP_ENABLE_DEPRECATED_DEAL_FINDER_ALIAS", "false")).strip().lower() in {"1", "true", "yes"}:
        deal_finder = McpEndpointBinding(
            name="deal-finder",
            mcp_path_prefix="/deal-finder/mcp",
            resource_path="/deal-finder/mcp",
            health_path="/deal-finder/healthz",
            oauth_protected_resource_path="/deal-finder/.well-known/oauth-protected-resource",
            rewrite_to_prefix="/mcp",
            app_scope="deal-finder",
            profile_name=profile_name,
            environment=str(os.getenv("XYN_ENV", "")).strip(),
        )
        return [root, deal_finder]
    return [root]


def _load_endpoint_bindings(profile_name: str) -> List[McpEndpointBinding]:
    raw = str(os.getenv("XYN_MCP_ENDPOINT_BINDINGS", "")).strip()
    if not raw:
        return _default_endpoint_bindings(profile_name)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _default_endpoint_bindings(profile_name)
    if not isinstance(payload, list):
        return _default_endpoint_bindings(profile_name)
    bindings: List[McpEndpointBinding] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip() or f"binding-{index}"
        mcp_path_prefix = _normalize_path(str(item.get("mcp_path_prefix") or ""), default="/mcp")
        rewrite_to_prefix = _normalize_path(str(item.get("rewrite_to_prefix") or ""), default="/mcp")
        resource_path = _normalize_path(str(item.get("resource_path") or ""), default=mcp_path_prefix)
        health_path = _normalize_path(str(item.get("health_path") or ""), default=f"{mcp_path_prefix.rstrip('/')}/healthz")
        oauth_path = _normalize_path(
            str(item.get("oauth_protected_resource_path") or ""),
            default=f"{mcp_path_prefix.rstrip('/')}/.well-known/oauth-protected-resource",
        )
        bindings.append(
            McpEndpointBinding(
                name=name,
                mcp_path_prefix=mcp_path_prefix,
                resource_path=resource_path,
                health_path=health_path,
                oauth_protected_resource_path=oauth_path,
                rewrite_to_prefix=rewrite_to_prefix,
                app_scope=str(item.get("app_scope") or "").strip() or name,
                profile_name=str(item.get("profile_name") or "").strip() or profile_name,
                environment=str(item.get("environment") or "").strip(),
                deployment_namespace=str(item.get("deployment_namespace") or "").strip(),
            )
        )
    if not bindings:
        return _default_endpoint_bindings(profile_name)
    has_root = any(binding.mcp_path_prefix == "/mcp" for binding in bindings)
    if not has_root:
        bindings.insert(0, _default_endpoint_bindings(profile_name)[0])
    return bindings


def _register_tool(mcp_server: Any, *, name: str, description: str, fn: Callable[..., Dict[str, Any]]) -> None:
    enabled_tools = getattr(mcp_server, "_xyn_enabled_tools", None)
    if isinstance(enabled_tools, set) and enabled_tools and name not in enabled_tools:
        return
    if hasattr(mcp_server, "add_tool"):
        mcp_server.add_tool(fn, name=name, description=description)
        return
    if hasattr(mcp_server, "tool"):
        decorator = mcp_server.tool(name=name, description=description)
        decorator(fn)
        return
    raise RuntimeError("MCP server does not expose add_tool/tool registration API")


def register_xyn_tools(mcp_server: Any, adapter: XynApiAdapter) -> None:
    def list_applications(workspace_id: str = "") -> Dict[str, Any]:
        return adapter.list_applications(workspace_id=workspace_id)

    def get_application(application_id: str) -> Dict[str, Any]:
        return adapter.get_application(application_id=application_id)

    def list_application_change_sessions(application_id: str) -> Dict[str, Any]:
        return adapter.list_application_change_sessions(application_id=application_id)

    def get_application_change_session(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_application_change_session(application_id=application_id, session_id=session_id)

    def create_application_change_session(
        application_id: str,
        artifact_source: Dict[str, Any] | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.create_application_change_session(
            application_id=application_id,
            artifact_source=artifact_source,
            payload=payload,
        )

    def create_decomposition_campaign(
        application_id: str = "",
        artifact_id: str = "",
        artifact_slug: str = "",
        workspace_id: str = "",
        artifact_source: Dict[str, Any] | None = None,
        target_source_files: list[str] | None = None,
        extraction_seams: list[str] | None = None,
        moved_handlers_modules: list[str] | None = None,
        required_test_suites: list[str] | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.create_decomposition_campaign(
            application_id=application_id,
            artifact_id=artifact_id,
            artifact_slug=artifact_slug,
            workspace_id=workspace_id,
            artifact_source=artifact_source,
            target_source_files=target_source_files,
            extraction_seams=extraction_seams,
            moved_handlers_modules=moved_handlers_modules,
            required_test_suites=required_test_suites,
            payload=payload,
        )

    def get_decomposition_campaign(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_decomposition_campaign(application_id=application_id, session_id=session_id)

    def inspect_decomposition_guardrails(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.inspect_decomposition_guardrails(application_id=application_id, session_id=session_id)

    def get_decomposition_observability(
        application_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
        artifact_slug: str = "",
        top_n: int = 50,
    ) -> Dict[str, Any]:
        return adapter.get_decomposition_observability(
            application_id=application_id,
            session_id=session_id,
            artifact_id=artifact_id,
            artifact_slug=artifact_slug,
            top_n=top_n,
        )

    def get_application_change_session_plan(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_application_change_session_plan(application_id=application_id, session_id=session_id)

    def stage_apply_application_change_session(
        application_id: str = "",
        session_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.stage_apply_application_change_session(
            application_id=application_id,
            session_id=session_id,
            payload=payload,
        )

    def prepare_preview_application_change_session(
        application_id: str = "",
        session_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.prepare_preview_application_change_session(
            application_id=application_id,
            session_id=session_id,
            payload=payload,
        )

    def get_application_change_session_preview_status(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_application_change_session_preview_status(application_id=application_id, session_id=session_id)

    def validate_application_change_session(
        application_id: str = "",
        session_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.validate_application_change_session(
            application_id=application_id,
            session_id=session_id,
            payload=payload,
        )

    def commit_application_change_session(
        application_id: str = "",
        session_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.commit_application_change_session(
            application_id=application_id,
            session_id=session_id,
            payload=payload,
        )

    def promote_application_change_session(
        application_id: str = "",
        session_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.promote_application_change_session(
            application_id=application_id,
            session_id=session_id,
            payload=payload,
        )

    def rollback_application_change_session(
        application_id: str = "",
        session_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.rollback_application_change_session(
            application_id=application_id,
            session_id=session_id,
            payload=payload,
        )

    def get_application_change_session_commits(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_application_change_session_commits(application_id=application_id, session_id=session_id)

    def get_application_change_session_promotion_evidence(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_application_change_session_promotion_evidence(application_id=application_id, session_id=session_id)

    def list_runtime_runs(
        application_id: str = "",
        session_id: str = "",
        limit: int = 50,
        cursor: str = "",
        status: str = "",
    ) -> Dict[str, Any]:
        return adapter.list_runtime_runs(
            application_id=application_id,
            session_id=session_id,
            limit=limit,
            cursor=cursor,
            status=status,
        )

    def get_runtime_run(run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_runtime_run(run_id=run_id, application_id=application_id, session_id=session_id)

    def get_runtime_run_logs(run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_runtime_run_logs(run_id=run_id, application_id=application_id, session_id=session_id)

    def get_runtime_run_artifacts(run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_runtime_run_artifacts(run_id=run_id, application_id=application_id, session_id=session_id)

    def get_runtime_run_commands(run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_runtime_run_commands(run_id=run_id, application_id=application_id, session_id=session_id)

    def cancel_runtime_run(run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.cancel_runtime_run(run_id=run_id, application_id=application_id, session_id=session_id)

    def rerun_runtime_run(run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.rerun_runtime_run(run_id=run_id, application_id=application_id, session_id=session_id)

    def get_dev_task_by_id(task_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_dev_task_by_id(task_id=task_id, application_id=application_id, session_id=session_id)

    def list_dev_tasks_for_change_session(
        application_id: str = "",
        session_id: str = "",
        limit: int = 100,
        status: str = "",
    ) -> Dict[str, Any]:
        return adapter.list_dev_tasks_for_change_session(
            application_id=application_id,
            session_id=session_id,
            limit=limit,
            status=status,
        )

    def list_blueprints() -> Dict[str, Any]:
        return adapter.list_blueprints()

    def get_blueprint(blueprint_id: str) -> Dict[str, Any]:
        return adapter.get_blueprint(blueprint_id=blueprint_id)

    def create_blueprint(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return adapter.create_blueprint(payload=payload)

    def list_release_targets() -> Dict[str, Any]:
        return adapter.list_release_targets()

    def get_release_target(target_id: str) -> Dict[str, Any]:
        return adapter.get_release_target(target_id=target_id)

    def create_release_target(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return adapter.create_release_target(payload=payload)

    def list_artifacts(limit: int | None = None, offset: int | None = None) -> Dict[str, Any]:
        return adapter.list_artifacts(limit=limit, offset=offset)

    def list_remote_artifact_sources() -> Dict[str, Any]:
        return adapter.list_remote_artifact_sources()

    def search_remote_artifact_catalog(
        query: str = "",
        artifact_slug: str = "",
        artifact_type: str = "",
        source_root: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> Dict[str, Any]:
        return adapter.search_remote_artifact_catalog(
            query=query,
            artifact_slug=artifact_slug,
            artifact_type=artifact_type,
            source_root=source_root,
            limit=limit,
            cursor=cursor,
        )

    def list_remote_artifact_candidates(
        manifest_source: str = "",
        package_source: str = "",
        artifact_slug: str = "",
        artifact_type: str = "",
    ) -> Dict[str, Any]:
        return adapter.list_remote_artifact_candidates(
            manifest_source=manifest_source,
            package_source=package_source,
            artifact_slug=artifact_slug,
            artifact_type=artifact_type,
        )

    def get_artifact(artifact_id: str) -> Dict[str, Any]:
        return adapter.get_artifact(artifact_id=artifact_id)

    def get_artifact_source_tree(
        artifact_slug: str = "",
        artifact_id: str = "",
        include_line_counts: bool = True,
        max_files: int | None = None,
        max_depth: int | None = None,
        include_files: bool = True,
    ) -> Dict[str, Any]:
        return adapter.get_artifact_source_tree(
            artifact_slug=artifact_slug,
            artifact_id=artifact_id,
            include_line_counts=include_line_counts,
            max_files=max_files,
            max_depth=max_depth,
            include_files=include_files,
        )

    def read_artifact_source_file(
        path: str,
        artifact_slug: str = "",
        artifact_id: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> Dict[str, Any]:
        return adapter.read_artifact_source_file(
            path=path,
            artifact_slug=artifact_slug,
            artifact_id=artifact_id,
            start_line=start_line,
            end_line=end_line,
        )

    def search_artifact_source(
        query: str,
        artifact_slug: str = "",
        artifact_id: str = "",
        path_glob: str = "",
        file_extensions: str = "",
        regex: bool = False,
        case_sensitive: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        return adapter.search_artifact_source(
            query=query,
            artifact_slug=artifact_slug,
            artifact_id=artifact_id,
            path_glob=path_glob,
            file_extensions=file_extensions,
            regex=regex,
            case_sensitive=case_sensitive,
            limit=limit,
        )

    def analyze_artifact_codebase(
        artifact_slug: str = "",
        artifact_id: str = "",
        mode: str = "general",
    ) -> Dict[str, Any]:
        return adapter.analyze_artifact_codebase(artifact_slug=artifact_slug, artifact_id=artifact_id, mode=mode)

    def analyze_python_api_artifact(
        artifact_slug: str = "",
        artifact_id: str = "",
    ) -> Dict[str, Any]:
        return adapter.analyze_python_api_artifact(artifact_slug=artifact_slug, artifact_id=artifact_id)

    def get_artifact_module_metrics(
        artifact_slug: str = "",
        artifact_id: str = "",
        top_n: int = 200,
    ) -> Dict[str, Any]:
        return adapter.get_artifact_module_metrics(
            artifact_slug=artifact_slug,
            artifact_id=artifact_id,
            top_n=top_n,
        )

    def list_deployment_providers() -> Dict[str, Any]:
        return adapter.list_deployment_providers()

    def get_provider_capabilities(provider_key: str) -> Dict[str, Any]:
        return adapter.get_provider_capabilities(provider_key=provider_key)

    def inspect_change_session_control(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.inspect_change_session_control(
            application_id=application_id,
            session_id=session_id,
        )

    def assess_change_session_readiness(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.assess_change_session_readiness(
            application_id=application_id,
            session_id=session_id,
        )

    def list_change_session_pending_checkpoints(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.list_change_session_pending_checkpoints(
            application_id=application_id,
            session_id=session_id,
        )

    def decide_change_session_checkpoint(
        application_id: str = "",
        session_id: str = "",
        checkpoint_id: str = "",
        decision: str = "approved",
        notes: str = "",
    ) -> Dict[str, Any]:
        return adapter.decide_change_session_checkpoint(
            application_id=application_id,
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            decision=decision,
            notes=notes,
        )

    def run_change_session_control_action(
        application_id: str = "",
        session_id: str = "",
        operation: str = "",
        action_payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.run_change_session_control_action(
            application_id=application_id,
            session_id=session_id,
            operation=operation,
            action_payload=action_payload,
        )

    def get_change_session_promotion_evidence(application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        return adapter.get_change_session_promotion_evidence(
            application_id=application_id,
            session_id=session_id,
        )

    def get_release_target_deployment_plan(target_id: str) -> Dict[str, Any]:
        return adapter.get_release_target_deployment_plan(target_id=target_id)

    def create_release_target_deployment_preparation_evidence(
        target_id: str,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.create_release_target_deployment_preparation_evidence(
            target_id=target_id,
            payload=payload,
        )

    def get_release_target_deployment_preparation_evidence(target_id: str, limit: int = 10) -> Dict[str, Any]:
        return adapter.get_release_target_deployment_preparation_evidence(
            target_id=target_id,
            limit=limit,
        )

    def create_release_target_execution_preparation_handoff(
        target_id: str,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.create_release_target_execution_preparation_handoff(
            target_id=target_id,
            payload=payload,
        )

    def get_release_target_execution_preparation_handoff(target_id: str, limit: int = 10) -> Dict[str, Any]:
        return adapter.get_release_target_execution_preparation_handoff(
            target_id=target_id,
            limit=limit,
        )

    def approve_release_target_execution_preparation(
        target_id: str,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.approve_release_target_execution_preparation(
            target_id=target_id,
            payload=payload,
        )

    def consume_release_target_execution_preparation(
        target_id: str,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.consume_release_target_execution_preparation(
            target_id=target_id,
            payload=payload,
        )

    def run_release_target_execution_step(
        target_id: str,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.run_release_target_execution_step(
            target_id=target_id,
            payload=payload,
        )

    def approve_release_target_execution_step(
        target_id: str,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.approve_release_target_execution_step(
            target_id=target_id,
            payload=payload,
        )

    def get_release_target_execution_step_history(target_id: str, limit: int = 10) -> Dict[str, Any]:
        return adapter.get_release_target_execution_step_history(
            target_id=target_id,
            limit=limit,
        )

    def create_change_effort(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return adapter.create_change_effort(payload=payload)

    def list_change_efforts(
        workspace_id: str = "",
        artifact_slug: str = "",
        status: str = "",
        limit: int = 100,
    ) -> Dict[str, Any]:
        return adapter.list_change_efforts(
            workspace_id=workspace_id,
            artifact_slug=artifact_slug,
            status=status,
            limit=limit,
        )

    def get_change_effort(effort_id: str) -> Dict[str, Any]:
        return adapter.get_change_effort(effort_id=effort_id)

    def resolve_effort_source(effort_id: str) -> Dict[str, Any]:
        return adapter.resolve_effort_source(effort_id=effort_id)

    def allocate_effort_branch(effort_id: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return adapter.allocate_effort_branch(effort_id=effort_id, payload=payload)

    def allocate_effort_worktree(effort_id: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return adapter.allocate_effort_worktree(effort_id=effort_id, payload=payload)

    def get_effort_diff(effort_id: str) -> Dict[str, Any]:
        return adapter.get_effort_diff(effort_id=effort_id)

    def get_effort_changed_files(effort_id: str) -> Dict[str, Any]:
        return adapter.get_effort_changed_files(effort_id=effort_id)

    def get_effort_git_status(effort_id: str) -> Dict[str, Any]:
        return adapter.get_effort_git_status(effort_id=effort_id)

    def get_effort_preview_binding(effort_id: str) -> Dict[str, Any]:
        return adapter.get_effort_preview_binding(effort_id=effort_id)

    def promote_change_effort(effort_id: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return adapter.promote_change_effort(effort_id=effort_id, payload=payload)

    def declare_release(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        return adapter.declare_release(payload=payload)

    def get_artifact_provenance(artifact_slug: str, workspace_id: str = "") -> Dict[str, Any]:
        return adapter.get_artifact_provenance(artifact_slug=artifact_slug, workspace_id=workspace_id)

    def create_campaign(
        workspace_id: str = "",
        name: str = "",
        campaign_type: str = "generic",
        status: str = "draft",
        description: str = "",
        metadata: Dict[str, Any] | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.create_campaign(
            workspace_id=workspace_id,
            name=name,
            campaign_type=campaign_type,
            status=status,
            description=description,
            metadata=metadata,
            payload=payload,
        )

    def update_campaign(
        campaign_id: str,
        workspace_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.update_campaign(
            campaign_id=campaign_id,
            workspace_id=workspace_id,
            payload=payload,
        )

    def create_data_source(
        workspace_id: str = "",
        key: str = "",
        name: str = "",
        source_type: str = "generic",
        source_mode: str = "manual",
        refresh_cadence_seconds: int = 0,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.create_data_source(
            workspace_id=workspace_id,
            key=key,
            name=name,
            source_type=source_type,
            source_mode=source_mode,
            refresh_cadence_seconds=refresh_cadence_seconds,
            payload=payload,
        )

    def update_data_source(
        source_id: str,
        workspace_id: str = "",
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.update_data_source(
            source_id=source_id,
            workspace_id=workspace_id,
            payload=payload,
        )

    def list_data_sources(
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        return adapter.list_data_sources(workspace_id=workspace_id)

    def get_data_source(
        source_id: str,
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        return adapter.get_data_source(source_id=source_id, workspace_id=workspace_id)

    def activate_data_source(
        source_id: str,
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        return adapter.activate_data_source(source_id=source_id, workspace_id=workspace_id)

    def pause_data_source(
        source_id: str,
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        return adapter.pause_data_source(source_id=source_id, workspace_id=workspace_id)

    def delete_data_source(
        source_id: str,
        workspace_id: str = "",
    ) -> Dict[str, Any]:
        return adapter.delete_data_source(source_id=source_id, workspace_id=workspace_id)

    def create_notification_rule(
        workspace_id: str = "",
        address: str = "",
        channel: str = "email",
        event: str = "campaign",
        enabled: bool = True,
        is_primary: bool = False,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.create_notification_rule(
            workspace_id=workspace_id,
            address=address,
            channel=channel,
            event=event,
            enabled=enabled,
            is_primary=is_primary,
            payload=payload,
        )

    def update_notification_rule(
        target_id: str,
        workspace_id: str = "",
        enabled: bool = True,
        payload: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return adapter.update_notification_rule(
            target_id=target_id,
            workspace_id=workspace_id,
            enabled=enabled,
            payload=payload,
        )

    _register_tool(
        mcp_server,
        name="list_applications",
        description="List applications available for solution change sessions.",
        fn=list_applications,
    )
    _register_tool(
        mcp_server,
        name="get_application",
        description="Get one application by id.",
        fn=get_application,
    )
    _register_tool(
        mcp_server,
        name="list_application_change_sessions",
        description="List solution change sessions for an application.",
        fn=list_application_change_sessions,
    )
    _register_tool(
        mcp_server,
        name="get_application_change_session",
        description="Get one solution change session by application_id/session_id.",
        fn=get_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="create_application_change_session",
        description="Create a solution change session for an application. Supports explicit artifact_source metadata when targeting a remote catalog artifact.",
        fn=create_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="create_decomposition_campaign",
        description="Create a decomposition-focused change session campaign. For remote non-installed artifacts, first call list_remote_artifact_candidates and pass artifact_source.",
        fn=create_decomposition_campaign,
    )
    _register_tool(
        mcp_server,
        name="get_decomposition_campaign",
        description="Read compact decomposition campaign/session status metadata for safe autonomous orchestration.",
        fn=get_decomposition_campaign,
    )
    _register_tool(
        mcp_server,
        name="inspect_decomposition_guardrails",
        description="Inspect decomposition guardrails: changed routes/imports/files, oversized delta, and test recommendations.",
        fn=inspect_decomposition_guardrails,
    )
    _register_tool(
        mcp_server,
        name="get_decomposition_observability",
        description="Read decomposition observability deltas including module metrics and route inventory data.",
        fn=get_decomposition_observability,
    )
    _register_tool(
        mcp_server,
        name="get_application_change_session_plan",
        description="Get the execution plan/control status for a solution change session.",
        fn=get_application_change_session_plan,
    )
    _register_tool(
        mcp_server,
        name="stage_apply_application_change_session",
        description="Run stage-apply for a solution change session.",
        fn=stage_apply_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="prepare_preview_application_change_session",
        description="Prepare preview environment for a solution change session.",
        fn=prepare_preview_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="get_application_change_session_preview_status",
        description="Get compact, agent-facing preview readiness/status for a solution change session.",
        fn=get_application_change_session_preview_status,
    )
    _register_tool(
        mcp_server,
        name="validate_application_change_session",
        description="Validate a staged solution change session.",
        fn=validate_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="commit_application_change_session",
        description="Commit a validated solution change session.",
        fn=commit_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="promote_application_change_session",
        description="Promote a committed solution change session.",
        fn=promote_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="rollback_application_change_session",
        description="Rollback a promoted solution change session.",
        fn=rollback_application_change_session,
    )
    _register_tool(
        mcp_server,
        name="get_application_change_session_commits",
        description="Read commit metadata for a solution change session.",
        fn=get_application_change_session_commits,
    )
    _register_tool(
        mcp_server,
        name="get_application_change_session_promotion_evidence",
        description="Read promotion evidence for a solution change session.",
        fn=get_application_change_session_promotion_evidence,
    )
    _register_tool(
        mcp_server,
        name="list_runtime_runs",
        description="List runtime runs for a change session or globally.",
        fn=list_runtime_runs,
    )
    _register_tool(
        mcp_server,
        name="get_runtime_run",
        description="Get runtime run status and execution summary.",
        fn=get_runtime_run,
    )
    _register_tool(
        mcp_server,
        name="get_runtime_run_logs",
        description="Get runtime run logs/step summaries for failure analysis.",
        fn=get_runtime_run_logs,
    )
    _register_tool(
        mcp_server,
        name="get_runtime_run_artifacts",
        description="List artifacts produced by a runtime run.",
        fn=get_runtime_run_artifacts,
    )
    _register_tool(
        mcp_server,
        name="get_runtime_run_commands",
        description="Get commands/steps executed by a runtime run.",
        fn=get_runtime_run_commands,
    )
    _register_tool(
        mcp_server,
        name="cancel_runtime_run",
        description="Cancel a running runtime run.",
        fn=cancel_runtime_run,
    )
    _register_tool(
        mcp_server,
        name="rerun_runtime_run",
        description="Rerun or retry a completed/failed runtime run when supported.",
        fn=rerun_runtime_run,
    )
    _register_tool(
        mcp_server,
        name="get_dev_task_by_id",
        description="Get one dev task by id.",
        fn=get_dev_task_by_id,
    )
    _register_tool(
        mcp_server,
        name="list_dev_tasks_for_change_session",
        description="List dev tasks associated with a change session.",
        fn=list_dev_tasks_for_change_session,
    )

    _register_tool(
        mcp_server,
        name="list_blueprints",
        description="List available blueprints for release-target binding.",
        fn=list_blueprints,
    )
    _register_tool(
        mcp_server,
        name="get_blueprint",
        description="Get one blueprint by id.",
        fn=get_blueprint,
    )
    _register_tool(
        mcp_server,
        name="create_blueprint",
        description="Create or update a blueprint using existing blueprint API payload fields.",
        fn=create_blueprint,
    )
    _register_tool(
        mcp_server,
        name="list_release_targets",
        description="List discoverable release targets with provider and configuration summaries.",
        fn=list_release_targets,
    )
    _register_tool(
        mcp_server,
        name="get_release_target",
        description="Get one release target by id.",
        fn=get_release_target,
    )
    _register_tool(
        mcp_server,
        name="create_release_target",
        description="Create a release target using the existing release-target API payload contract.",
        fn=create_release_target,
    )
    _register_tool(
        mcp_server,
        name="list_artifacts",
        description="List discoverable local/installed artifacts from the current registry model. Remote catalog candidates are returned by list_remote_artifact_candidates.",
        fn=list_artifacts,
    )
    _register_tool(
        mcp_server,
        name="list_remote_artifact_sources",
        description="List configured remote artifact catalog roots (bucket/prefix/source type) used for MCP remote discovery.",
        fn=list_remote_artifact_sources,
    )
    _register_tool(
        mcp_server,
        name="search_remote_artifact_catalog",
        description="Search remote artifact catalog by name/slug/type without pre-known manifest source. Use before change-session creation for non-installed artifacts.",
        fn=search_remote_artifact_catalog,
    )
    _register_tool(
        mcp_server,
        name="list_remote_artifact_candidates",
        description="List remote (not-yet-installed) artifact candidates from manifest/package sources. Use before creating change sessions for S3-backed artifacts like Deal Finder.",
        fn=list_remote_artifact_candidates,
    )
    _register_tool(
        mcp_server,
        name="get_artifact",
        description="Get one artifact by id.",
        fn=get_artifact,
    )
    _register_tool(
        mcp_server,
        name="get_artifact_source_tree",
        description="Get artifact source file tree (prefer artifact_slug; supports artifact_id). Optional bounds: max_files/max_depth/include_files.",
        fn=get_artifact_source_tree,
    )
    _register_tool(
        mcp_server,
        name="read_artifact_source_file",
        description="Read a line-range chunk from a source file in an artifact (prefer artifact_slug).",
        fn=read_artifact_source_file,
    )
    _register_tool(
        mcp_server,
        name="search_artifact_source",
        description="Search source files in an artifact with optional glob/extension/regex controls (prefer artifact_slug).",
        fn=search_artifact_source,
    )
    _register_tool(
        mcp_server,
        name="analyze_artifact_codebase",
        description="Return synthesized structural/refactor analysis for an artifact codebase (mode: general|python_api, prefer artifact_slug).",
        fn=analyze_artifact_codebase,
    )
    _register_tool(
        mcp_server,
        name="analyze_python_api_artifact",
        description="Specialized Python web/API monolith assessment for oversized files, framework overlap, routes, and extraction planning.",
        fn=analyze_python_api_artifact,
    )
    _register_tool(
        mcp_server,
        name="get_artifact_module_metrics",
        description="Return per-file module metrics for an artifact codebase.",
        fn=get_artifact_module_metrics,
    )
    _register_tool(
        mcp_server,
        name="list_deployment_providers",
        description="List deployment provider/module capabilities available to release-target workflows.",
        fn=list_deployment_providers,
    )
    _register_tool(
        mcp_server,
        name="get_provider_capabilities",
        description="Get deployment provider/module capability details by provider key.",
        fn=get_provider_capabilities,
    )

    _register_tool(
        mcp_server,
        name="inspect_change_session_control",
        description="Inspect canonical control status for a solution change session.",
        fn=inspect_change_session_control,
    )
    _register_tool(
        mcp_server,
        name="assess_change_session_readiness",
        description="Read-only orchestration readiness assessment for binding/auth/prompt/schema/retry safety on a change session.",
        fn=assess_change_session_readiness,
    )
    _register_tool(
        mcp_server,
        name="list_change_session_pending_checkpoints",
        description="List pending planning checkpoints that gate stage-apply for a change session.",
        fn=list_change_session_pending_checkpoints,
    )
    _register_tool(
        mcp_server,
        name="decide_change_session_checkpoint",
        description="Approve or reject a planning checkpoint (defaults to first pending checkpoint if checkpoint_id is omitted).",
        fn=decide_change_session_checkpoint,
    )
    _register_tool(
        mcp_server,
        name="run_change_session_control_action",
        description="Execute a canonical control action for a change session.",
        fn=run_change_session_control_action,
    )
    _register_tool(
        mcp_server,
        name="get_change_session_promotion_evidence",
        description="Fetch durable promotion/rollback evidence for a change session.",
        fn=get_change_session_promotion_evidence,
    )
    _register_tool(
        mcp_server,
        name="get_release_target_deployment_plan",
        description="Fetch non-destructive seam-driven deployment plan for a release target.",
        fn=get_release_target_deployment_plan,
    )
    _register_tool(
        mcp_server,
        name="create_release_target_deployment_preparation_evidence",
        description="Create deployment-preparation evidence for a release target.",
        fn=create_release_target_deployment_preparation_evidence,
    )
    _register_tool(
        mcp_server,
        name="get_release_target_deployment_preparation_evidence",
        description="Read deployment-preparation evidence history for a release target.",
        fn=get_release_target_deployment_preparation_evidence,
    )
    _register_tool(
        mcp_server,
        name="create_release_target_execution_preparation_handoff",
        description="Create execution-preparation handoff from deployment-preparation evidence.",
        fn=create_release_target_execution_preparation_handoff,
    )
    _register_tool(
        mcp_server,
        name="get_release_target_execution_preparation_handoff",
        description="Read execution-preparation handoff history for a release target.",
        fn=get_release_target_execution_preparation_handoff,
    )
    _register_tool(
        mcp_server,
        name="approve_release_target_execution_preparation",
        description="Approve execution-preparation handoff for a release target.",
        fn=approve_release_target_execution_preparation,
    )
    _register_tool(
        mcp_server,
        name="consume_release_target_execution_preparation",
        description="Consume execution-preparation handoff into prepared execution evidence.",
        fn=consume_release_target_execution_preparation,
    )
    _register_tool(
        mcp_server,
        name="run_release_target_execution_step",
        description="Run one explicitly approved bounded execution step for a release target.",
        fn=run_release_target_execution_step,
    )
    _register_tool(
        mcp_server,
        name="approve_release_target_execution_step",
        description="Approve one prepared execution step for a release target.",
        fn=approve_release_target_execution_step,
    )
    _register_tool(
        mcp_server,
        name="get_release_target_execution_step_history",
        description="Read execution-step evidence history for a release target.",
        fn=get_release_target_execution_step_history,
    )
    _register_tool(
        mcp_server,
        name="create_change_effort",
        description="Create a change effort for branch/worktree-scoped artifact development.",
        fn=create_change_effort,
    )
    _register_tool(
        mcp_server,
        name="list_change_efforts",
        description="List change efforts filtered by workspace/artifact/status.",
        fn=list_change_efforts,
    )
    _register_tool(
        mcp_server,
        name="get_change_effort",
        description="Get one change effort by id.",
        fn=get_change_effort,
    )
    _register_tool(
        mcp_server,
        name="resolve_effort_source",
        description="Resolve artifact provenance-backed source metadata into a change effort.",
        fn=resolve_effort_source,
    )
    _register_tool(
        mcp_server,
        name="allocate_effort_branch",
        description="Allocate deterministic branch metadata for a change effort.",
        fn=allocate_effort_branch,
    )
    _register_tool(
        mcp_server,
        name="allocate_effort_worktree",
        description="Allocate deterministic isolated worktree metadata for a change effort.",
        fn=allocate_effort_worktree,
    )
    _register_tool(
        mcp_server,
        name="get_effort_diff",
        description="Read effort diff (or metadata-backed diff summary when backend diff endpoint is unavailable).",
        fn=get_effort_diff,
    )
    _register_tool(
        mcp_server,
        name="get_effort_changed_files",
        description="Read changed files for a change effort.",
        fn=get_effort_changed_files,
    )
    _register_tool(
        mcp_server,
        name="get_effort_git_status",
        description="Read git status for a change effort worktree.",
        fn=get_effort_git_status,
    )
    _register_tool(
        mcp_server,
        name="get_effort_preview_binding",
        description="Resolve change-effort linkage to preview/session status.",
        fn=get_effort_preview_binding,
    )
    _register_tool(
        mcp_server,
        name="promote_change_effort",
        description="Create promotion intent/preflight metadata for effort branch promotion to develop.",
        fn=promote_change_effort,
    )
    _register_tool(
        mcp_server,
        name="declare_release",
        description="Declare an explicit release binding commit, artifact revisions, and image digests.",
        fn=declare_release,
    )
    _register_tool(
        mcp_server,
        name="get_artifact_provenance",
        description="Read artifact provenance timeline across efforts, promotions, and release declarations.",
        fn=get_artifact_provenance,
    )
    _register_tool(
        mcp_server,
        name="create_campaign",
        description="Create a campaign within a workspace.",
        fn=create_campaign,
    )
    _register_tool(
        mcp_server,
        name="update_campaign",
        description="Update a campaign within a workspace.",
        fn=update_campaign,
    )
    _register_tool(
        mcp_server,
        name="create_data_source",
        description="Create a source connector (data source) within a workspace.",
        fn=create_data_source,
    )
    _register_tool(
        mcp_server,
        name="update_data_source",
        description="Update a source connector (data source) within a workspace.",
        fn=update_data_source,
    )
    _register_tool(
        mcp_server,
        name="list_data_sources",
        description="List data sources for a workspace.",
        fn=list_data_sources,
    )
    _register_tool(
        mcp_server,
        name="get_data_source",
        description="Get one data source by id.",
        fn=get_data_source,
    )
    _register_tool(
        mcp_server,
        name="activate_data_source",
        description="Enable/activate a data source.",
        fn=activate_data_source,
    )
    _register_tool(
        mcp_server,
        name="pause_data_source",
        description="Disable/pause a data source.",
        fn=pause_data_source,
    )
    _register_tool(
        mcp_server,
        name="delete_data_source",
        description="Delete a data source when supported by backend capabilities.",
        fn=delete_data_source,
    )
    _register_tool(
        mcp_server,
        name="create_notification_rule",
        description="Create a notification target/rule for a workspace.",
        fn=create_notification_rule,
    )
    _register_tool(
        mcp_server,
        name="update_notification_rule",
        description="Update a notification target/rule for a workspace.",
        fn=update_notification_rule,
    )


def _probe_backend_route(adapter: XynApiAdapter, *, method: str, path: str, base: str) -> Dict[str, Any]:
    base_url = adapter.config.control_api_base_url if base == "control" else (
        adapter.config.code_api_base_url or adapter.config.control_api_base_url
    )
    result = adapter._request(  # noqa: SLF001 - intentional internal parity probe
        method=method,
        path=path,
        base_url=base_url,
    )
    if not result.get("ok"):
        result["error_classification"] = str(result.get("error_classification") or adapter._classify_error(result))  # noqa: SLF001
    return result


def _build_tool_surface(adapter: XynApiAdapter) -> Dict[str, Any]:
    enabled_tools = set(TOOL_NAMES)
    parity: Dict[str, Dict[str, Any]] = {}

    # Probe optional endpoints and disable tools when backend route parity fails.
    for tool_name, (method, path, base) in _TOOL_ROUTE_PROBES.items():
        probe = _probe_backend_route(adapter, method=method, path=path, base=base)
        status_code = int(probe.get("status_code") or 0)
        # list_runtime_runs should tolerate transient startup/transport failures to
        # avoid catalog flapping ("Unknown tool" race during boot).
        if tool_name == "list_runtime_runs":
            route_exists = bool(status_code) and status_code not in _UNSUPPORTED_ROUTE_STATUS_CODES
        elif tool_name == "run_change_session_control_action":
            # Control-action routes are object-scoped and may return auth/state errors
            # for probe ids; only hide when route is definitively unavailable.
            route_exists = bool(status_code) and status_code != 404
        else:
            # For optional surfaces (e.g., change-efforts), only advertise when
            # probe indicates route is truly available (exclude transient 5xx).
            route_exists = (
                bool(status_code)
                and status_code not in _UNSUPPORTED_ROUTE_STATUS_CODES
                and status_code < 500
            )
        if not route_exists:
            enabled_tools.discard(tool_name)
        parity[tool_name] = {
            "enabled": route_exists,
            "reason": "" if route_exists else "backend_route_unavailable",
            "error_classification": str(probe.get("error_classification") or ""),
            "auth_required": status_code in {401, 403},
            "route_exists": route_exists,
            "status_code": status_code,
            "path": path,
            "base": base,
        }

    return {
        "enabled_tools": sorted(enabled_tools),
        "disabled_tools": sorted([name for name in TOOL_NAMES if name not in enabled_tools]),
        "parity": parity,
    }


def _assert_critical_planner_tools_available(tool_surface: Dict[str, Any]) -> None:
    declared_tools = set(TOOL_NAMES)
    undeclared = sorted(tool for tool in _CRITICAL_PLANNER_TOOLS if tool not in declared_tools)
    if undeclared:
        raise RuntimeError(
            "Critical planner MCP tools are not declared in TOOL_NAMES: "
            + ", ".join(undeclared)
        )
    enabled_tools = set(tool_surface.get("enabled_tools") or [])
    missing = sorted(tool for tool in _CRITICAL_PLANNER_TOOLS if tool not in enabled_tools)
    if missing:
        raise RuntimeError(
            "Critical planner MCP tools are unavailable at startup: "
            + ", ".join(missing)
            + ". Verify planner control-plane route wiring."
        )


def _build_upstream_health(adapter: XynApiAdapter) -> Dict[str, Any]:
    probes: Dict[str, Dict[str, Any]] = {}
    for probe_name, (method, path, base) in _UPSTREAM_HEALTH_PROBES.items():
        result = _probe_backend_route(adapter, method=method, path=path, base=base)
        status_code = int(result.get("status_code") or 0)
        response = result.get("response") if isinstance(result.get("response"), dict) else {}
        raw_text = str(response.get("raw_text") or "").strip()
        looks_like_html_404 = status_code == 404 and (
            "page not found" in raw_text.lower() or "<!doctype html" in raw_text.lower()
        )
        looks_like_json = not raw_text
        if raw_text:
            stripped = raw_text.lstrip()
            looks_like_json = stripped.startswith("{") or stripped.startswith("[")
        probes[probe_name] = {
            "ok": bool(result.get("ok")),
            "status_code": status_code,
            "path": path,
            "base": base,
            "base_url": str(result.get("base_url") or ""),
            "response_is_json": looks_like_json,
            "html_404": looks_like_html_404,
            "blocked_reason": str(response.get("blocked_reason") or "").strip(),
            "error": str(response.get("error") or "").strip(),
            "detail": str(response.get("detail") or "").strip() or raw_text[:240],
        }
    overall_ok = all(bool(item.get("ok")) for item in probes.values())
    return {"ok": overall_ok, "probes": probes}


def create_xyn_mcp_server(
    adapter: XynApiAdapter | None = None,
    *,
    profile_name: str = "root",
) -> Any:
    from mcp.server.fastmcp import FastMCP

    configured_adapter = adapter or XynApiAdapter(XynApiAdapterConfig.from_env())
    tool_surface = _build_tool_surface(configured_adapter)
    _assert_critical_planner_tools_available(tool_surface)
    server = FastMCP("xyn-control-adapter")
    setattr(server, "_xyn_enabled_tools", set(tool_surface.get("enabled_tools") or []))
    setattr(server, "_xyn_tool_surface", tool_surface)
    register_xyn_tools(server, configured_adapter)
    return server


def _create_xyn_mcp_http_subapp(
    *,
    adapter: XynApiAdapter,
    auth_config: McpAuthConfig,
    mcp_resource_path: str = "/mcp",
    profile_name: str = "root",
) -> Starlette:
    mcp_server = create_xyn_mcp_server(
        adapter,
        profile_name=profile_name,
    )
    tool_surface = getattr(mcp_server, "_xyn_tool_surface", {}) if mcp_server is not None else {}
    upstream_health = _build_upstream_health(adapter)
    enabled_tools = list(tool_surface.get("enabled_tools") or TOOL_NAMES)
    disabled_tools = list(tool_surface.get("disabled_tools") or [])
    parity = tool_surface.get("parity") if isinstance(tool_surface.get("parity"), dict) else {}
    runtime_identity = McpRuntimeIdentity.from_env()
    endpoint_bindings = _load_endpoint_bindings(profile_name)
    endpoint_bindings_by_name = {binding.name: binding for binding in endpoint_bindings}
    deprecated_alias_enabled = (
        str(os.getenv("XYN_MCP_ENABLE_DEPRECATED_DEAL_FINDER_ALIAS", "false")).strip().lower() in {"1", "true", "yes"}
    )
    # Prefer explicit streamable HTTP app construction (works across mcp versions).
    if hasattr(mcp_server, "streamable_http_app"):
        app = mcp_server.streamable_http_app()
    else:
        # Back-compat fallback for older FastMCP variants.
        app = mcp_server.run(transport="streamable-http", return_app=True)

    def _runtime_identity_payload(binding: McpEndpointBinding) -> Dict[str, Any]:
        effective_environment = str(binding.environment or runtime_identity.environment).strip()
        return {
            "environment": effective_environment,
            "deployment_id": runtime_identity.deployment_id,
            "build_sha": runtime_identity.build_sha,
            "image_tag": runtime_identity.image_tag,
            "release_target": runtime_identity.release_target,
            "deployment_namespace": binding.deployment_namespace,
            "app_scope": binding.app_scope,
            "mcp_profile": binding.profile_name,
            "binding_name": binding.name,
            "binding_mcp_path_prefix": binding.mcp_path_prefix,
            "binding_resource_path": binding.resource_path,
        }

    async def healthz(_request):
        root_binding = endpoint_bindings_by_name.get("root", endpoint_bindings[0])
        return JSONResponse(
            {
                "status": "ok",
                "service": "xyn-mcp-adapter",
                "mcp_profile": root_binding.profile_name,
                "app_scope": root_binding.app_scope,
                "tool_count": len(enabled_tools),
                "tools": enabled_tools,
                "disabled_tools": disabled_tools,
                "tool_parity": parity,
                "upstream_health": upstream_health,
                "xyn_control_api_base_url": adapter.config.control_api_base_url,
                "xyn_code_api_base_url": adapter.config.code_api_base_url
                or adapter.config.control_api_base_url,
                "auth": {
                    "has_bearer_token": bool(adapter.config.bearer_token),
                    "has_internal_token": bool(adapter.config.internal_token),
                    "mcp_auth_mode": auth_config.mode,
                    "mcp_auth_token_configured": bool(auth_config.bearer_token),
                    "mcp_auth_oidc_configured": bool(auth_config.oidc_issuer and auth_config.oidc_client_id),
                },
                "runtime_identity": _runtime_identity_payload(root_binding),
                "deprecated_alias_mode_enabled": deprecated_alias_enabled,
            }
        )

    def _binding_health_handler(binding: McpEndpointBinding) -> Callable[..., Any]:
        async def _health(_request):
            payload = {
                "status": "ok",
                "service": "xyn-mcp-adapter",
                "mcp_profile": binding.profile_name,
                "app_scope": binding.app_scope,
                "tool_count": len(enabled_tools),
                "tools": enabled_tools,
                "disabled_tools": disabled_tools,
                "tool_parity": parity,
                "upstream_health": upstream_health,
                "xyn_control_api_base_url": adapter.config.control_api_base_url,
                "xyn_code_api_base_url": adapter.config.code_api_base_url or adapter.config.control_api_base_url,
                "auth": {
                    "has_bearer_token": bool(adapter.config.bearer_token),
                    "has_internal_token": bool(adapter.config.internal_token),
                    "mcp_auth_mode": auth_config.mode,
                    "mcp_auth_token_configured": bool(auth_config.bearer_token),
                    "mcp_auth_oidc_configured": bool(auth_config.oidc_issuer and auth_config.oidc_client_id),
                },
                "runtime_identity": _runtime_identity_payload(binding),
                "deprecated_alias_mode_enabled": deprecated_alias_enabled,
                "endpoint_binding": {
                    "name": binding.name,
                    "mcp_path_prefix": binding.mcp_path_prefix,
                    "resource_path": binding.resource_path,
                    "oauth_protected_resource_path": binding.oauth_protected_resource_path,
                    "health_path": binding.health_path,
                    "rewrite_to_prefix": binding.rewrite_to_prefix,
                    "environment": binding.environment,
                    "deployment_namespace": binding.deployment_namespace,
                },
            }
            return JSONResponse(payload)

        return _health

    def _base_url_for(request) -> str:
        proto = str(request.headers.get("x-forwarded-proto", "") or "").split(",")[0].strip() or str(request.url.scheme or "https")
        host = str(request.headers.get("x-forwarded-host", "") or "").split(",")[0].strip() or str(request.headers.get("host", "")).strip()
        if not host:
            host = str(request.url.netloc or "").strip()
        return f"{proto}://{host}" if host else ""

    def _oauth_protected_resource_metadata(
        request,
        *,
        resource_path: str = mcp_resource_path,
        binding: Optional[McpEndpointBinding] = None,
    ) -> Dict[str, Any]:
        base_url = _base_url_for(request)
        resource = f"{base_url}{resource_path}" if base_url else resource_path
        selected_binding = binding or endpoint_bindings_by_name.get("root", endpoint_bindings[0])
        metadata: Dict[str, Any] = {
            "resource": resource,
            "bearer_methods_supported": ["header"],
            "xyn_runtime_identity": _runtime_identity_payload(selected_binding),
        }
        if auth_config.oidc_issuer:
            metadata["authorization_servers"] = [auth_config.oidc_issuer]
        return metadata

    def _oauth_www_authenticate_header(request, *, binding: Optional[McpEndpointBinding] = None) -> str:
        params: Dict[str, str] = {"realm": "xyn-mcp"}
        if auth_config.oidc_issuer:
            params["authorization_uri"] = auth_config.oidc_issuer
        base_url = _base_url_for(request)
        selected_binding = binding or endpoint_bindings_by_name.get("root", endpoint_bindings[0])
        if base_url:
            params["resource_metadata"] = f"{base_url}{selected_binding.oauth_protected_resource_path}"
        header_value = "Bearer " + ", ".join(f'{key}="{value}"' for key, value in params.items())
        return header_value

    def _unauthorized(request, message: str, *, binding: Optional[McpEndpointBinding] = None) -> JSONResponse:
        headers = {}
        if auth_config.mode == "oidc":
            headers["WWW-Authenticate"] = _oauth_www_authenticate_header(request, binding=binding)
        return JSONResponse({"error": "unauthorized", "message": message}, status_code=401, headers=headers)

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
        timeout_seconds = min(float(adapter.config.timeout_seconds), 10.0)
        try:
            config_response = httpx.request(
                method="GET",
                url=auth_config.oidc_well_known_config_url,
                timeout=timeout_seconds,
            )
        except Exception:
            return False, "OIDC token validation failed: unable to load issuer metadata"
        if config_response.status_code >= 400:
            return False, "OIDC token validation failed: issuer metadata unavailable"
        try:
            oidc_config = config_response.json()
        except Exception:
            return False, "OIDC token validation failed: issuer metadata was not valid JSON"

        userinfo_endpoint = str(oidc_config.get("userinfo_endpoint") or "").strip()
        if not userinfo_endpoint:
            return False, "OIDC token validation failed: issuer did not provide userinfo_endpoint"
        try:
            userinfo_response = httpx.request(
                method="GET",
                url=userinfo_endpoint,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout_seconds,
            )
        except Exception:
            return False, "OIDC token validation failed: unable to reach userinfo endpoint"
        if userinfo_response.status_code >= 400:
            return False, "Invalid OIDC bearer token"
        try:
            claims = userinfo_response.json()
        except Exception:
            return False, "OIDC token validation failed: userinfo response was not valid JSON"

        audience = claims.get("aud")
        if isinstance(audience, str) and audience and audience != auth_config.oidc_client_id:
            return False, "Invalid OIDC bearer token audience"
        if isinstance(audience, list) and audience and auth_config.oidc_client_id not in [str(item) for item in audience]:
            return False, "Invalid OIDC bearer token audience"
        return True, ""

    def _oauth_protected_resource_handler(binding: McpEndpointBinding) -> Callable[..., Any]:
        async def _handler(request) -> Response:
            if auth_config.mode != "oidc":
                return Response(status_code=404)
            return JSONResponse(
                _oauth_protected_resource_metadata(request, resource_path=binding.resource_path, binding=binding),
                status_code=200,
            )

        return _handler

    def _binding_for_request_path(path: str) -> Optional[McpEndpointBinding]:
        matched: Optional[McpEndpointBinding] = None
        matched_len = -1
        for binding in endpoint_bindings:
            prefix = binding.mcp_path_prefix
            if path == prefix or path.startswith(f"{prefix}/"):
                if len(prefix) > matched_len:
                    matched = binding
                    matched_len = len(prefix)
        return matched

    public_paths = {
        binding.health_path for binding in endpoint_bindings
    } | {binding.oauth_protected_resource_path for binding in endpoint_bindings}

    async def _mcp_auth_guard(request, call_next):
        original_path = str(request.url.path or "")
        matched_binding = _binding_for_request_path(original_path)
        path = original_path
        if matched_binding is not None:
            normalized_prefix = matched_binding.mcp_path_prefix.rstrip("/") or "/"
            suffix = path[len(normalized_prefix) :]
            if not suffix.startswith("/"):
                suffix = f"/{suffix}" if suffix else ""
            rewritten_path = (matched_binding.rewrite_to_prefix.rstrip("/") or "/") + suffix
            if rewritten_path != path:
                request.scope["path"] = rewritten_path
                request.scope["raw_path"] = rewritten_path.encode("utf-8")
                path = rewritten_path
        if original_path in public_paths or not path.startswith("/mcp"):
            return await call_next(request)
        if auth_config.mode == "none":
            return await call_next(request)
        token = _extract_bearer_token(request.headers.get("Authorization", ""))
        if not token:
            return _unauthorized(
                request,
                "Missing Authorization: Bearer <token> header",
                binding=matched_binding,
            )
        token_ctx = None
        if auth_config.mode == "token":
            if not auth_config.bearer_token:
                return _unauthorized(
                    request,
                    "MCP auth token mode is enabled but XYN_MCP_AUTH_BEARER_TOKEN is not configured",
                    binding=matched_binding,
                )
            if not secrets.compare_digest(token, auth_config.bearer_token):
                return _unauthorized(request, "Invalid bearer token", binding=matched_binding)
            token_ctx = set_request_bearer_token(token)
            try:
                return await call_next(request)
            finally:
                if token_ctx is not None:
                    reset_request_bearer_token(token_ctx)
        ok, message = await _validate_oidc_bearer(token)
        if not ok:
            return _unauthorized(request, message, binding=matched_binding)
        token_ctx = set_request_bearer_token(token)
        try:
            return await call_next(request)
        finally:
            if token_ctx is not None:
                reset_request_bearer_token(token_ctx)
    app.add_middleware(BaseHTTPMiddleware, dispatch=_mcp_auth_guard)

    # Add diagnostics routes directly on the same MCP Starlette app so lifespan/task-group init stays intact.
    app.add_route("/healthz", healthz, methods=["GET"])
    seen_health_paths = {"/healthz"}
    seen_oauth_paths = set()
    for binding in endpoint_bindings:
        if binding.health_path not in seen_health_paths:
            app.add_route(binding.health_path, _binding_health_handler(binding), methods=["GET"])
            seen_health_paths.add(binding.health_path)
        if binding.oauth_protected_resource_path not in seen_oauth_paths:
            app.add_route(
                binding.oauth_protected_resource_path,
                _oauth_protected_resource_handler(binding),
                methods=["GET"],
            )
            seen_oauth_paths.add(binding.oauth_protected_resource_path)
    return app


def create_xyn_mcp_http_app(adapter: XynApiAdapter | None = None) -> Starlette:
    configured_adapter = adapter or XynApiAdapter(XynApiAdapterConfig.from_env())
    auth_config = McpAuthConfig.from_env()
    return _create_xyn_mcp_http_subapp(
        adapter=configured_adapter,
        auth_config=auth_config,
        mcp_resource_path="/mcp",
        profile_name="root",
    )

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
