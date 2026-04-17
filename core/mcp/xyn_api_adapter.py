from __future__ import annotations

import os
import logging
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx
from core.artifact_code_review import (
    analyze_codebase,
    build_hierarchical_tree,
    build_source_index,
    compute_module_metrics,
    FilePathNotFoundError,
    parse_artifact_source_files,
    read_file_chunk,
    search_files,
)
from core.artifact_source_resolution import (
    parse_packaged_artifact_metadata,
    resolve_artifact_source,
)

_REQUEST_BEARER_TOKEN: ContextVar[str] = ContextVar("xyn_mcp_request_bearer_token", default="")
_SOURCE_FALLBACK_STATUS_CODES = {400, 401, 403, 404, 422, 429}
_SOURCE_FALLBACK_STATUS_MIN = 500
_SOURCE_FALLBACK_STATUS_MAX = 599
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_TRANSIENT_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MCP_FAILURE_CLASSES = {
    "binding_rotated",
    "empty_tool_surface",
    "auth_expired",
    "backend_validation_error",
    "backend_server_error",
    "contract_mismatch",
    "transient_transport_failure",
    "application_not_found",
    "artifact_not_found",
    "workspace_forbidden",
    "scope_resolution_failed",
    "unsupported_scope_mode",
    "unknown_mcp_failure",
}

logger = logging.getLogger(__name__)

_RUNTIME_JSON_PATH_PATTERN = re.compile(
    r"^/(?:xyn/api/|api/v1/)?(?:runs|runtime-runs)(?:/|$)",
    re.IGNORECASE,
)
_WORKSPACE_ROLE_SYSTEM_PLATFORM = "system_platform"
_WORKSPACE_ROLE_DEFAULT_USER = "default_user"
_WORKSPACE_ROLE_USER_VISIBLE = "user_visible"
_SYSTEM_ARTIFACT_SLUGS = {"xyn-api", "xyn-ui", "core.workbench"}
_ARTIFACT_SOURCE_TREE_PATHS = ["/xyn/api/artifacts/source-tree", "/api/v1/artifacts/source-tree"]
_ARTIFACT_SOURCE_FILE_PATHS = ["/xyn/api/artifacts/source-file", "/api/v1/artifacts/source-file"]
_ARTIFACT_SOURCE_SEARCH_PATHS = ["/xyn/api/artifacts/source-search", "/api/v1/artifacts/source-search"]
_ARTIFACT_ANALYZE_CODEBASE_PATHS = ["/xyn/api/artifacts/analyze-codebase", "/api/v1/artifacts/analyze-codebase"]
_ARTIFACT_ANALYZE_PYTHON_API_PATHS = ["/xyn/api/artifacts/analyze-python-api", "/api/v1/artifacts/analyze-python-api"]
_ARTIFACT_MODULE_METRICS_PATHS = ["/xyn/api/artifacts/module-metrics", "/api/v1/artifacts/module-metrics"]


def set_request_bearer_token(token: str) -> Token:
    return _REQUEST_BEARER_TOKEN.set(str(token or "").strip())


def reset_request_bearer_token(token: Token) -> None:
    _REQUEST_BEARER_TOKEN.reset(token)


def get_request_bearer_token() -> str:
    return _REQUEST_BEARER_TOKEN.get()


@dataclass(frozen=True)
class XynApiAdapterConfig:
    control_api_base_url: str
    bearer_token: str
    internal_token: str
    cookie: str
    timeout_seconds: float
    upstream_host_header: str = ""
    upstream_forwarded_proto: str = ""
    code_api_base_url: str = ""
    default_workspace_id: str = ""

    @classmethod
    def from_env(cls) -> "XynApiAdapterConfig":
        public_base_url = str(os.getenv("XYN_PUBLIC_BASE_URL", "")).strip()
        parsed_public = urlparse(public_base_url) if public_base_url else None
        derived_host = str(parsed_public.netloc or "").strip() if parsed_public else ""
        if ":" in derived_host:
            derived_host = derived_host.split(":", 1)[0].strip()
        derived_proto = str(parsed_public.scheme or "").strip() if parsed_public else ""
        legacy_api_base_url = str(os.getenv("XYN_MCP_XYN_API_BASE_URL", "")).strip()
        return cls(
            control_api_base_url=str(os.getenv("XYN_MCP_XYN_CONTROL_API_BASE_URL", "")).strip()
            or legacy_api_base_url
            or "http://localhost:8001",
            code_api_base_url=str(os.getenv("XYN_MCP_XYN_CODE_API_BASE_URL", "")).strip(),
            bearer_token=str(os.getenv("XYN_MCP_XYN_API_BEARER_TOKEN", "")).strip()
            or str(os.getenv("XYN_MCP_AUTH_BEARER_TOKEN", "")).strip(),
            internal_token=str(os.getenv("XYN_MCP_INTERNAL_TOKEN", "")).strip(),
            # Deprecated: browser session-cookie forwarding is retained only as
            # a temporary fallback; bearer token propagation is the canonical path.
            cookie=str(os.getenv("XYN_MCP_COOKIE", "")).strip(),
            timeout_seconds=float(os.getenv("XYN_MCP_TIMEOUT_SECONDS", "30").strip() or "30"),
            upstream_host_header=str(os.getenv("XYN_MCP_UPSTREAM_HOST_HEADER", "")).strip() or derived_host,
            upstream_forwarded_proto=str(os.getenv("XYN_MCP_UPSTREAM_FORWARDED_PROTO", "")).strip() or derived_proto,
            default_workspace_id=str(os.getenv("XYN_MCP_WORKSPACE_ID", "")).strip(),
        )


@dataclass
class McpBindingState:
    binding_id: str = ""
    base_url: str = ""
    resolved_at: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    failure_reason: str = ""
    tool_surface_count: int = 0


@dataclass(frozen=True)
class ChangeSessionHandle:
    application_id: str
    session_id: str
    workspace_id: str = ""
    artifact_scope: tuple[str, ...] = ()
    last_known_binding_id: str = ""


class XynApiAdapter:
    """Thin HTTP adapter over existing Xyn API/control/evidence endpoints."""

    def __init__(self, config: XynApiAdapterConfig):
        self._config = config
        self._planner_preferred_base_url = ""
        self._planner_binding_state = McpBindingState()
        self._session_last_success_read_at: dict[str, float] = {}
        self._session_last_failure_classification: dict[str, str] = {}
        self._resolved_default_workspace_id: str = ""

    @property
    def config(self) -> XynApiAdapterConfig:
        return self._config

    @staticmethod
    def _now_ts() -> float:
        import time

        return float(time.time())

    def _planner_binding_snapshot(self) -> Dict[str, Any]:
        state = self._planner_binding_state
        return {
            "binding_id": str(state.binding_id or ""),
            "base_url": str(state.base_url or ""),
            "resolved_at": float(state.resolved_at or 0.0),
            "last_success_at": float(state.last_success_at or 0.0),
            "last_failure_at": float(state.last_failure_at or 0.0),
            "failure_reason": str(state.failure_reason or ""),
            "tool_surface_count": int(state.tool_surface_count or 0),
        }

    @staticmethod
    def _session_state_key(application_id: str, session_id: str) -> str:
        return f"{str(application_id or '').strip()}::{str(session_id or '').strip()}"

    @staticmethod
    def _session_context_from_paths(paths: list[str]) -> Dict[str, str]:
        for path in paths:
            token = str(path or "").strip()
            match = re.search(r"/applications/([^/]+)/change-sessions/([^/]+)", token)
            if match:
                return {
                    "application_id": str(match.group(1) or "").strip(),
                    "session_id": str(match.group(2) or "").strip(),
                }
        return {"application_id": "", "session_id": ""}

    def _build_change_session_handle(
        self,
        *,
        application_id: str,
        session_id: str,
        workspace_id: str = "",
        artifact_scope: Optional[list[str]] = None,
        last_known_binding_id: str = "",
    ) -> ChangeSessionHandle:
        return ChangeSessionHandle(
            application_id=str(application_id or "").strip(),
            session_id=str(session_id or "").strip(),
            workspace_id=str(workspace_id or "").strip(),
            artifact_scope=tuple(str(item or "").strip() for item in (artifact_scope or []) if str(item or "").strip()),
            last_known_binding_id=str(last_known_binding_id or "").strip(),
        )

    @staticmethod
    def _change_session_control_paths(handle: ChangeSessionHandle, *, suffix: str = "") -> list[str]:
        normalized_suffix = f"/{str(suffix or '').lstrip('/')}" if str(suffix or "").strip() else ""
        paths: list[str] = []
        if str(handle.session_id or "").strip():
            session_base = f"/change-sessions/{handle.session_id}"
            paths.append(f"/xyn/api{session_base}{normalized_suffix}")
            paths.append(f"/api/v1{session_base}{normalized_suffix}")
        if str(handle.application_id or "").strip():
            base = f"/applications/{handle.application_id}/change-sessions/{handle.session_id}"
            paths.append(f"/xyn/api{base}{normalized_suffix}")
            paths.append(f"/api/v1{base}{normalized_suffix}")
        return paths

    def _planner_binding_reordered_bases(
        self,
        *,
        base_urls: list[str],
        force_refresh: bool = False,
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in base_urls:
            token = str(item or "").strip()
            if token and token not in seen:
                seen.add(token)
                ordered.append(token)
        if not ordered:
            return []
        preferred = str(self._planner_binding_state.base_url or self._planner_preferred_base_url or "").strip()
        if not preferred or preferred not in ordered:
            return ordered
        if force_refresh:
            return [base for base in ordered if base != preferred] + [preferred]
        return [preferred] + [base for base in ordered if base != preferred]

    def _record_planner_binding_success(
        self,
        *,
        base_url: str,
        path: str,
        response: Optional[Dict[str, Any]] = None,
    ) -> None:
        ts = self._now_ts()
        payload = response if isinstance(response, dict) else {}
        control = payload.get("control") if isinstance(payload.get("control"), dict) else {}
        next_actions = payload.get("next_allowed_actions") if isinstance(payload.get("next_allowed_actions"), list) else []
        if not next_actions and isinstance(control, dict):
            next_actions = control.get("next_allowed_actions") if isinstance(control.get("next_allowed_actions"), list) else []
        surface_count = len(next_actions)
        previous_surface_count = int(self._planner_binding_state.tool_surface_count or 0)
        self._planner_binding_state = McpBindingState(
            binding_id=self._binding_id(base_url, path),
            base_url=str(base_url or "").strip(),
            resolved_at=float(self._planner_binding_state.resolved_at or ts),
            last_success_at=ts,
            last_failure_at=float(self._planner_binding_state.last_failure_at or 0.0),
            failure_reason="",
            tool_surface_count=surface_count if surface_count > 0 else previous_surface_count,
        )
        self._planner_preferred_base_url = str(base_url or "").strip()

    def _record_planner_binding_failure(self, *, reason: str) -> None:
        ts = self._now_ts()
        self._planner_binding_state = McpBindingState(
            binding_id=str(self._planner_binding_state.binding_id or ""),
            base_url=str(self._planner_binding_state.base_url or ""),
            resolved_at=float(self._planner_binding_state.resolved_at or ts),
            last_success_at=float(self._planner_binding_state.last_success_at or 0.0),
            last_failure_at=ts,
            failure_reason=str(reason or "").strip(),
            tool_surface_count=int(self._planner_binding_state.tool_surface_count or 0),
        )

    def _planner_surface_is_empty(self, response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        control = response.get("control") if isinstance(response.get("control"), dict) else {}
        session = control.get("session") if isinstance(control.get("session"), dict) else {}
        planning = session.get("planning") if isinstance(session.get("planning"), dict) else {}
        next_actions = response.get("next_allowed_actions") if isinstance(response.get("next_allowed_actions"), list) else []
        if not next_actions and isinstance(control, dict):
            next_actions = control.get("next_allowed_actions") if isinstance(control.get("next_allowed_actions"), list) else []
        if next_actions:
            return False
        if bool(planning.get("pending_prompt")) or bool(planning.get("pending_question")) or bool(planning.get("pending_option_set")):
            return False
        # Treat explicit empty surfaces as stale only if we previously had a non-empty surface.
        return bool(int(self._planner_binding_state.tool_surface_count or 0) > 0)

    def _headers(self, *, prefer_request_bearer: bool = True) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        # Prefer per-request bearer propagated by MCP auth middleware so OAuth sessions
        # can flow through ChatGPT -> MCP -> Xyn backend. Fall back to static config token.
        request_bearer = get_request_bearer_token() if prefer_request_bearer else ""
        bearer = request_bearer or self._config.bearer_token
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if self._config.internal_token:
            headers["X-Internal-Token"] = self._config.internal_token
        # Deprecated fallback; do not rely on browser-cookie auth in production.
        if self._config.cookie:
            headers["Cookie"] = self._config.cookie
        if self._config.upstream_host_header:
            headers["Host"] = self._config.upstream_host_header
            headers["X-Forwarded-Host"] = self._config.upstream_host_header
        if self._config.upstream_forwarded_proto:
            headers["X-Forwarded-Proto"] = self._config.upstream_forwarded_proto
        return headers

    @staticmethod
    def _binding_id(base_url: str, path: str) -> str:
        return f"{str(base_url or '').rstrip('/')}{str(path or '').strip()}"

    @staticmethod
    def _is_runtime_json_path(path: str) -> bool:
        normalized = str(path or "").strip()
        return bool(_RUNTIME_JSON_PATH_PATTERN.match(normalized))

    @staticmethod
    def _looks_like_html(raw_text: str) -> bool:
        token = str(raw_text or "").strip().lower()
        if not token:
            return False
        return token.startswith("<!doctype html") or token.startswith("<html") or "<body" in token

    @staticmethod
    def _classify_error(result: Dict[str, Any]) -> str:
        current = str(result.get("error_classification") or "").strip()
        if current in _MCP_FAILURE_CLASSES:
            return current
        status_code = int(result.get("status_code") or 0)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        error_token = str(body.get("error") or "").strip().lower()
        blocked_reason = str(body.get("blocked_reason") or "").strip().lower()
        raw_text = str(body.get("raw_text") or "").strip().lower()
        detail_text = str(body.get("detail") or "").strip().lower()

        if error_token in {"upstream_unreachable"}:
            return "transient_transport_failure"
        if error_token in {"empty_surface", "empty_tool_surface"} or blocked_reason in {"empty_surface", "empty_tool_surface"}:
            return "empty_tool_surface"
        if blocked_reason in {"artifact_not_found"}:
            return "artifact_not_found"
        if blocked_reason in {"application_not_found"}:
            return "application_not_found"
        if error_token == "workspace_forbidden" or blocked_reason == "workspace_forbidden":
            return "workspace_forbidden"
        if blocked_reason in {"scope_resolution_failed", "unsupported_scope_mode"}:
            return blocked_reason
        if status_code in {401, 403} or error_token in {"unauthorized", "not authenticated", "not_authenticated"}:
            return "auth_expired"
        if blocked_reason in {"planner_route_unavailable", "route_unavailable", "schema_mismatch"}:
            return "contract_mismatch"
        if status_code in {404, 405}:
            return "binding_rotated"
        if status_code in {400, 409, 422}:
            return "backend_validation_error"
        if status_code in {429}:
            return "transient_transport_failure"
        if status_code in _TRANSIENT_RETRYABLE_STATUS_CODES:
            return "backend_server_error"
        if status_code >= 500:
            return "backend_server_error"
        if "<!doctype html" in raw_text or "page not found" in raw_text or detail_text == "internal server error":
            return "backend_server_error"
        return "unknown_mcp_failure"

    def _control_api_base_urls(self) -> list[str]:
        out: list[str] = []
        control_api = str(self._config.control_api_base_url or "").strip()
        if control_api:
            out.append(control_api)
            parsed = urlparse(control_api)
            host = str(parsed.hostname or "").strip().lower()
            port = f":{parsed.port}" if parsed.port else ""
            scheme = str(parsed.scheme or "http").strip() or "http"
            derived_hosts: list[str] = []
            if host == "xyn-local-api":
                derived_hosts.extend(["xyn-api", "local-api"])
            elif host == "local-api":
                derived_hosts.extend(["xyn-local-api", "xyn-api"])
            elif host == "xyn-api":
                derived_hosts.extend(["xyn-local-api", "local-api"])
            for candidate_host in derived_hosts:
                candidate = f"{scheme}://{candidate_host}{port}"
                if candidate not in out:
                    out.append(candidate)
        seed_base = str(os.getenv("XYN_SEED_URL", "")).strip().rstrip("/")
        if seed_base and seed_base not in out:
            out.append(seed_base)
        public_base = str(os.getenv("XYN_PUBLIC_BASE_URL", "")).strip().rstrip("/")
        if public_base and public_base not in out:
            out.append(public_base)
        return out

    @staticmethod
    def _is_planner_path(path: str) -> bool:
        normalized = str(path or "").strip()
        return (
            normalized.startswith("/xyn/api/applications")
            or normalized.startswith("/api/v1/applications")
            or normalized.startswith("/xyn/api/change-sessions")
            or normalized.startswith("/api/v1/change-sessions")
        )

    def _planner_base_urls(self) -> list[str]:
        base_urls = [base for base in self._control_api_base_urls() if str(base or "").strip()]
        return self._planner_binding_reordered_bases(base_urls=base_urls, force_refresh=False)

    @staticmethod
    def _normalize_planner_route_error(result: Dict[str, Any], *, attempted_paths: list[str]) -> Dict[str, Any]:
        if bool(result.get("ok")):
            return result
        if not str(result.get("error_classification") or "").strip():
            body = result.get("response") if isinstance(result.get("response"), dict) else {}
            status_code = int(result.get("status_code") or 0)
            blocked_reason = str(body.get("blocked_reason") or "").strip().lower()
            if blocked_reason == "artifact_not_found":
                result["error_classification"] = "artifact_not_found"
            elif blocked_reason == "application_not_found":
                result["error_classification"] = "application_not_found"
            elif blocked_reason in {"scope_resolution_failed", "unsupported_scope_mode"}:
                result["error_classification"] = blocked_reason
            elif status_code in {401, 403}:
                result["error_classification"] = "auth_expired"
            elif status_code in {404, 405}:
                result["error_classification"] = "binding_rotated"
            elif status_code in {400, 409, 422}:
                result["error_classification"] = "backend_validation_error"
            elif status_code in _TRANSIENT_RETRYABLE_STATUS_CODES or str(body.get("error") or "") == "upstream_unreachable":
                result["error_classification"] = "backend_server_error"
        status_code = int(result.get("status_code") or 0)
        if status_code != 404:
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        raw_text = str(body.get("raw_text") or "").strip().lower()
        detail_text = str(body.get("detail") or "").strip().lower()
        is_route_miss = (
            "page not found" in raw_text
            or "<!doctype html" in raw_text
            or detail_text == "not found"
        )
        if not is_route_miss:
            return result
        looks_like_session_route = any("/change-sessions/" in str(p or "") for p in attempted_paths)
        if looks_like_session_route:
            result["response"] = {
                "error": "stale_binding_path",
                "blocked_reason": "binding_rotated",
                "detail": "The active change-session route appears stale after binding rotation.",
                "attempted_paths": [str(p or "").strip() for p in attempted_paths if str(p or "").strip()],
                "base_url": str(result.get("base_url") or "").strip(),
                "recommended_action": "refresh_binding_and_retry",
            }
            result["error_classification"] = "binding_rotated"
            return result
        result["response"] = {
            "error": "planner_route_unavailable",
            "blocked_reason": "planner_route_unavailable",
            "detail": "Planner workflow route is not available on the configured control API upstream.",
            "attempted_paths": [str(p or "").strip() for p in attempted_paths if str(p or "").strip()],
            "base_url": str(result.get("base_url") or "").strip(),
            "recommended_action": "verify_xyn_api_control_plane_route_mount",
            "next_allowed_actions": ["list_artifacts", "list_runtime_runs"],
        }
        result["error_classification"] = "contract_mismatch"
        return result

    @staticmethod
    def _api_redirect_as_json_error(*, status_code: int, path: str, response: Any) -> Optional[Dict[str, Any]]:
        code = int(status_code or 0)
        if code not in _REDIRECT_STATUS_CODES:
            return None
        normalized_path = str(path or "").strip()
        if not (normalized_path.startswith("/xyn/api/") or normalized_path.startswith("/api/v1/")):
            return None
        location = str(getattr(response, "headers", {}).get("location", "") or "").strip()
        lowered_location = location.lower()
        looks_like_login_redirect = any(
            token in lowered_location
            for token in ("/login", "/accounts/login", "/auth/login", "/oauth", "signin", "authorize")
        )
        if not looks_like_login_redirect and code not in {302, 303}:
            return None
        return {
            "ok": False,
            "status_code": 401,
            "method": "",
            "path": normalized_path,
            "base_url": "",
            "response": {
                "error": "unauthorized",
                "blocked_reason": "interactive_login_redirect",
                "detail": "API request was redirected to an interactive login endpoint.",
                "redirect_status_code": code,
                "redirect_location": location,
            },
        }

    def _request(
        self,
        *,
        method: str,
        path: str,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        base_url: Optional[str] = None,
        allow_reissue_on_transport_error: bool = True,
    ) -> Dict[str, Any]:
        is_planner_path = self._is_planner_path(path)
        if base_url:
            resolved_base_url = str(base_url).rstrip("/")
        elif is_planner_path:
            planner_bases = self._planner_binding_reordered_bases(
                base_urls=self._control_api_base_urls(),
                force_refresh=False,
            )
            resolved_base_url = str((planner_bases[0] if planner_bases else self._config.control_api_base_url) or "").rstrip("/")
        else:
            resolved_base_url = str(self._config.control_api_base_url).rstrip("/")
        url = f"{resolved_base_url}{path}"
        request_bearer = get_request_bearer_token()
        static_bearer = str(self._config.bearer_token or "").strip()

        def _do_request(*, prefer_request_bearer: bool) -> httpx.Response:
            return httpx.request(
                method=method.upper(),
                url=url,
                headers=self._headers(prefer_request_bearer=prefer_request_bearer),
                json=json_payload,
                params=params,
                timeout=self._config.timeout_seconds,
            )

        try:
            response = _do_request(prefer_request_bearer=True)
        except httpx.RequestError as exc:
            if path.startswith("/xyn/api/") and allow_reissue_on_transport_error:
                for candidate_base in self._control_api_base_urls():
                    candidate_base = str(candidate_base or "").rstrip("/")
                    if not candidate_base or candidate_base == resolved_base_url:
                        continue
                    candidate_url = f"{candidate_base}{path}"
                    try:
                        response = httpx.request(
                            method=method.upper(),
                            url=candidate_url,
                            headers=self._headers(prefer_request_bearer=True),
                            json=json_payload,
                            params=params,
                            timeout=self._config.timeout_seconds,
                        )
                        resolved_base_url = candidate_base
                        url = candidate_url
                        logger.info(
                            "mcp_request_rebound_on_transport_error path=%s prev_binding=%s new_binding=%s",
                            path,
                            self._binding_id(base_url or self._config.control_api_base_url, path),
                            self._binding_id(candidate_base, path),
                        )
                        break
                    except httpx.RequestError:
                        continue
                else:
                    response = None
                if response is not None:
                    try:
                        body = response.json()
                    except Exception:
                        body = {"raw_text": response.text}
                    result = {
                        "ok": bool(200 <= response.status_code < 300),
                        "status_code": int(response.status_code),
                        "method": method.upper(),
                        "path": path,
                        "base_url": resolved_base_url,
                        "response": body if isinstance(body, (dict, list)) else {"value": body},
                    }
                    result["error_classification"] = self._classify_error(result) if not bool(result.get("ok")) else ""
                    return result
            result = {
                "ok": False,
                "status_code": 503,
                "method": method.upper(),
                "path": path,
                "base_url": resolved_base_url,
                "response": {
                    "error": "upstream_unreachable",
                    "blocked_reason": "upstream_unreachable",
                    "detail": str(exc),
                    "recommended_action": "verify_control_api_base_url_and_network",
                },
            }
            result["error_classification"] = "transient_transport_failure"
            if is_planner_path:
                self._record_planner_binding_failure(reason="transient_transport_failure")
            return result
        redirect_error = self._api_redirect_as_json_error(
            status_code=int(response.status_code),
            path=path,
            response=response,
        )
        if isinstance(redirect_error, dict):
            redirect_error["method"] = method.upper()
            redirect_error["base_url"] = resolved_base_url
            # Fallback: if a request-scoped bearer was present and differed from configured
            # static bearer, retry with static bearer before surfacing interactive redirect.
            should_retry_with_static = bool(request_bearer and static_bearer and request_bearer != static_bearer)
            if not should_retry_with_static:
                redirect_error["error_classification"] = "auth_expired"
                if is_planner_path:
                    self._record_planner_binding_failure(reason="auth_expired")
                return redirect_error
            try:
                retry_response = _do_request(prefer_request_bearer=False)
            except httpx.RequestError:
                redirect_error["error_classification"] = "auth_expired"
                if is_planner_path:
                    self._record_planner_binding_failure(reason="transient_transport_failure")
                return redirect_error
            retry_redirect = self._api_redirect_as_json_error(
                status_code=int(retry_response.status_code),
                path=path,
                response=retry_response,
            )
            if isinstance(retry_redirect, dict):
                retry_redirect["method"] = method.upper()
                retry_redirect["base_url"] = resolved_base_url
                retry_redirect["error_classification"] = "auth_expired"
                if is_planner_path:
                    self._record_planner_binding_failure(reason="auth_expired")
                return retry_redirect
            response = retry_response
            logger.info(
                "mcp_request_auth_refreshed method=%s path=%s binding=%s",
                method.upper(),
                path,
                self._binding_id(resolved_base_url, path),
            )
        body: Any
        try:
            body = response.json()
        except Exception:
            body = {"raw_text": response.text}
        result = {
            "ok": bool(200 <= response.status_code < 300),
            "status_code": int(response.status_code),
            "method": method.upper(),
            "path": path,
            "base_url": resolved_base_url,
            "response": body if isinstance(body, (dict, list)) else {"value": body},
        }
        if bool(result.get("ok")) and self._is_runtime_json_path(path):
            payload = result.get("response") if isinstance(result.get("response"), dict) else {}
            raw_text = str(payload.get("raw_text") or "").strip()
            content_type = str(getattr(response, "headers", {}).get("content-type", "") or "").lower()
            if self._looks_like_html(raw_text) or ("text/html" in content_type and not isinstance(body, (dict, list))):
                result = {
                    "ok": False,
                    "status_code": 502,
                    "method": method.upper(),
                    "path": path,
                    "base_url": resolved_base_url,
                    "response": {
                        "error": "runtime_route_contract_mismatch",
                        "blocked_reason": "contract_mismatch",
                        "detail": "Runtime endpoint returned HTML instead of JSON.",
                        "content_type": content_type,
                    },
                    "error_classification": "contract_mismatch",
                }
                return result
        should_retry_auth = (
            bool(request_bearer and static_bearer and request_bearer != static_bearer)
            and int(result.get("status_code") or 0) in {401, 403}
            and isinstance(result.get("response"), dict)
            and str((result.get("response") or {}).get("error") or "").strip().lower() in {"unauthorized", "not authenticated", "not_authenticated"}
        )
        if should_retry_auth:
            try:
                retry_response = _do_request(prefer_request_bearer=False)
            except httpx.RequestError:
                return result
            retry_redirect = self._api_redirect_as_json_error(
                status_code=int(retry_response.status_code),
                path=path,
                response=retry_response,
            )
            if isinstance(retry_redirect, dict):
                retry_redirect["method"] = method.upper()
                retry_redirect["base_url"] = resolved_base_url
                return retry_redirect
            try:
                retry_body = retry_response.json()
            except Exception:
                retry_body = {"raw_text": retry_response.text}
            result = {
                "ok": bool(200 <= retry_response.status_code < 300),
                "status_code": int(retry_response.status_code),
                "method": method.upper(),
                "path": path,
                "base_url": resolved_base_url,
                "response": retry_body if isinstance(retry_body, (dict, list)) else {"value": retry_body},
                "_auth_refreshed": True,
            }
            result["error_classification"] = self._classify_error(result) if not bool(result.get("ok")) else ""
            if is_planner_path and bool(result.get("ok")):
                self._record_planner_binding_success(
                    base_url=resolved_base_url,
                    path=path,
                    response=result.get("response") if isinstance(result.get("response"), dict) else {},
                )
            return result
        result["error_classification"] = self._classify_error(result) if not bool(result.get("ok")) else ""

        if is_planner_path and bool(result.get("ok")):
            if str(method or "").upper() == "GET" and self._planner_surface_is_empty(result.get("response")):
                self._record_planner_binding_failure(reason="empty_tool_surface")
            else:
                self._record_planner_binding_success(
                    base_url=resolved_base_url,
                    path=path,
                    response=result.get("response") if isinstance(result.get("response"), dict) else {},
                )
            return result

        if is_planner_path and not bool(result.get("ok")):
            classification = str(result.get("error_classification") or "").strip()
            stale_binding = classification in {"auth_expired", "binding_rotated"} or int(result.get("status_code") or 0) in {404, 405}
            if stale_binding:
                self._record_planner_binding_failure(
                    reason="auth_expired" if classification == "auth_expired" else "binding_rotated"
                )
            else:
                self._record_planner_binding_failure(reason=classification or "unknown_mcp_failure")

            safe_to_retry = str(method or "").upper() == "GET" or bool(allow_reissue_on_transport_error)
            if safe_to_retry and stale_binding:
                refreshed_bases = self._planner_binding_reordered_bases(
                    base_urls=self._control_api_base_urls(),
                    force_refresh=True,
                )
                retry_base = str((refreshed_bases[0] if refreshed_bases else "") or "").rstrip("/")
                if retry_base and retry_base != resolved_base_url:
                    retry_result = self._request(
                        method=method,
                        path=path,
                        json_payload=json_payload,
                        params=params,
                        base_url=retry_base,
                        allow_reissue_on_transport_error=False,
                    )
                    if bool(retry_result.get("ok")):
                        retry_result["continuity"] = {
                            "previous_binding_id": self._binding_id(resolved_base_url, path),
                            "new_binding_id": self._binding_id(retry_base, path),
                            "retry_reason": "binding_rotated",
                            "auth_refreshed": classification == "auth_expired",
                            "action_reissued": True,
                        }
                    return retry_result
        return result

    def _request_bytes(
        self,
        *,
        method: str,
        path: str,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        resolved_base_url = str(base_url or self._config.control_api_base_url).rstrip("/")
        url = f"{resolved_base_url}{path}"
        try:
            response = httpx.request(
                method=method.upper(),
                url=url,
                headers=self._headers(),
                json=json_payload,
                params=params,
                timeout=self._config.timeout_seconds,
            )
        except httpx.RequestError as exc:
            return {
                "ok": False,
                "status_code": 503,
                "method": method.upper(),
                "path": path,
                "base_url": resolved_base_url,
                "response": {"error": "upstream_unreachable", "detail": str(exc)},
                "content": b"",
            }
        body: Any
        try:
            body = response.json()
        except Exception:
            body = {"raw_text": response.text}
        raw_content = response.content
        if not isinstance(raw_content, (bytes, bytearray)):
            raw_content = b""
        return {
            "ok": bool(200 <= response.status_code < 300),
            "status_code": int(response.status_code),
            "method": method.upper(),
            "path": path,
            "base_url": resolved_base_url,
            "response": body if isinstance(body, (dict, list)) else {"value": body},
            "content": bytes(raw_content),
        }

    def _request_with_fallback_paths(
        self,
        *,
        method: str,
        paths: list[str],
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        base_urls: Optional[list[str]] = None,
        allow_reissue_on_transport_error: bool = True,
    ) -> Dict[str, Any]:
        # MCP failure recovery matrix:
        # - binding_rotated: re-resolve binding/base URL and retry safely.
        # - empty_tool_surface: treat as stale discovery state, refresh candidates and retry reads.
        # - auth_expired: surface auth-required; do not blind-retry mutating actions.
        # - backend_validation_error: return server validation details without retry.
        # - backend_server_error: preserve session context; avoid mutation retries unless idempotent-safe.
        # - contract_mismatch: surface schema/route mismatch details for operator fix.
        # - transient_transport_failure: bounded retry on safe reads only.
        # - unknown_mcp_failure: no retry, surface diagnostics.
        request_method = str(method or "").upper()
        session_ctx = self._session_context_from_paths(paths)
        application_id = str(session_ctx.get("application_id") or "").strip()
        session_id = str(session_ctx.get("session_id") or "").strip()
        last_result: Dict[str, Any] = {"ok": False, "status_code": 404, "response": {"error": "not_found"}}
        first_result: Optional[Dict[str, Any]] = None
        preferred_result: Optional[Dict[str, Any]] = None
        failed_bindings: list[str] = []
        deduped_base_urls: list[str] = []
        input_base_urls = list(base_urls or self._control_api_base_urls())
        for candidate in input_base_urls:
            base = str(candidate or "").strip()
            if not base or base in deduped_base_urls:
                continue
            deduped_base_urls.append(base)
        if not deduped_base_urls:
            deduped_base_urls = [self._config.control_api_base_url]
        planner_request = any(self._is_planner_path(path) for path in paths)
        if planner_request:
            deduped_base_urls = self._planner_binding_reordered_bases(
                base_urls=deduped_base_urls,
                force_refresh=False,
            )
        for base_url in deduped_base_urls:
            for path in paths:
                result = self._request(
                    method=method,
                    path=path,
                    json_payload=json_payload,
                    params=params,
                    base_url=base_url,
                    allow_reissue_on_transport_error=allow_reissue_on_transport_error,
                )
                last_result = result
                if first_result is None:
                    first_result = result
                if bool(result.get("ok")):
                    if planner_request and str(method or "").upper() == "GET" and self._planner_surface_is_empty(result.get("response")):
                        self._record_planner_binding_failure(reason="empty_tool_surface")
                        failed_bindings.append(self._binding_id(str(result.get("base_url") or base_url), path))
                        continue
                    current_binding = self._binding_id(str(result.get("base_url") or base_url), path)
                    previous_binding = failed_bindings[-1] if failed_bindings else ""
                    if previous_binding and previous_binding != current_binding:
                        continuity = {
                            "previous_binding_id": previous_binding,
                            "new_binding_id": current_binding,
                            "retry_reason": "binding_rotated",
                            "auth_refreshed": bool(result.get("_auth_refreshed")),
                            "action_reissued": True,
                        }
                        result["continuity"] = continuity
                        logger.info(
                            "mcp_binding_rotated method=%s application_id=%s session_id=%s previous_binding=%s new_binding=%s auth_refreshed=%s",
                            request_method,
                            application_id,
                            session_id,
                            previous_binding,
                            current_binding,
                            bool(result.get("_auth_refreshed")),
                        )
                    elif bool(result.get("_auth_refreshed")):
                        result["continuity"] = {
                            "previous_binding_id": current_binding,
                            "new_binding_id": current_binding,
                            "retry_reason": "auth_expired",
                            "auth_refreshed": True,
                            "action_reissued": True,
                        }
                        logger.info(
                            "mcp_auth_refreshed method=%s application_id=%s session_id=%s binding=%s",
                            request_method,
                            application_id,
                            session_id,
                            current_binding,
                        )
                    if request_method == "POST":
                        result["action_delivery_state"] = "definitely_applied"
                    if planner_request:
                        self._record_planner_binding_success(
                            base_url=str(result.get("base_url") or base_url),
                            path=path,
                            response=result.get("response") if isinstance(result.get("response"), dict) else {},
                        )
                        result["binding_state"] = self._planner_binding_snapshot()
                    return result
                code = int(result.get("status_code") or 0)
                failed_bindings.append(self._binding_id(str(result.get("base_url") or base_url), path))
                if planner_request:
                    failure_class = str(result.get("error_classification") or self._classify_error(result))
                    if failure_class == "auth_expired":
                        self._record_planner_binding_failure(reason="auth_expired")
                    elif failure_class in {"contract_mismatch", "binding_rotated"} or code in {404, 405}:
                        self._record_planner_binding_failure(reason="binding_rotated")
                    elif failure_class in {"backend_server_error", "transient_transport_failure", "backend_validation_error"}:
                        self._record_planner_binding_failure(reason=failure_class)
                    elif code >= 500:
                        self._record_planner_binding_failure(reason="backend_server_error")
                if (
                    request_method == "POST"
                    and not allow_reissue_on_transport_error
                    and code == 503
                    and str((result.get("response") if isinstance(result.get("response"), dict) else {}).get("error") or "") == "upstream_unreachable"
                ):
                    result["action_delivery_state"] = "unknown"
                    return result
                # Mutating actions: only retry when previous attempt is known not to have
                # reached a viable backend route (404/405 stale binding/route).
                if request_method == "POST" and code not in {404, 405}:
                    result["action_delivery_state"] = (
                        "unknown" if code >= 500 else "not_sent"
                    )
                    return result
                # Continue searching across endpoint/base-url variants for compatibility.
                blocked_reason = str(
                    ((result.get("response") or {}).get("blocked_reason") if isinstance(result.get("response"), dict) else "")
                    or ""
                ).strip()
                if code in {401, 403} or blocked_reason == "interactive_login_redirect":
                    if preferred_result is None:
                        preferred_result = result
                    continue
                if code in {400, 401, 403, 404, 405, 503} or blocked_reason == "interactive_login_redirect":
                    continue
                return result
        out = preferred_result or first_result or last_result
        if planner_request and str(method or "").upper() == "GET" and bool(out.get("ok")) and self._planner_surface_is_empty(out.get("response")):
            out = {
                **out,
                "ok": False,
                "status_code": 503,
                "response": {
                    "error": "empty_tool_surface",
                    "blocked_reason": "empty_tool_surface",
                    "detail": "Planner binding resolved an empty tool surface after refresh.",
                },
                "error_classification": "empty_tool_surface",
            }
            self._record_planner_binding_failure(reason="empty_tool_surface")
        if request_method == "POST" and bool(out.get("ok")):
            out["action_delivery_state"] = "definitely_applied"
        if not bool(out.get("ok")):
            out["error_classification"] = str(out.get("error_classification") or self._classify_error(out))
            if failed_bindings:
                current_binding = self._binding_id(str(out.get("base_url") or ""), str(out.get("path") or ""))
                previous_binding = failed_bindings[-1]
                if previous_binding and previous_binding != current_binding:
                    out["continuity"] = {
                        "previous_binding_id": previous_binding,
                        "new_binding_id": current_binding,
                        "retry_reason": "binding_rotated",
                        "auth_refreshed": bool(out.get("_auth_refreshed")),
                        "action_reissued": True,
                    }
                    if str(out.get("error_classification") or "") not in {
                        "auth_expired",
                        "contract_mismatch",
                        "backend_validation_error",
                        "backend_server_error",
                        "transient_transport_failure",
                    }:
                        out["error_classification"] = "binding_rotated"
        if request_method == "POST" and not bool(out.get("ok")):
            status_code = int(out.get("status_code") or 0)
            out["action_delivery_state"] = "unknown" if status_code >= 500 else "not_sent"
        if planner_request:
            out["binding_state"] = self._planner_binding_snapshot()
        if request_method == "POST":
            continuity = out.get("continuity") if isinstance(out.get("continuity"), dict) else {}
            logger.info(
                "mcp_change_session_action_result application_id=%s session_id=%s prior_binding=%s new_binding=%s retry=%s delivery_state=%s classification=%s",
                application_id,
                session_id,
                str(continuity.get("previous_binding_id") or ""),
                str(continuity.get("new_binding_id") or ""),
                bool(continuity.get("action_reissued")),
                str(out.get("action_delivery_state") or ""),
                str(out.get("error_classification") or ""),
            )
        return out

    def _code_api_base_urls(self) -> list[str]:
        out: list[str] = []
        code_api = str(self._config.code_api_base_url or "").strip()
        control_api = str(self._config.control_api_base_url or "").strip()
        if code_api:
            out.append(code_api)
        if control_api:
            parsed = urlparse(control_api)
            host = str(parsed.hostname or "").strip().lower()
            port = f":{parsed.port}" if parsed.port else ""
            scheme = str(parsed.scheme or "http").strip() or "http"
            if host:
                derived_hosts: list[str] = []
                if host == "xyn-local-api":
                    derived_hosts.extend(["xyn-core", "core"])
                elif host == "local-api":
                    derived_hosts.extend(["core", "xyn-core"])
                elif host.endswith("-local-api"):
                    derived_hosts.append(f"{host[:-len('-local-api')]}-core")
                for candidate_host in derived_hosts:
                    candidate = f"{scheme}://{candidate_host}{port}"
                    if candidate not in out:
                        out.append(candidate)
        if control_api and control_api not in out:
            out.append(control_api)
        return out

    def _resolve_artifact_record(self, *, artifact_id: str = "", artifact_slug: str = "") -> dict[str, Any]:
        resolved_id = str(artifact_id or "").strip()
        resolved_slug = str(artifact_slug or "").strip()
        if not resolved_id and not resolved_slug:
            return {}
        listing = self.list_artifacts(limit=500, offset=0)
        if not listing.get("ok"):
            return {}
        body = listing.get("response") if isinstance(listing.get("response"), dict) else {}
        rows = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
        slug_matches: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = str(row.get("id") or "").strip()
            row_slug = str(row.get("slug") or "").strip()
            if resolved_id and row_id == resolved_id:
                return row
            if resolved_slug and row_slug == resolved_slug:
                slug_matches.append(row)
        if len(slug_matches) == 1:
            return slug_matches[0]
        if len(slug_matches) > 1:
            return {
                "_resolution_error": "artifact_slug_ambiguous",
                "slug": resolved_slug,
                "matches": [
                    {
                        "id": str(item.get("id") or ""),
                        "slug": str(item.get("slug") or ""),
                        "title": str(item.get("title") or ""),
                        "artifact_type": str(item.get("artifact_type") or ""),
                        "status": str(item.get("status") or ""),
                    }
                    for item in slug_matches
                ],
            }
        return {}

    @staticmethod
    def _should_source_fallback(status_code: int) -> bool:
        code = int(status_code or 0)
        return code in _SOURCE_FALLBACK_STATUS_CODES or (_SOURCE_FALLBACK_STATUS_MIN <= code <= _SOURCE_FALLBACK_STATUS_MAX)

    @staticmethod
    def _slug_ambiguity_error(*, artifact_slug: str, matches: list[dict[str, Any]]) -> Dict[str, Any]:
        return {
            "ok": False,
            "status_code": 409,
            "method": "GET",
            "path": "/api/v1/artifacts/source-tree",
            "response": {
                "error": "artifact_slug_ambiguous",
                "blocked_reason": "artifact_slug_ambiguous",
                "recommended_action": "retry_with_artifact_id",
                "artifact_slug": str(artifact_slug or ""),
                "candidates": matches[:20],
                "next_allowed_actions": ["list_artifacts", "get_artifact", "get_artifact_source_tree"],
            },
        }

    def _artifact_files_via_export_package(
        self,
        *,
        artifact_id: str = "",
        artifact_slug: str = "",
    ) -> Optional[dict[str, Any]]:
        artifact_row = self._resolve_artifact_record(artifact_id=artifact_id, artifact_slug=artifact_slug)
        if str(artifact_row.get("_resolution_error") or "") == "artifact_slug_ambiguous":
            return {
                "_resolution_error": "artifact_slug_ambiguous",
                "artifact_slug": str(artifact_slug or ""),
                "matches": artifact_row.get("matches") if isinstance(artifact_row.get("matches"), list) else [],
            }
        resolved_artifact_id = str(artifact_row.get("id") or artifact_id or "").strip()
        resolved_artifact_slug = str(artifact_row.get("slug") or artifact_slug or "").strip()
        if not resolved_artifact_id:
            return None
        export = self._request_bytes(
            method="POST",
            path=f"/xyn/api/artifacts/{resolved_artifact_id}/export-package",
            json_payload={},
            base_url=self._config.control_api_base_url,
        )
        if not export.get("ok"):
            return None
        payload = export.get("content")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            return None
        packaged_files = parse_artifact_source_files(
            artifact_name=str(resolved_artifact_slug or resolved_artifact_id),
            artifact_bytes=bytes(payload),
        )
        if not packaged_files:
            return None
        packaged_metadata = parse_packaged_artifact_metadata(packaged_files)
        if not resolved_artifact_slug:
            resolved_artifact_slug = str(packaged_metadata.get("slug") or "").strip()
        artifact_reference = (
            artifact_row.get("artifact_reference")
            if isinstance(artifact_row.get("artifact_reference"), dict)
            else {}
        )
        source_ref_type = str(
            packaged_metadata.get("source_ref_type")
            or artifact_reference.get("source_ref_type")
            or ""
        ).strip()
        source_ref_id = str(
            packaged_metadata.get("source_ref_id")
            or artifact_reference.get("source_ref_id")
            or ""
        ).strip()
        resolved = resolve_artifact_source(
            artifact_slug=resolved_artifact_slug,
            artifact_id=resolved_artifact_id,
            source_ref_type=source_ref_type,
            source_ref_id=source_ref_id,
            metadata=packaged_metadata,
            packaged_files=packaged_files,
        )
        return {
            "artifact_id": resolved_artifact_id,
            "artifact_slug": resolved_artifact_slug,
            "files": resolved.files,
            "source_mode": resolved.source_mode,
            "source_origin": resolved.source_origin,
            "resolution_branch": resolved.resolution_branch,
            "resolution_details": resolved.resolution_details,
            "provenance": resolved.provenance,
            "resolved_source_roots": resolved.resolved_source_roots,
            "warnings": resolved.warnings,
        }

    @staticmethod
    def _with_release_target_not_found_hint(result: Dict[str, Any], *, target_id: str) -> Dict[str, Any]:
        if int(result.get("status_code") or 0) != 404:
            return result
        response_body = result.get("response")
        if not isinstance(response_body, dict):
            response_body = {}
        warnings = response_body.get("warnings") if isinstance(response_body.get("warnings"), list) else []
        warning = (
            "Release target not found for the provided target_id. "
            "Use list_release_targets to fetch current ids, then retry with a listed id."
        )
        if warning not in warnings:
            warnings.append(warning)
        response_body["warnings"] = warnings
        response_body.setdefault("blocked_reason", "release_target_not_found")
        response_body.setdefault("recommended_action", "refresh_release_targets_and_retry")
        response_body.setdefault(
            "next_allowed_actions",
            ["list_release_targets", "get_release_target", "get_release_target_deployment_plan"],
        )
        response_body.setdefault("target_id", str(target_id or ""))
        result["response"] = response_body
        return result

    @staticmethod
    def _with_artifact_not_found_hint(
        result: Dict[str, Any],
        *,
        artifact_id: str = "",
        artifact_slug: str = "",
    ) -> Dict[str, Any]:
        if int(result.get("status_code") or 0) != 404:
            return result
        response_body = result.get("response")
        if not isinstance(response_body, dict):
            response_body = {}
        warnings = response_body.get("warnings") if isinstance(response_body.get("warnings"), list) else []
        warning = (
            "Artifact not found in the source-review backend for the provided identifier. "
            "Use list_artifacts, then retry with an id returned from that call."
        )
        if warning not in warnings:
            warnings.append(warning)
        response_body["warnings"] = warnings
        response_body.setdefault("blocked_reason", "artifact_not_found")
        response_body.setdefault("recommended_action", "refresh_artifacts_and_retry")
        response_body.setdefault("next_allowed_actions", ["list_artifacts", "get_artifact_source_tree"])
        response_body.setdefault("artifact_id", str(artifact_id or ""))
        response_body.setdefault("artifact_slug", str(artifact_slug or ""))
        result["response"] = response_body
        return result

    @staticmethod
    def _release_target_discovery_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        dns = payload.get("dns") if isinstance(payload.get("dns"), dict) else {}
        topology = payload.get("topology") if isinstance(payload.get("topology"), dict) else {}
        artifact_binding = payload.get("artifact_binding") if isinstance(payload.get("artifact_binding"), dict) else {}
        provider_binding = payload.get("provider_binding") if isinstance(payload.get("provider_binding"), dict) else {}
        return {
            "id": str(payload.get("id") or ""),
            "provider": {
                "runtime_transport": str(runtime.get("transport") or ""),
                "runtime_type": str(runtime.get("type") or ""),
                "dns_provider": str(dns.get("provider") or ""),
                "provider_key": str(provider_binding.get("provider_key") or ""),
                "module_fqn": str(provider_binding.get("module_fqn") or ""),
            },
            "artifact_reference": {
                "blueprint_id": str(payload.get("blueprint_id") or ""),
                "artifact_id": str(artifact_binding.get("artifact_id") or ""),
                "artifact_slug": str(artifact_binding.get("artifact_slug") or ""),
                "artifact_family_id": str(artifact_binding.get("artifact_family_id") or ""),
            },
            "configuration_summary": {
                "name": str(payload.get("name") or ""),
                "environment": str(payload.get("environment") or ""),
                "fqdn": str(payload.get("fqdn") or ""),
                "target_instance_id": str(payload.get("target_instance_id") or ""),
                "topology_kind": str(topology.get("kind") or ""),
            },
            "status": str(payload.get("status") or payload.get("execution_status") or payload.get("state") or ""),
        }

    @staticmethod
    def _artifact_discovery_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        artifact_type = payload.get("artifact_type") if isinstance(payload.get("artifact_type"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        artifact_title = str(payload.get("title") or "").strip()
        artifact_identifier = str(payload.get("artifact_id") or payload.get("id") or "").strip()
        inferred_slug = str(
            payload.get("slug")
            or metadata.get("generated_artifact_slug")
            or artifact_title
            or payload.get("name")
            or payload.get("label")
            or artifact_identifier
            or ""
        )
        return {
            "id": artifact_identifier,
            "slug": inferred_slug,
            "title": str(artifact_title or payload.get("name") or payload.get("label") or inferred_slug),
            "artifact_type": str(
                artifact_type.get("slug")
                or payload.get("kind")
                or payload.get("type")
                or ""
            ),
            "status": str(payload.get("artifact_state") or payload.get("status") or payload.get("sync_state") or ""),
            "artifact_reference": {
                "source_ref_type": str(payload.get("source_ref_type") or ""),
                "source_ref_id": str(payload.get("source_ref_id") or ""),
            },
        }

    @staticmethod
    def _extract_preview_urls(payload: Any) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            token = str(value or "").strip()
            if not token:
                return
            if token in seen:
                return
            seen.add(token)
            out.append(token)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in {"preview_url", "url", "preview"} and isinstance(inner, str):
                        add(inner)
                    elif normalized_key == "preview_urls" and isinstance(inner, list):
                        for item in inner:
                            add(item)
                    walk(inner)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return out

    @staticmethod
    def _extract_commit_shas(payload: Any) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        candidate_keys = {
            "commit_sha",
            "merge_commit_sha",
            "target_commit_sha",
            "head_commit_sha",
            "base_commit_sha",
            "promotion_commit_sha",
        }

        def add(value: Any) -> None:
            token = str(value or "").strip()
            if not token:
                return
            if token in seen:
                return
            seen.add(token)
            out.append(token)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in candidate_keys:
                        add(inner)
                    walk(inner)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return out

    @staticmethod
    def _extract_changed_files(payload: Any) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        candidate_keys = {
            "changed_file",
            "changed_files",
            "files_changed",
            "affected_file",
            "affected_files",
            "files_touched",
            "modified_files",
            "touched_files",
            "paths",
        }

        def add(value: Any) -> None:
            token = str(value or "").strip()
            if not token or token in seen:
                return
            seen.add(token)
            out.append(token)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in candidate_keys:
                        if isinstance(inner, list):
                            for item in inner:
                                if isinstance(item, dict):
                                    candidate = item.get("path") or item.get("file") or item.get("name")
                                    add(candidate)
                                else:
                                    add(item)
                        elif isinstance(inner, dict):
                            candidate = inner.get("path") or inner.get("file") or inner.get("name")
                            add(candidate)
                        else:
                            add(inner)
                    walk(inner)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return out

    @staticmethod
    def _extract_string_list(payload: Any, keys: set[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            token = str(value or "").strip()
            if not token or token in seen:
                return
            seen.add(token)
            out.append(token)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in keys:
                        if isinstance(inner, list):
                            for item in inner:
                                if isinstance(item, dict):
                                    add(item.get("name") or item.get("path") or item.get("route") or item.get("import"))
                                else:
                                    add(item)
                        elif isinstance(inner, dict):
                            add(inner.get("name") or inner.get("path") or inner.get("route") or inner.get("import"))
                        else:
                            add(inner)
                    walk(inner)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return out

    @staticmethod
    def _extract_decomposition_campaign(payload: Any) -> Dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        candidates: list[Dict[str, Any]] = []
        for key in ("decomposition_campaign", "campaign", "metadata", "session_metadata", "decomposition"):
            value = body.get(key)
            if isinstance(value, dict):
                candidates.append(value)
        campaign: Dict[str, Any] = {}
        for source in candidates:
            if str(source.get("kind") or "").strip().lower() in {"decomposition", "xyn_api_decomposition"}:
                campaign = source
                break
        if not campaign:
            campaign = candidates[0] if candidates else {}
        target_files = campaign.get("target_source_files")
        if not isinstance(target_files, list):
            target_files = campaign.get("target_files")
        seams = campaign.get("extraction_seams")
        moved = campaign.get("moved_handlers_modules")
        if not isinstance(moved, list):
            moved = campaign.get("moved_modules")
        required_tests = campaign.get("required_test_suites")
        return {
            "kind": str(campaign.get("kind") or "xyn_api_decomposition"),
            "target_source_files": [str(item).strip() for item in (target_files or []) if str(item).strip()],
            "extraction_seams": [str(item).strip() for item in (seams or []) if str(item).strip()],
            "moved_handlers_modules": [str(item).strip() for item in (moved or []) if str(item).strip()],
            "required_test_suites": [str(item).strip() for item in (required_tests or []) if str(item).strip()],
            "promotion_readiness": str(campaign.get("promotion_readiness") or "unknown"),
        }

    @staticmethod
    def _extract_decomposition_guardrails(payload: Any) -> Dict[str, Any]:
        body = payload if isinstance(payload, dict) else {}
        changed_routes = XynApiAdapter._extract_string_list(body, {"changed_routes", "route_changes", "route_inventory_delta"})
        changed_imports = XynApiAdapter._extract_string_list(body, {"changed_imports", "import_changes"})
        affected_files = XynApiAdapter._extract_changed_files(body)
        test_recommendations = XynApiAdapter._extract_string_list(
            body,
            {"test_recommendations", "recommended_tests", "required_test_suites", "suggested_tests"},
        )
        oversized = body.get("oversized_file_delta")
        if not isinstance(oversized, dict):
            oversized = {}
        return {
            "changed_routes": changed_routes,
            "changed_imports": changed_imports,
            "affected_files": affected_files,
            "oversized_file_delta": oversized,
            "test_recommendations": test_recommendations,
        }

    @staticmethod
    def _extract_promotion_evidence_ids(payload: Any) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            token = str(value or "").strip()
            if not token:
                return
            if token in seen:
                return
            seen.add(token)
            out.append(token)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                is_evidence_like = any(
                    token in str(value.get(key) or "").strip().lower()
                    for key, token in [("type", "evidence"), ("kind", "evidence"), ("category", "evidence")]
                )
                if is_evidence_like and value.get("id"):
                    add(value.get("id"))
                for key, inner in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in {"promotion_evidence_id", "evidence_id"}:
                        add(inner)
                    walk(inner)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return out

    @staticmethod
    def _normalize_change_session_result(
        result: Dict[str, Any],
        *,
        application_id: str = "",
        session_id: str = "",
        default_next_allowed_actions: Optional[list[str]] = None,
        scope_hint: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = result.get("response")
        body = response if isinstance(response, dict) else {}
        status_code = int(result.get("status_code") or 0)
        ok = bool(result.get("ok"))

        status_candidates = [
            body.get("status"),
            body.get("state"),
            body.get("session_status"),
            body.get("control_status"),
        ]
        control = body.get("control") if isinstance(body.get("control"), dict) else {}
        status_candidates.extend([control.get("status"), control.get("state")])
        current_status = next((str(item) for item in status_candidates if str(item or "").strip()), "")
        if not current_status:
            current_status = "ok" if ok else ("forbidden" if status_code in {401, 403} else "error")
        control_reported_status = str(current_status or "").strip()

        blocked_reason = str(body.get("blocked_reason") or "").strip()
        if not blocked_reason and not ok:
            if status_code in {401, 403}:
                blocked_reason = "permission_denied"
            elif status_code == 404:
                blocked_reason = "not_found"
            elif status_code == 409:
                blocked_reason = "conflict"
            elif status_code >= 500:
                blocked_reason = "upstream_error"
            else:
                blocked_reason = "request_failed"
        classification = str(result.get("error_classification") or "").strip()
        if not classification:
            if blocked_reason == "artifact_not_found":
                classification = "artifact_not_found"
            elif blocked_reason in {"scope_resolution_failed", "unsupported_scope_mode"}:
                classification = blocked_reason
            elif blocked_reason == "application_not_found":
                classification = "application_not_found"
            elif status_code in {400, 409, 422}:
                classification = "backend_validation_error"
            elif status_code >= 500:
                classification = "backend_server_error"
            elif status_code in {401, 403}:
                classification = "auth_expired"

        next_allowed_actions = body.get("next_allowed_actions") if isinstance(body.get("next_allowed_actions"), list) else []
        if not next_allowed_actions:
            next_allowed_actions = list(default_next_allowed_actions or [])
        runtime_summary = XynApiAdapter._summarize_runtime_status_from_payload(
            body.get("raw") if isinstance(body.get("raw"), dict) else body
        )
        runtime_latest_status = str(runtime_summary.get("latest_status") or "").strip().lower()
        control_status_token = str(current_status or "").strip().lower()
        terminal_runtime_statuses = {"completed", "failed", "blocked", "canceled", "cancelled"}
        stale_control_statuses = {"queued", "running", "stage_apply_requested"}
        status_reconciled = False
        if runtime_latest_status in terminal_runtime_statuses and control_status_token in stale_control_statuses:
            current_status = runtime_latest_status
            status_reconciled = True

        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        hint = scope_hint if isinstance(scope_hint, dict) else {}
        hint_scope = hint.get("scope") if isinstance(hint.get("scope"), dict) else {}
        scope_type = str(
            body.get("scope_type")
            or scope.get("scope_type")
            or hint.get("scope_type")
            or hint_scope.get("scope_type")
            or "application"
        ).strip().lower() or "application"
        if scope_type not in {"application", "artifact"}:
            scope_type = "application"
        resolved_application_id = str(
            application_id
            or body.get("application_id")
            or scope.get("application_id")
            or hint.get("application_id")
            or hint_scope.get("application_id")
            or ""
        )
        resolved_session_id = str(session_id or body.get("session_id") or hint.get("session_id") or "")
        resolved_artifact_id = str(
            scope.get("artifact_id")
            or body.get("artifact_id")
            or hint.get("artifact_id")
            or hint_scope.get("artifact_id")
            or ""
        )
        resolved_artifact_slug = str(
            scope.get("artifact_slug")
            or body.get("artifact_slug")
            or hint.get("artifact_slug")
            or hint_scope.get("artifact_slug")
            or ""
        )
        resolved_workspace_id = str(
            scope.get("workspace_id")
            or body.get("workspace_id")
            or hint.get("workspace_id")
            or hint_scope.get("workspace_id")
            or ""
        )
        normalized = {
            "application_id": resolved_application_id,
            "session_id": resolved_session_id,
            "scope_type": scope_type,
            "scope": {
                "scope_type": scope_type,
                "application_id": resolved_application_id,
                "artifact_id": resolved_artifact_id,
                "artifact_slug": resolved_artifact_slug,
                "workspace_id": resolved_workspace_id,
            },
            "change_session_handle": {
                "application_id": resolved_application_id,
                "session_id": resolved_session_id,
                "workspace_id": resolved_workspace_id,
                "artifact_scope": XynApiAdapter._extract_string_list(
                    body.get("raw") if isinstance(body.get("raw"), dict) else body,
                    {"selected_artifact_ids", "artifact_ids"},
                ),
                "last_known_binding_id": str(
                    (
                        result.get("continuity")
                        if isinstance(result.get("continuity"), dict)
                        else {}
                    ).get("new_binding_id")
                    or XynApiAdapter._binding_id(str(result.get("base_url") or ""), str(result.get("path") or ""))
                ),
            },
            "current_status": current_status,
            "historical_status": {
                "control_reported_status": control_reported_status,
                "status_reconciled": status_reconciled,
            },
            "runtime_summary": runtime_summary,
            "next_allowed_actions": next_allowed_actions,
            "blocked_reason": blocked_reason,
            "preview_urls": XynApiAdapter._extract_preview_urls(body),
            "preview": XynApiAdapter._extract_preview_compact(body),
            "commit_shas": XynApiAdapter._extract_commit_shas(body),
            "changed_files": XynApiAdapter._extract_changed_files(body),
            "promotion_evidence_ids": XynApiAdapter._extract_promotion_evidence_ids(body),
            "decomposition_campaign": XynApiAdapter._extract_decomposition_campaign(body),
            "guardrails": XynApiAdapter._extract_decomposition_guardrails(body),
            "planner_prompt": XynApiAdapter._extract_planner_prompt_contract(
                body.get("raw") if isinstance(body.get("raw"), dict) else body
            ),
            "error_classification": classification,
            "action_delivery_state": str(result.get("action_delivery_state") or ""),
            "continuity": result.get("continuity") if isinstance(result.get("continuity"), dict) else {},
            "binding_state": result.get("binding_state") if isinstance(result.get("binding_state"), dict) else {},
            "raw": response,
        }
        result["response"] = normalized
        return result

    @staticmethod
    def _normalize_runtime_status_token(value: Any) -> str:
        token = str(value or "").strip().lower()
        if token in {"done", "succeeded", "success"}:
            return "completed"
        if token in {"error"}:
            return "failed"
        if token in {"cancelled"}:
            return "canceled"
        return token

    @staticmethod
    def _extract_runtime_runs_from_payload(payload: Any) -> list[Dict[str, Any]]:
        out: list[Dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in {"runtime_runs", "runs"} and isinstance(inner, list):
                        for row in inner:
                            if isinstance(row, dict):
                                out.append(row)
                    walk(inner)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return out

    @staticmethod
    def _summarize_runtime_status_from_payload(payload: Any) -> Dict[str, Any]:
        rows = XynApiAdapter._extract_runtime_runs_from_payload(payload)
        normalized_rows = [XynApiAdapter._normalize_runtime_run_row(row) for row in rows if isinstance(row, dict)]
        statuses = [
            XynApiAdapter._normalize_runtime_status_token(row.get("status"))
            for row in normalized_rows
            if str(row.get("status") or "").strip()
        ]
        latest = statuses[0] if statuses else ""
        terminal = [status for status in statuses if status in {"completed", "failed", "blocked", "canceled"}]
        return {
            "latest_status": latest,
            "latest_run_id": str((normalized_rows[0] if normalized_rows else {}).get("run_id") or ""),
            "run_count": len(normalized_rows),
            "terminal_statuses": terminal,
        }

    @staticmethod
    def _application_discovery_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(payload.get("id") or payload.get("application_id") or ""),
            "slug": str(payload.get("slug") or payload.get("application_slug") or payload.get("name") or ""),
            "name": str(payload.get("name") or payload.get("title") or payload.get("slug") or ""),
            "status": str(payload.get("status") or payload.get("state") or ""),
        }

    @staticmethod
    def _change_session_discovery_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(payload.get("id") or payload.get("session_id") or ""),
            "application_id": str(payload.get("application_id") or ""),
            "status": str(payload.get("status") or payload.get("state") or payload.get("session_status") or ""),
            "created_at": str(payload.get("created_at") or ""),
            "updated_at": str(payload.get("updated_at") or ""),
            "summary": str(payload.get("summary") or ""),
        }

    @staticmethod
    def _normalize_preview_failure_reason(raw_reason: str) -> str:
        token = str(raw_reason or "").strip().lower()
        if not token:
            return ""
        allowed = {
            "runtime_target_missing",
            "session_preview_provision_failed",
            "docker_unavailable",
            "docker_restart_failed",
            "preview_environment_unavailable",
        }
        if token in allowed:
            return token
        if "runtime" in token and "target" in token and "missing" in token:
            return "runtime_target_missing"
        if "provision" in token and "preview" in token:
            return "session_preview_provision_failed"
        if "docker" in token and "restart" in token:
            return "docker_restart_failed"
        if "docker" in token and ("unavailable" in token or "not available" in token or "not running" in token):
            return "docker_unavailable"
        if "preview" in token and ("unavailable" in token or "health" in token):
            return "preview_environment_unavailable"
        return "session_preview_provision_failed"

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        token = str(value or "").strip().lower()
        return token in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _extract_preview_compact(payload: Dict[str, Any]) -> Dict[str, Any]:
        preview_urls = XynApiAdapter._extract_preview_urls(payload)
        primary_url = preview_urls[0] if preview_urls else str(payload.get("primary_url") or "").strip()
        isolated_requested = XynApiAdapter._to_bool(
            payload.get("isolated_session_preview_requested")
            or payload.get("isolated_preview_requested")
            or payload.get("session_preview_isolated")
        )
        session_build = payload.get("session_build") if isinstance(payload.get("session_build"), dict) else {}
        session_build_status = str(
            session_build.get("status")
            or payload.get("session_build_status")
            or payload.get("build_status")
            or ""
        )
        raw_reason = str(
            session_build.get("reason")
            or payload.get("session_build_reason")
            or payload.get("failure_reason")
            or payload.get("preview_failure_reason")
            or (
                (payload.get("error") or {}).get("reason")
                if isinstance(payload.get("error"), dict)
                else ""
            )
            or ""
        )
        normalized_reason = XynApiAdapter._normalize_preview_failure_reason(raw_reason)
        compose_project = str(
            session_build.get("compose_project")
            or payload.get("compose_project")
            or payload.get("preview_compose_project")
            or ""
        )
        if not compose_project and isinstance(payload.get("compose_projects"), list):
            for item in payload.get("compose_projects") or []:
                token = str(item or "").strip()
                if token:
                    compose_project = token
                    break
        runtime_target_ids = XynApiAdapter._extract_values_by_keys(
            payload,
            {"runtime_target_id", "runtime_target_ids", "target_id", "target_ids"},
        )
        if isinstance(payload.get("runtime_target_ids"), list):
            for item in payload.get("runtime_target_ids") or []:
                token = str(item or "").strip()
                if token and token not in runtime_target_ids:
                    runtime_target_ids.append(token)
        runtime_target_ids = [token for token in runtime_target_ids if token]
        artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        artifact_readiness = {
            "ready_count": 0,
            "not_ready_count": 0,
            "unknown_count": 0,
            "items": [],
        }
        for row in artifacts:
            if not isinstance(row, dict):
                continue
            status_value = str(row.get("status") or row.get("readiness") or row.get("state") or "")
            ready_value = row.get("ready") if row.get("ready") is not None else row.get("is_ready")
            computed_ready = XynApiAdapter._to_bool(ready_value) or status_value.strip().lower() == "ready"
            item = {
                "artifact_id": str(row.get("id") or row.get("artifact_id") or ""),
                "slug": str(row.get("slug") or row.get("artifact_slug") or row.get("name") or ""),
                "status": status_value,
                "ready": computed_ready,
            }
            if item["ready"]:
                artifact_readiness["ready_count"] += 1
            elif item["status"]:
                artifact_readiness["not_ready_count"] += 1
            else:
                artifact_readiness["unknown_count"] += 1
            artifact_readiness["items"].append(item)

        fallback_used = XynApiAdapter._to_bool(
            payload.get("fallback_to_existing_runtime")
            or payload.get("used_existing_runtime")
            or payload.get("reused_existing_runtime")
            or session_build.get("fallback_to_existing_runtime")
            or session_build.get("used_existing_runtime")
        )
        fallback_reason = str(
            payload.get("fallback_reason")
            or payload.get("existing_runtime_fallback_reason")
            or session_build.get("fallback_reason")
            or ""
        )
        preview_status = str(
            payload.get("preview_status")
            or payload.get("status")
            or payload.get("state")
            or session_build_status
            or ""
        )
        return {
            "preview_status": preview_status,
            "primary_url": primary_url,
            "preview_urls": preview_urls,
            "isolated_session_preview_requested": isolated_requested,
            "session_build": {
                "status": session_build_status,
                "reason": normalized_reason or raw_reason,
            },
            "compose_project": compose_project,
            "runtime_target_ids": runtime_target_ids,
            "artifact_readiness": artifact_readiness,
            "used_existing_runtime_fallback": fallback_used,
            "existing_runtime_fallback_reason": fallback_reason,
        }

    @staticmethod
    def _extract_values_by_keys(payload: Any, keys: set[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            token = str(value or "").strip()
            if not token or token in seen:
                return
            seen.add(token)
            out.append(token)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                for key, inner in value.items():
                    normalized_key = str(key or "").strip().lower()
                    if normalized_key in keys and not isinstance(inner, (dict, list)):
                        add(inner)
                    walk(inner)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        return out

    @staticmethod
    def _extract_runtime_repo_target(payload: Dict[str, Any]) -> Dict[str, str]:
        repo_keys = {"repo_key", "repository", "repo", "repo_name"}
        repo_urls = {"repo_url", "repository_url", "git_url"}
        repo_paths = {"repo_path", "workspace_path", "root_path", "checkout_path", "repo_subpath", "subpath"}
        branch_keys = {"branch", "work_branch", "source_branch", "head_branch"}
        target_branch_keys = {"target_branch", "base_branch", "destination_branch"}
        return {
            "repo_key": (XynApiAdapter._extract_values_by_keys(payload, repo_keys) or [""])[0],
            "repo_url": (XynApiAdapter._extract_values_by_keys(payload, repo_urls) or [""])[0],
            "repo_path": (XynApiAdapter._extract_values_by_keys(payload, repo_paths) or [""])[0],
            "branch": (XynApiAdapter._extract_values_by_keys(payload, branch_keys) or [""])[0],
            "target_branch": (XynApiAdapter._extract_values_by_keys(payload, target_branch_keys) or [""])[0],
        }

    @staticmethod
    def _normalize_runtime_artifacts(payload: Any) -> list[Dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            if isinstance(payload.get("artifacts"), list):
                rows = payload.get("artifacts") or []
            elif isinstance(payload.get("items"), list):
                rows = payload.get("items") or []
        out: list[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "id": str(row.get("id") or row.get("artifact_id") or ""),
                    "name": str(row.get("name") or ""),
                    "kind": str(row.get("kind") or row.get("artifact_type") or ""),
                    "content_type": str(row.get("content_type") or ""),
                    "byte_length": row.get("byte_length"),
                    "uri": str(row.get("uri") or ""),
                }
            )
        return out

    @staticmethod
    def _normalize_runtime_logs(payload: Any) -> list[Dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            if isinstance(payload.get("logs"), list):
                rows = payload.get("logs") or []
            elif isinstance(payload.get("items"), list):
                rows = payload.get("items") or []
            elif isinstance(payload.get("steps"), list):
                rows = payload.get("steps") or []
        out: list[Dict[str, Any]] = []
        for index, row in enumerate(rows):
            if isinstance(row, dict):
                out.append(
                    {
                        "step_id": str(row.get("id") or row.get("step_id") or ""),
                        "name": str(row.get("name") or row.get("label") or f"step_{index}"),
                        "status": str(row.get("status") or ""),
                        "summary": str(row.get("summary") or ""),
                        "error": row.get("error"),
                        "outputs": row.get("outputs"),
                    }
                )
            else:
                out.append({"step_id": "", "name": f"log_{index}", "status": "", "summary": str(row)})
        return out

    @staticmethod
    def _normalize_runtime_commands(payload: Any) -> list[Dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        if isinstance(payload, dict):
            if isinstance(payload.get("commands"), list):
                rows = payload.get("commands") or []
            elif isinstance(payload.get("items"), list):
                rows = payload.get("items") or []
            elif isinstance(payload.get("steps"), list):
                rows = payload.get("steps") or []
        out: list[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "id": str(row.get("id") or row.get("command_id") or row.get("step_id") or ""),
                    "command": str(row.get("command") or row.get("cmd") or row.get("name") or ""),
                    "status": str(row.get("status") or ""),
                    "summary": str(row.get("summary") or ""),
                }
            )
        return out

    @staticmethod
    def _normalize_runtime_run_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        outputs = payload.get("outputs") if isinstance(payload.get("outputs"), dict) else {}
        artifact_candidates = payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []
        artifacts = XynApiAdapter._normalize_runtime_artifacts(artifact_candidates)
        produced_outputs = {
            "patch_artifacts": [a for a in artifacts if "patch" in str(a.get("kind") or "").lower() or "patch" in str(a.get("name") or "").lower()],
            "report_artifacts": [a for a in artifacts if "report" in str(a.get("kind") or "").lower() or "report" in str(a.get("name") or "").lower()],
            "log_artifacts": [a for a in artifacts if "log" in str(a.get("kind") or "").lower() or "log" in str(a.get("name") or "").lower()],
            "summary_artifacts": [a for a in artifacts if "summary" in str(a.get("kind") or "").lower() or "summary" in str(a.get("name") or "").lower()],
            "output_keys": sorted([str(key) for key in outputs.keys()]),
        }
        return {
            "run_id": str(payload.get("id") or payload.get("run_id") or ""),
            "status": str(payload.get("status") or payload.get("state") or ""),
            "worker_type": str(payload.get("worker_type") or ""),
            "worker_id": str(payload.get("worker_id") or ""),
            "repo_target": XynApiAdapter._extract_runtime_repo_target(payload),
            "failure_reason": str(payload.get("failure_reason") or ""),
            "summary": str(payload.get("summary") or ""),
            "error": payload.get("error"),
            "produced_outputs": produced_outputs,
            "raw": payload,
        }

    @staticmethod
    def _normalize_runtime_run_result(
        result: Dict[str, Any],
        *,
        default_next_allowed_actions: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        body = result.get("response")
        payload = body if isinstance(body, dict) else {}
        run_payload = payload.get("run") if isinstance(payload.get("run"), dict) else payload
        normalized_run = XynApiAdapter._normalize_runtime_run_row(run_payload) if isinstance(run_payload, dict) else {}
        status_code = int(result.get("status_code") or 0)
        blocked_reason = str(payload.get("blocked_reason") or "").strip()
        if not result.get("ok"):
            if blocked_reason:
                pass
            elif status_code in {401, 403}:
                blocked_reason = "permission_denied"
            elif status_code == 404:
                blocked_reason = "not_found"
            elif status_code == 409:
                blocked_reason = "conflict"
            elif status_code >= 500:
                blocked_reason = "upstream_error"
            else:
                blocked_reason = "request_failed"
        result["response"] = {
            **normalized_run,
            "current_status": str(normalized_run.get("status") or ("ok" if result.get("ok") else "error")),
            "next_allowed_actions": list(default_next_allowed_actions or []),
            "blocked_reason": blocked_reason,
            "raw": body,
        }
        return result

    @staticmethod
    def _extract_effort_changed_files(payload: Any) -> list[str]:
        return XynApiAdapter._extract_changed_files(payload)

    @staticmethod
    def _normalize_change_effort_source_roots(change_effort: Dict[str, Any], source: Dict[str, Any], metadata: Dict[str, Any]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            token = str(value or "").strip().strip("/")
            if not token or token in seen:
                return
            seen.add(token)
            out.append(token)

        add(source.get("monorepo_subpath"))
        add(change_effort.get("repo_subpath"))
        source_roots = metadata.get("source_roots")
        if isinstance(source_roots, list):
            for item in source_roots:
                add(item)
        return out

    @staticmethod
    def _normalize_change_effort_result(
        result: Dict[str, Any],
        *,
        default_next_allowed_actions: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        body = result.get("response")
        payload = body if isinstance(body, dict) else {}
        effort = payload.get("change_effort") if isinstance(payload.get("change_effort"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        promotion = payload.get("promotion") if isinstance(payload.get("promotion"), dict) else {}
        metadata = effort.get("metadata_json") if isinstance(effort.get("metadata_json"), dict) else {}

        effort_id = str(effort.get("id") or payload.get("effort_id") or "")
        artifact_slug = str(effort.get("artifact_slug") or "")
        repo_key = str(source.get("repo_key") or effort.get("repo_key") or "")
        repo_url = str(source.get("repo_url") or effort.get("repo_url") or "")
        repo_subpath = str(source.get("monorepo_subpath") or effort.get("repo_subpath") or "").strip()
        commit_sha = str(source.get("commit_sha") or metadata.get("commit_sha") or "")
        source_roots = XynApiAdapter._normalize_change_effort_source_roots(effort, source, metadata)
        work_branch = str(effort.get("work_branch") or metadata.get("work_branch") or "")
        base_branch = str(effort.get("base_branch") or "")
        target_branch = str(effort.get("target_branch") or promotion.get("to_branch") or "")
        worktree_path = str(effort.get("worktree_path") or metadata.get("worktree_path") or "")
        worktree_token = str(Path(worktree_path).name if worktree_path else "")
        allowed_paths = [f"{repo_subpath}/**"] if repo_subpath else []
        linked_application_id = str(metadata.get("application_id") or metadata.get("linked_application_id") or "")
        linked_session_id = str(metadata.get("session_id") or metadata.get("linked_session_id") or "")
        changed_files = XynApiAdapter._extract_effort_changed_files(payload)
        if not changed_files:
            changed_files = XynApiAdapter._extract_effort_changed_files(metadata)

        status_code = int(result.get("status_code") or 0)
        blocked_reason = ""
        if not result.get("ok"):
            detail_text = str(payload.get("detail") or payload.get("error") or "").strip().lower()
            if status_code in {401, 403}:
                blocked_reason = "permission_denied"
            elif status_code == 404:
                blocked_reason = "not_found"
            elif status_code == 409 and "provenance" in detail_text:
                blocked_reason = "missing_provenance"
            elif status_code == 409 and "ambig" in detail_text:
                blocked_reason = "ambiguous_source"
            elif status_code == 409 and "artifact not found" in detail_text:
                blocked_reason = "artifact_not_found"
            elif status_code == 409:
                blocked_reason = "conflict"
            elif status_code >= 500:
                blocked_reason = "upstream_error"
            else:
                blocked_reason = "request_failed"

        result["response"] = {
            "effort_id": effort_id,
            "artifact_slug": artifact_slug,
            "repo_key": repo_key,
            "repo_url": repo_url,
            "repo_subpath": repo_subpath,
            "commit_sha": commit_sha,
            "source_roots": source_roots,
            "branch_name": work_branch,
            "base_branch": base_branch,
            "promotion_target_branch": target_branch,
            "worktree_path": worktree_path,
            "worktree_token": worktree_token,
            "allowed_paths": allowed_paths,
            "artifact_ownership_boundaries": {
                "artifact_slug": artifact_slug,
                "repo_key": repo_key,
                "allowed_paths": allowed_paths,
            },
            "linked_change_session": {
                "application_id": linked_application_id,
                "session_id": linked_session_id,
                "connected": bool(linked_application_id and linked_session_id),
            },
            "changed_files": changed_files,
            "promotion": promotion,
            "current_status": str(effort.get("status") or promotion.get("status") or ""),
            "next_allowed_actions": list(default_next_allowed_actions or []),
            "blocked_reason": blocked_reason,
            "raw": body,
        }
        return result

    @staticmethod
    def _normalize_dev_task_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        run_ids = XynApiAdapter._extract_values_by_keys(payload, {"run_id", "runtime_run_id"})
        return {
            "id": str(payload.get("id") or payload.get("task_id") or payload.get("dev_task_id") or ""),
            "application_id": str(payload.get("application_id") or ""),
            "session_id": str(payload.get("session_id") or payload.get("change_session_id") or ""),
            "status": str(payload.get("status") or payload.get("state") or ""),
            "summary": str(payload.get("summary") or payload.get("title") or ""),
            "runtime_run_ids": run_ids,
            "raw": payload,
        }

    @staticmethod
    def _workspace_role_from_row(row: dict[str, Any]) -> str:
        role = str(row.get("workspace_role") or "").strip().lower()
        if role:
            return role
        metadata = row.get("metadata")
        if isinstance(metadata, dict):
            value = str(metadata.get("xyn_workspace_role") or "").strip().lower()
            if value:
                return value
            if metadata.get("xyn_system_workspace") is True:
                return _WORKSPACE_ROLE_SYSTEM_PLATFORM
        slug = str(row.get("slug") or "").strip().lower()
        if slug in {"platform-builder", "civic-lab"}:
            return _WORKSPACE_ROLE_SYSTEM_PLATFORM
        return ""

    @staticmethod
    def _workspace_resolution_by_intent(
        *,
        intent: str,
        candidate_workspaces: list[dict[str, Any]],
    ) -> str:
        normalized_intent = str(intent or "").strip().lower() or "user"
        preferred_role = _WORKSPACE_ROLE_SYSTEM_PLATFORM if normalized_intent == "system" else _WORKSPACE_ROLE_DEFAULT_USER
        role_match = next(
            (
                str(row.get("id") or "").strip()
                for row in candidate_workspaces
                if isinstance(row, dict)
                and str(row.get("id") or "").strip()
                and XynApiAdapter._workspace_role_from_row(row) == preferred_role
            ),
            "",
        )
        if role_match:
            return role_match
        user_visible = next(
            (
                str(row.get("id") or "").strip()
                for row in candidate_workspaces
                if isinstance(row, dict)
                and str(row.get("id") or "").strip()
                and XynApiAdapter._workspace_role_from_row(row)
                in {_WORKSPACE_ROLE_DEFAULT_USER, _WORKSPACE_ROLE_USER_VISIBLE}
            ),
            "",
        )
        if user_visible:
            return user_visible
        return ""

    def _list_accessible_workspaces(self) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=["/xyn/api/workspaces"],
            base_urls=[self._config.control_api_base_url],
        )
        workspace_rows: list[dict[str, Any]] = []
        if not result.get("ok"):
            return {"ok": False, "result": result, "workspaces": workspace_rows}
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("workspaces") if isinstance(body.get("workspaces"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            workspace_id = str(row.get("id") or "").strip()
            if not workspace_id:
                continue
            workspace_rows.append(
                {
                    "id": workspace_id,
                    "slug": str(row.get("slug") or "").strip(),
                    "title": str(row.get("title") or row.get("name") or "").strip(),
                    "workspace_role": str(row.get("workspace_role") or "").strip().lower(),
                    "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
                }
            )
        return {"ok": True, "result": result, "workspaces": workspace_rows}

    @staticmethod
    def _workspace_resolution_error_payload(
        *,
        error: str,
        detail: str,
        candidate_workspaces: list[dict[str, Any]],
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "error": str(error or "workspace_required"),
            "detail": str(detail or "").strip(),
        }
        if candidate_workspaces:
            out["candidate_workspaces"] = candidate_workspaces[:50]
        return out

    def _workspace_resolution_error_result(
        self,
        *,
        method: str,
        path: str,
        error: str,
        detail: str,
        candidate_workspaces: list[dict[str, Any]],
        status_code: int,
    ) -> Dict[str, Any]:
        return {
            "ok": False,
            "status_code": int(status_code),
            "method": str(method).upper(),
            "path": str(path),
            "base_url": str(self._config.control_api_base_url).rstrip("/"),
            "response": self._workspace_resolution_error_payload(
                error=error,
                detail=detail,
                candidate_workspaces=candidate_workspaces,
            ),
        }

    def _resolve_workspace_for_request(
        self,
        *,
        explicit_workspace_id: str = "",
        require_workspace: bool = True,
        intent: str = "user",
    ) -> Dict[str, Any]:
        explicit = str(explicit_workspace_id or "").strip()
        if explicit:
            return {"ok": True, "workspace_id": explicit, "source": "explicit", "candidate_workspaces": []}

        accessible = self._list_accessible_workspaces()
        candidate_workspaces = accessible.get("workspaces") if isinstance(accessible.get("workspaces"), list) else []
        if not accessible.get("ok"):
            if require_workspace:
                return {
                    "ok": False,
                    "status_code": 400,
                    "error": "workspace_required",
                    "detail": "workspace_id is required and no default workspace could be resolved.",
                    "candidate_workspaces": candidate_workspaces,
                }
            return {"ok": True, "workspace_id": "", "source": "none", "candidate_workspaces": candidate_workspaces}

        configured_default = str(self._resolved_default_workspace_id or self._config.default_workspace_id or "").strip()
        if configured_default:
            accessible_ids = {
                str(row.get("id") or "").strip()
                for row in candidate_workspaces
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            }
            if accessible.get("ok") and accessible_ids and configured_default not in accessible_ids:
                recovered = self._workspace_resolution_by_intent(intent=intent, candidate_workspaces=candidate_workspaces)
                if recovered:
                    self._resolved_default_workspace_id = recovered
                    return {
                        "ok": True,
                        "workspace_id": recovered,
                        "source": "recovered_from_workspace_forbidden",
                        "candidate_workspaces": candidate_workspaces,
                    }
                return {
                    "ok": False,
                    "status_code": 403,
                    "error": "workspace_forbidden",
                    "detail": "Configured default workspace is not accessible to the authenticated principal.",
                    "candidate_workspaces": candidate_workspaces,
                }
            return {
                "ok": True,
                "workspace_id": configured_default,
                "source": "configured_default",
                "candidate_workspaces": candidate_workspaces,
            }

        if not require_workspace:
            return {"ok": True, "workspace_id": "", "source": "none", "candidate_workspaces": []}

        if len(candidate_workspaces) == 1:
            return {
                "ok": True,
                "workspace_id": str(candidate_workspaces[0].get("id") or "").strip(),
                "source": "single_accessible_workspace",
                "candidate_workspaces": candidate_workspaces,
            }

        intent_selected = self._workspace_resolution_by_intent(intent=intent, candidate_workspaces=candidate_workspaces)
        if intent_selected:
            return {
                "ok": True,
                "workspace_id": intent_selected,
                "source": f"intent_{str(intent or 'user').strip().lower() or 'user'}",
                "candidate_workspaces": candidate_workspaces,
            }

        return {
            "ok": False,
            "status_code": 400,
            "error": "workspace_required",
            "detail": "workspace_id is required because multiple accessible workspaces were found.",
            "candidate_workspaces": candidate_workspaces,
        }

    def list_applications(self, *, workspace_id: str = "") -> Dict[str, Any]:
        resolved = self._resolve_workspace_for_request(
            explicit_workspace_id=workspace_id,
            require_workspace=True,
            intent="user",
        )
        if not resolved.get("ok"):
            return self._workspace_resolution_error_result(
                method="GET",
                path="/xyn/api/applications",
                error=str(resolved.get("error") or "workspace_required"),
                detail=str(resolved.get("detail") or ""),
                candidate_workspaces=resolved.get("candidate_workspaces") if isinstance(resolved.get("candidate_workspaces"), list) else [],
                status_code=int(resolved.get("status_code") or 400),
            )
        resolved_workspace_id = str(resolved.get("workspace_id") or "").strip()
        params = {"workspace_id": resolved_workspace_id}
        paths = ["/xyn/api/applications", "/api/v1/applications"]
        result = self._request_with_fallback_paths(
            method="GET",
            paths=paths,
            base_urls=self._planner_base_urls(),
            params=params,
        )
        if not result.get("ok"):
            return self._normalize_planner_route_error(result, attempted_paths=paths)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("applications") if isinstance(body.get("applications"), list) else (body.get("items") if isinstance(body.get("items"), list) else [])
        normalized = [self._application_discovery_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {
            "applications": normalized,
            "count": len(normalized),
            "resolved_workspace_id": resolved_workspace_id,
        }
        return result

    def _workspace_intent_for_artifact_scope(
        self,
        *,
        artifact_id: str = "",
        artifact_slug: str = "",
    ) -> str:
        normalized_slug = str(artifact_slug or "").strip().lower()
        if normalized_slug in _SYSTEM_ARTIFACT_SLUGS:
            return "system"
        normalized_artifact_id = str(artifact_id or "").strip()
        if normalized_artifact_id and not normalized_slug:
            row = self._resolve_artifact_record(artifact_id=normalized_artifact_id)
            if isinstance(row, dict):
                resolved_slug = str(row.get("slug") or "").strip().lower()
                if resolved_slug in _SYSTEM_ARTIFACT_SLUGS:
                    return "system"
        return "user"

    def get_application(self, *, application_id: str) -> Dict[str, Any]:
        paths = [
            f"/xyn/api/applications/{application_id}",
            f"/api/v1/applications/{application_id}",
        ]
        result = self._request_with_fallback_paths(
            method="GET",
            paths=paths,
            base_urls=self._planner_base_urls(),
        )
        if not result.get("ok"):
            return self._normalize_planner_route_error(result, attempted_paths=paths)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {"application": self._application_discovery_row(body)}
        return result

    def list_application_change_sessions(self, *, application_id: str) -> Dict[str, Any]:
        paths = [
            f"/xyn/api/applications/{application_id}/change-sessions",
            f"/api/v1/applications/{application_id}/change-sessions",
        ]
        result = self._request_with_fallback_paths(
            method="GET",
            paths=paths,
            base_urls=self._planner_base_urls(),
        )
        if not result.get("ok"):
            return self._normalize_planner_route_error(result, attempted_paths=paths)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("change_sessions") if isinstance(body.get("change_sessions"), list) else (body.get("items") if isinstance(body.get("items"), list) else [])
        normalized = [self._change_session_discovery_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {
            "application_id": str(application_id),
            "change_sessions": normalized,
            "count": len(normalized),
        }
        return result

    def get_application_change_session(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        paths = self._change_session_control_paths(handle)
        result = self._request_with_fallback_paths(
            method="GET",
            paths=paths,
            base_urls=self._planner_base_urls(),
        )
        if not result.get("ok"):
            return self._normalize_planner_route_error(result, attempted_paths=paths)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {
            "change_session": self._change_session_discovery_row(body),
            "application_id": str(application_id or body.get("application_id") or ""),
        }
        return result

    def create_application_change_session(
        self,
        *,
        application_id: str,
        artifact_source: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        paths = [
            f"/xyn/api/applications/{application_id}/change-sessions",
            f"/api/v1/applications/{application_id}/change-sessions",
        ]
        request_payload = dict(payload or {})
        if isinstance(artifact_source, dict):
            cleaned_source = {str(key): value for key, value in artifact_source.items() if str(key).strip()}
            if cleaned_source:
                request_payload["artifact_source"] = cleaned_source
        result = self._request_with_fallback_paths(
            method="POST",
            paths=paths,
            json_payload=request_payload,
            base_urls=self._planner_base_urls(),
            allow_reissue_on_transport_error=False,
        )
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            default_next_allowed_actions=[
                "get_application_change_session",
                "get_application_change_session_plan",
                "stage_apply_application_change_session",
            ],
        )

    def create_change_session_with_scope(
        self,
        *,
        application_id: str = "",
        artifact_id: str = "",
        artifact_slug: str = "",
        workspace_id: str = "",
        artifact_source: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_artifact_id = str(artifact_id or "").strip()
        normalized_artifact_slug = str(artifact_slug or "").strip()
        normalized_workspace_id = str(workspace_id or "").strip()
        request_payload = dict(payload or {})

        if normalized_application_id and not (normalized_artifact_id or normalized_artifact_slug):
            # Backward-compatibility guard: some callers still pass artifact_id in application_id.
            # For decomposition/create-session flows, detect that shape and route to artifact scope.
            if "decomposition_campaign" in request_payload:
                app_lookup = self.get_application(application_id=normalized_application_id)
                if not app_lookup.get("ok"):
                    status = int(app_lookup.get("status_code") or 0)
                    if status in {404, 400}:
                        artifact_row = self._resolve_artifact_record(artifact_id=normalized_application_id)
                        if isinstance(artifact_row, dict) and (
                            str(artifact_row.get("id") or "").strip() == normalized_application_id
                        ):
                            normalized_artifact_id = normalized_application_id
                            normalized_artifact_slug = str(artifact_row.get("slug") or normalized_artifact_slug).strip()
                            normalized_application_id = ""

        if normalized_application_id and not (normalized_artifact_id or normalized_artifact_slug):
            result = self.create_application_change_session(
                application_id=normalized_application_id,
                artifact_source=artifact_source,
                payload=payload,
            )
            response = result.get("response") if isinstance(result.get("response"), dict) else {}
            response["scope_type"] = str(response.get("scope_type") or "application")
            response["scope"] = {
                "scope_type": "application",
                "application_id": str(response.get("application_id") or normalized_application_id),
                "artifact_id": normalized_artifact_id,
                "artifact_slug": normalized_artifact_slug,
                "workspace_id": str(response.get("change_session_handle", {}).get("workspace_id") or normalized_workspace_id),
            }
            result["response"] = response
            return result

        # Artifact-scoped/session-scoped create operations require workspace context.
        # Resolve it deterministically for fresh installs when the caller omitted it.
        if not normalized_application_id and not normalized_workspace_id:
            workspace_intent = self._workspace_intent_for_artifact_scope(
                artifact_id=normalized_artifact_id,
                artifact_slug=normalized_artifact_slug,
            )
            resolved_workspace = self._resolve_workspace_for_request(
                explicit_workspace_id="",
                require_workspace=True,
                intent=workspace_intent,
            )
            if not resolved_workspace.get("ok"):
                candidate_workspaces = (
                    resolved_workspace.get("candidate_workspaces")
                    if isinstance(resolved_workspace.get("candidate_workspaces"), list)
                    else []
                )
                resolution_error = str(resolved_workspace.get("error") or "workspace_required").strip() or "workspace_required"
                blocked_reason = (
                    "workspace_forbidden"
                    if resolution_error == "workspace_forbidden"
                    else "scope_resolution_failed"
                )
                detail = str(resolved_workspace.get("detail") or "").strip() or (
                    "workspace_id is required when application_id is omitted."
                )
                return {
                    "ok": False,
                    "status_code": int(resolved_workspace.get("status_code") or 400),
                    "method": "POST",
                    "path": "/xyn/api/change-sessions",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "error": resolution_error,
                        "detail": detail,
                        "blocked_reason": blocked_reason,
                        "next_allowed_actions": [
                            "list_workspaces",
                            "list_artifacts",
                            "create_decomposition_campaign",
                        ],
                        "candidate_workspaces": candidate_workspaces,
                    },
                    "error_classification": (
                        "workspace_forbidden"
                        if resolution_error == "workspace_forbidden"
                        else "scope_resolution_failed"
                    ),
                }
            normalized_workspace_id = str(resolved_workspace.get("workspace_id") or "").strip()

        if normalized_application_id:
            request_payload["application_id"] = normalized_application_id
        if normalized_workspace_id:
            request_payload["workspace_id"] = normalized_workspace_id
        if normalized_artifact_id:
            request_payload["artifact_id"] = normalized_artifact_id
        if normalized_artifact_slug:
            request_payload["artifact_slug"] = normalized_artifact_slug
        if isinstance(artifact_source, dict):
            cleaned_source = {str(key): value for key, value in artifact_source.items() if str(key).strip()}
            if cleaned_source:
                request_payload["artifact_source"] = cleaned_source
        paths = ["/xyn/api/change-sessions"]
        result = self._request_with_fallback_paths(
            method="POST",
            paths=paths,
            json_payload=request_payload,
            base_urls=self._planner_base_urls(),
            allow_reissue_on_transport_error=False,
        )
        normalized = self._normalize_change_session_result(
            result,
            default_next_allowed_actions=[
                "get_application_change_session",
                "get_application_change_session_plan",
                "stage_apply_application_change_session",
            ],
        )
        body = normalized.get("response") if isinstance(normalized.get("response"), dict) else {}
        requested_artifact_scope = bool(normalized_artifact_id or normalized_artifact_slug)
        fallback_scope_type = "artifact" if requested_artifact_scope else "application"
        scope_type = str(body.get("scope_type") or fallback_scope_type).strip().lower() or fallback_scope_type
        if requested_artifact_scope and scope_type == "application" and not str(body.get("application_id") or "").strip():
            scope_type = "artifact"
        if scope_type not in {"application", "artifact"}:
            scope_type = fallback_scope_type
        if not normalized.get("ok"):
            blocked = str(body.get("blocked_reason") or "").strip().lower()
            if blocked == "artifact_not_found":
                normalized["error_classification"] = "artifact_not_found"
            elif blocked == "application_not_found":
                normalized["error_classification"] = "application_not_found"
            elif blocked == "scope_resolution_failed":
                normalized["error_classification"] = "scope_resolution_failed"
            elif blocked in {"contract_mismatch", "unsupported_scope_mode"}:
                normalized["error_classification"] = blocked
            elif blocked == "workspace_forbidden":
                normalized["error_classification"] = "workspace_forbidden"
            elif int(normalized.get("status_code") or 0) >= 500:
                normalized["error_classification"] = "backend_server_error"
            elif int(normalized.get("status_code") or 0) in {400, 409, 422}:
                normalized["error_classification"] = "backend_validation_error"
        body["scope_type"] = scope_type
        body["scope"] = {
            "scope_type": scope_type,
            "application_id": str(body.get("application_id") or normalized_application_id),
            "artifact_id": str((body.get("scope") or {}).get("artifact_id") or normalized_artifact_id),
            "artifact_slug": str((body.get("scope") or {}).get("artifact_slug") or normalized_artifact_slug),
            "workspace_id": str((body.get("scope") or {}).get("workspace_id") or normalized_workspace_id),
        }
        normalized["response"] = body
        return normalized

    def create_decomposition_campaign(
        self,
        *,
        application_id: str = "",
        artifact_id: str = "",
        artifact_slug: str = "",
        workspace_id: str = "",
        artifact_source: Optional[Dict[str, Any]] = None,
        target_source_files: Optional[list[str]] = None,
        extraction_seams: Optional[list[str]] = None,
        moved_handlers_modules: Optional[list[str]] = None,
        required_test_suites: Optional[list[str]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request_payload = dict(payload or {})
        request_payload["decomposition_campaign"] = {
            "kind": "xyn_api_decomposition",
            "target_source_files": [str(item).strip() for item in (target_source_files or []) if str(item).strip()],
            "extraction_seams": [str(item).strip() for item in (extraction_seams or []) if str(item).strip()],
            "moved_handlers_modules": [str(item).strip() for item in (moved_handlers_modules or []) if str(item).strip()],
            "required_test_suites": [str(item).strip() for item in (required_test_suites or []) if str(item).strip()],
            "promotion_readiness": str((request_payload.get("promotion_readiness") or "planning")).strip() or "planning",
        }
        result = self.create_change_session_with_scope(
            application_id=application_id,
            artifact_id=artifact_id,
            artifact_slug=artifact_slug,
            workspace_id=workspace_id,
            artifact_source=artifact_source,
            payload=request_payload,
        )
        if isinstance(result.get("response"), dict):
            result["response"]["decomposition_campaign"] = dict(request_payload["decomposition_campaign"])
        return result

    def get_decomposition_campaign(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        result = self.inspect_change_session_control(handle=handle)
        normalized = self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            scope_hint={
                "scope_type": "artifact",
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
            },
            default_next_allowed_actions=[
                "stage_apply_application_change_session",
                "list_runtime_runs",
                "prepare_preview_application_change_session",
                "validate_application_change_session",
                "commit_application_change_session",
                "promote_application_change_session",
            ],
        )
        body = normalized.get("response") if isinstance(normalized.get("response"), dict) else {}
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        resolved_application_id = str(body.get("application_id") or scope.get("application_id") or application_id or "")
        normalized["response"] = {
            "application_id": resolved_application_id,
            "session_id": str(body.get("session_id") or session_id),
            "scope_type": str(body.get("scope_type") or ""),
            "scope": scope,
            "current_status": str(body.get("current_status") or ""),
            "next_allowed_actions": body.get("next_allowed_actions") if isinstance(body.get("next_allowed_actions"), list) else [],
            "blocked_reason": str(body.get("blocked_reason") or ""),
            "decomposition_campaign": body.get("decomposition_campaign") if isinstance(body.get("decomposition_campaign"), dict) else {},
            "guardrails": body.get("guardrails") if isinstance(body.get("guardrails"), dict) else {},
            "preview": body.get("preview") if isinstance(body.get("preview"), dict) else {},
            "commit_shas": body.get("commit_shas") if isinstance(body.get("commit_shas"), list) else [],
            "changed_files": body.get("changed_files") if isinstance(body.get("changed_files"), list) else [],
            "promotion_evidence_ids": body.get("promotion_evidence_ids") if isinstance(body.get("promotion_evidence_ids"), list) else [],
            "raw": body.get("raw"),
        }
        return normalized

    def inspect_decomposition_guardrails(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        result = self.inspect_change_session_control(handle=handle)
        normalized = self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            scope_hint={
                "scope_type": "artifact",
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
            },
            default_next_allowed_actions=[
                "stage_apply_application_change_session",
                "prepare_preview_application_change_session",
                "validate_application_change_session",
                "commit_application_change_session",
            ],
        )
        body = normalized.get("response") if isinstance(normalized.get("response"), dict) else {}
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        guardrails = body.get("guardrails") if isinstance(body.get("guardrails"), dict) else {}
        normalized["response"] = {
            "application_id": str(body.get("application_id") or scope.get("application_id") or application_id or ""),
            "session_id": str(body.get("session_id") or session_id),
            "scope_type": str(body.get("scope_type") or ""),
            "scope": scope,
            "current_status": str(body.get("current_status") or ""),
            "blocked_reason": str(body.get("blocked_reason") or ""),
            "guardrails": guardrails,
            "preview": body.get("preview") if isinstance(body.get("preview"), dict) else {},
            "decomposition_campaign": body.get("decomposition_campaign") if isinstance(body.get("decomposition_campaign"), dict) else {},
            "raw": body.get("raw"),
        }
        return normalized

    def get_decomposition_observability(
        self,
        *,
        application_id: str = "",
        session_id: str = "",
        artifact_id: str = "",
        artifact_slug: str = "",
        top_n: int = 50,
    ) -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        session_status = self.get_decomposition_campaign(application_id=handle.application_id, session_id=handle.session_id)
        body = session_status.get("response") if isinstance(session_status.get("response"), dict) else {}
        raw = body.get("raw") if isinstance(body.get("raw"), dict) else {}
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}

        resolved_artifact_id = str(artifact_id or "").strip()
        resolved_artifact_slug = str(artifact_slug or "").strip()
        if not resolved_artifact_id:
            resolved_artifact_id = str(scope.get("artifact_id") or body.get("artifact_id") or "").strip()
        if not resolved_artifact_slug:
            resolved_artifact_slug = str(scope.get("artifact_slug") or body.get("artifact_slug") or "").strip()
        if not resolved_artifact_id:
            selected_ids = self._extract_string_list(raw, {"selected_artifact_ids", "artifact_ids"})
            if selected_ids:
                resolved_artifact_id = selected_ids[0]
        if not resolved_artifact_slug:
            selected_slugs = self._extract_string_list(raw, {"artifact_slugs", "selected_artifact_slugs"})
            if selected_slugs:
                resolved_artifact_slug = selected_slugs[0]

        metrics_result = self.get_artifact_module_metrics(
            artifact_id=resolved_artifact_id,
            artifact_slug=resolved_artifact_slug,
            top_n=max(1, int(top_n)),
        )
        analysis_result = self.analyze_python_api_artifact(
            artifact_id=resolved_artifact_id,
            artifact_slug=resolved_artifact_slug,
        )

        metrics_body = metrics_result.get("response") if isinstance(metrics_result.get("response"), dict) else {}
        analysis_body = analysis_result.get("response") if isinstance(analysis_result.get("response"), dict) else {}
        route_inventory = analysis_body.get("route_inventory")
        if not isinstance(route_inventory, dict):
            route_inventory = {}
        route_inventory_delta = analysis_body.get("route_inventory_delta")
        if not isinstance(route_inventory_delta, dict):
            route_inventory_delta = {}

        return {
            "ok": bool(session_status.get("ok")) and bool(metrics_result.get("ok")) and bool(analysis_result.get("ok")),
            "status_code": 200,
            "method": "GET",
            "path": f"/xyn/api/change-sessions/{session_id}/decomposition-observability",
            "base_url": str(self._config.control_api_base_url).rstrip("/"),
            "response": {
                "application_id": str(body.get("application_id") or scope.get("application_id") or application_id or ""),
                "session_id": str(session_id),
                "scope_type": str(body.get("scope_type") or ""),
                "scope": scope,
                "artifact_id": resolved_artifact_id,
                "artifact_slug": resolved_artifact_slug,
                "current_status": str(body.get("current_status") or ""),
                "guardrails": body.get("guardrails") if isinstance(body.get("guardrails"), dict) else {},
                "module_metrics": metrics_body.get("metrics") if isinstance(metrics_body.get("metrics"), list) else [],
                "module_metrics_count": int(metrics_body.get("count") or 0),
                "route_inventory": route_inventory,
                "route_inventory_delta": route_inventory_delta,
                "analysis_summary": analysis_body.get("summary") if isinstance(analysis_body.get("summary"), dict) else {},
                "source_mode": str(analysis_body.get("source_mode") or metrics_body.get("source_mode") or ""),
                "warnings": list(analysis_body.get("warnings") or metrics_body.get("warnings") or []),
                "raw": {
                    "session": body.get("raw"),
                    "metrics": metrics_body,
                    "analysis": analysis_body,
                },
            },
        }

    def get_application_change_session_plan(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        attempted_paths = [
            *self._change_session_control_paths(handle, suffix="/plan"),
            *self._change_session_control_paths(handle, suffix="/control"),
        ]
        # Canonical workflow route is POST-only in xyn-api.
        result = self._request_with_fallback_paths(
            method="POST",
            paths=self._change_session_control_paths(handle, suffix="/plan"),
            json_payload={},
            base_urls=self._planner_base_urls(),
        )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            # Back-compat for older deployments that exposed GET /plan.
            result = self._request_with_fallback_paths(
                method="GET",
                paths=self._change_session_control_paths(handle, suffix="/plan"),
                base_urls=self._planner_base_urls(),
            )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            # Compatibility fallback to canonical control inspection when explicit /plan route is unavailable.
            result = self.inspect_change_session_control(handle=handle)
        if not result.get("ok"):
            result = self._normalize_planner_route_error(result, attempted_paths=attempted_paths)
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            scope_hint={
                "scope_type": "artifact",
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
            },
            default_next_allowed_actions=[
                "stage_apply_application_change_session",
                "prepare_preview_application_change_session",
                "validate_application_change_session",
            ],
        )

    def _run_application_change_session_operation(
        self,
        *,
        application_id: str = "",
        session_id: str = "",
        operation: str = "",
        payload: Optional[Dict[str, Any]] = None,
        next_allowed_actions: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        result = self.run_change_session_control_action(
            handle=handle,
            operation=operation,
            action_payload=payload,
        )
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            scope_hint={
                "scope_type": "artifact",
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
            },
            default_next_allowed_actions=next_allowed_actions,
        )

    def stage_apply_application_change_session(
        self, *, application_id: str = "", session_id: str = "", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._run_application_change_session_operation(
            application_id=application_id,
            session_id=session_id,
            operation="stage_apply",
            payload=payload,
            next_allowed_actions=[
                "prepare_preview_application_change_session",
                "validate_application_change_session",
                "commit_application_change_session",
            ],
        )

    def prepare_preview_application_change_session(
        self, *, application_id: str = "", session_id: str = "", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._run_application_change_session_operation(
            application_id=application_id,
            session_id=session_id,
            operation="prepare_preview",
            payload=payload,
            next_allowed_actions=[
                "validate_application_change_session",
                "commit_application_change_session",
            ],
        )

    def validate_application_change_session(
        self, *, application_id: str = "", session_id: str = "", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._run_application_change_session_operation(
            application_id=application_id,
            session_id=session_id,
            operation="validate",
            payload=payload,
            next_allowed_actions=[
                "commit_application_change_session",
                "prepare_preview_application_change_session",
            ],
        )

    def commit_application_change_session(
        self, *, application_id: str = "", session_id: str = "", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._run_application_change_session_operation(
            application_id=application_id,
            session_id=session_id,
            operation="commit",
            payload=payload,
            next_allowed_actions=[
                "get_application_change_session_commits",
                "promote_application_change_session",
            ],
        )

    def promote_application_change_session(
        self, *, application_id: str = "", session_id: str = "", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._run_application_change_session_operation(
            application_id=application_id,
            session_id=session_id,
            operation="promote",
            payload=payload,
            next_allowed_actions=[
                "get_application_change_session_promotion_evidence",
                "rollback_application_change_session",
            ],
        )

    def rollback_application_change_session(
        self, *, application_id: str = "", session_id: str = "", payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._run_application_change_session_operation(
            application_id=application_id,
            session_id=session_id,
            operation="rollback",
            payload=payload,
            next_allowed_actions=[
                "get_application_change_session_promotion_evidence",
                "inspect_change_session_control",
            ],
        )

    def get_application_change_session_commits(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        result = self._request_with_fallback_paths(
            method="GET",
            paths=self._change_session_control_paths(handle, suffix="/commits"),
            base_urls=self._planner_base_urls(),
        )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            control = self.inspect_change_session_control(handle=handle)
            fallback = self._commit_evidence_result_from_control(
                control,
                application_id=application_id,
                session_id=session_id,
            )
            if fallback is not None:
                result = fallback
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            scope_hint={
                "scope_type": "artifact",
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
            },
            default_next_allowed_actions=[
                "promote_application_change_session",
                "rollback_application_change_session",
            ],
        )

    def get_application_change_session_promotion_evidence(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        result = self.get_change_session_promotion_evidence(application_id=application_id, session_id=session_id)
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
            control = self.inspect_change_session_control(handle=handle)
            fallback = self._promotion_evidence_result_from_control(
                control,
                application_id=application_id,
                session_id=session_id,
            )
            if fallback is not None:
                result = fallback
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            scope_hint={
                "scope_type": "artifact",
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
            },
            default_next_allowed_actions=[
                "rollback_application_change_session",
                "inspect_change_session_control",
            ],
        )

    def get_application_change_session_preview_status(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        result = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
        normalized = self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            scope_hint={
                "scope_type": "artifact",
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
            },
            default_next_allowed_actions=[
                "prepare_preview_application_change_session",
                "validate_application_change_session",
                "commit_application_change_session",
            ],
        )
        response = normalized.get("response") if isinstance(normalized.get("response"), dict) else {}
        normalized["response"] = {
            "application_id": str(application_id),
            "session_id": str(session_id),
            "current_status": str(response.get("current_status") or ""),
            "next_allowed_actions": response.get("next_allowed_actions") if isinstance(response.get("next_allowed_actions"), list) else [],
            "blocked_reason": str(response.get("blocked_reason") or ""),
            "preview": response.get("preview") if isinstance(response.get("preview"), dict) else {},
            "raw": response.get("raw"),
        }
        return normalized

    def list_runtime_runs(
        self,
        *,
        application_id: str = "",
        session_id: str = "",
        limit: int = 50,
        cursor: str = "",
        status: str = "",
    ) -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs")
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs")
        paths.extend(
            [
                "/xyn/api/runtime-runs",
                "/xyn/api/runs",
                "/api/v1/runs",
            ]
        )
        result = self._request_with_fallback_paths(
            method="GET",
            paths=paths,
            params={
                "limit": int(limit),
                "cursor": str(cursor or "").strip() or None,
                "status": str(status or "").strip() or None,
            },
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok"):
            return result
        body = result.get("response")
        rows = body if isinstance(body, list) else []
        if isinstance(body, dict):
            if isinstance(body.get("runtime_runs"), list):
                rows = body.get("runtime_runs") or []
            elif isinstance(body.get("runs"), list):
                rows = body.get("runs") or []
            elif isinstance(body.get("items"), list):
                rows = body.get("items") or []
        normalized = [self._normalize_runtime_run_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {
            "application_id": normalized_application_id,
            "session_id": normalized_session_id,
            "runtime_runs": normalized,
            "count": len(normalized),
            "next_cursor": str((body or {}).get("next_cursor") or "") if isinstance(body, dict) else "",
        }
        return result

    def get_runtime_run(self, *, run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}"
            )
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}")
        paths.extend(
            [
                f"/xyn/api/runtime-runs/{normalized_run_id}",
                f"/xyn/api/runs/{normalized_run_id}",
                f"/api/v1/runs/{normalized_run_id}",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths, base_urls=self._code_api_base_urls())
        normalized = self._normalize_runtime_run_result(
            result,
            default_next_allowed_actions=[
                "get_runtime_run_logs",
                "get_runtime_run_artifacts",
                "get_runtime_run_commands",
                "cancel_runtime_run",
                "rerun_runtime_run",
            ],
        )
        if bool(normalized.get("ok")):
            return normalized
        if int(normalized.get("status_code") or 0) not in {404, 405}:
            return normalized
        fallback = self._runtime_run_single_from_list_fallback(
            run_id=normalized_run_id,
            application_id=normalized_application_id,
            session_id=normalized_session_id,
            default_next_allowed_actions=[
                "get_runtime_run_logs",
                "get_runtime_run_artifacts",
                "get_runtime_run_commands",
                "cancel_runtime_run",
                "rerun_runtime_run",
            ],
        )
        return fallback or normalized

    def get_runtime_run_logs(self, *, run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/logs"
            )
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/logs")
        paths.extend(
            [
                f"/xyn/api/runtime-runs/{normalized_run_id}/logs",
                f"/xyn/api/runs/{normalized_run_id}/logs",
                f"/xyn/api/runs/{normalized_run_id}/steps",
                f"/api/v1/runs/{normalized_run_id}/steps",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths, base_urls=self._code_api_base_urls())
        if not result.get("ok"):
            return result
        logs = self._normalize_runtime_logs(result.get("response"))
        result["response"] = {
            "run_id": normalized_run_id,
            "logs": logs,
            "count": len(logs),
            "raw": result.get("response"),
        }
        return result

    def get_runtime_run_artifacts(self, *, run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/artifacts"
            )
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/artifacts")
        paths.extend(
            [
                f"/xyn/api/runtime-runs/{normalized_run_id}/artifacts",
                f"/xyn/api/runs/{normalized_run_id}/artifacts",
                f"/api/v1/runs/{normalized_run_id}/artifacts",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths, base_urls=self._code_api_base_urls())
        if not result.get("ok"):
            return result
        artifacts = self._normalize_runtime_artifacts(result.get("response"))
        result["response"] = {
            "run_id": normalized_run_id,
            "artifacts": artifacts,
            "count": len(artifacts),
            "raw": result.get("response"),
        }
        return result

    def get_runtime_run_commands(self, *, run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/commands"
            )
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/commands")
        paths.extend(
            [
                f"/xyn/api/runtime-runs/{normalized_run_id}/commands",
                f"/xyn/api/runs/{normalized_run_id}/commands",
                f"/xyn/api/runs/{normalized_run_id}/steps",
                f"/api/v1/runs/{normalized_run_id}/steps",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths, base_urls=self._code_api_base_urls())
        if not result.get("ok"):
            return result
        commands = self._normalize_runtime_commands(result.get("response"))
        result["response"] = {
            "run_id": normalized_run_id,
            "commands": commands,
            "count": len(commands),
            "raw": result.get("response"),
        }
        return result

    def cancel_runtime_run(self, *, run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/cancel"
            )
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/cancel")
        paths.extend(
            [
                f"/xyn/api/runtime-runs/{normalized_run_id}/cancel",
                f"/xyn/api/runs/{normalized_run_id}/cancel",
                f"/api/v1/runs/{normalized_run_id}/cancel",
            ]
        )
        result = self._request_with_fallback_paths(
            method="POST",
            paths=paths,
            json_payload={},
            base_urls=self._code_api_base_urls(),
        )
        return self._normalize_runtime_run_result(
            result,
            default_next_allowed_actions=[
                "get_runtime_run",
                "get_runtime_run_logs",
                "rerun_runtime_run",
            ],
        )

    def rerun_runtime_run(self, *, run_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_run_id = str(run_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/rerun"
            )
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/retry"
            )
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/rerun")
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/runtime-runs/{normalized_run_id}/retry")
        paths.extend(
            [
                f"/xyn/api/runtime-runs/{normalized_run_id}/rerun",
                f"/xyn/api/runtime-runs/{normalized_run_id}/retry",
                f"/xyn/api/runs/{normalized_run_id}/rerun",
                f"/xyn/api/runs/{normalized_run_id}/retry",
                f"/api/v1/runs/{normalized_run_id}/retry",
            ]
        )
        result = self._request_with_fallback_paths(
            method="POST",
            paths=paths,
            json_payload={},
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            result["response"] = {
                "error": "not_supported",
                "detail": "Runtime rerun/retry endpoint not available in this backend.",
                "blocked_reason": "not_supported",
            }
            return result
        return self._normalize_runtime_run_result(
            result,
            default_next_allowed_actions=[
                "get_runtime_run",
                "get_runtime_run_logs",
                "get_runtime_run_artifacts",
                "get_runtime_run_commands",
            ],
        )

    def _runtime_run_single_from_list_fallback(
        self,
        *,
        run_id: str,
        application_id: str,
        session_id: str,
        default_next_allowed_actions: Optional[list[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        listing = self.list_runtime_runs(
            application_id=application_id,
            session_id=session_id,
            limit=200,
        )
        if not bool(listing.get("ok")):
            return None
        body = listing.get("response") if isinstance(listing.get("response"), dict) else {}
        rows = body.get("runtime_runs") if isinstance(body.get("runtime_runs"), list) else []
        target_run_id = str(run_id or "").strip()
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_run_id = str(row.get("run_id") or row.get("id") or "").strip()
            if row_run_id != target_run_id:
                continue
            payload = row.get("raw") if isinstance(row.get("raw"), dict) else row
            fallback = self._normalize_runtime_run_result(
                {
                    "ok": True,
                    "status_code": 200,
                    "method": "GET",
                    "path": "/api/v1/runs",
                    "base_url": str(listing.get("base_url") or ""),
                    "response": payload,
                },
                default_next_allowed_actions=default_next_allowed_actions,
            )
            response_body = fallback.get("response") if isinstance(fallback.get("response"), dict) else {}
            warnings = response_body.get("warnings") if isinstance(response_body.get("warnings"), list) else []
            warnings.append("run_status_resolved_from_list_runtime_runs_fallback")
            response_body["warnings"] = warnings
            response_body["source"] = "list_runtime_runs_fallback"
            fallback["response"] = response_body
            return fallback
        return None

    def get_dev_task_by_id(self, *, task_id: str, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        normalized_task_id = str(task_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/dev-tasks/{normalized_task_id}"
            )
            paths.append(
                f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/tasks/{normalized_task_id}"
            )
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/dev-tasks/{normalized_task_id}")
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/tasks/{normalized_task_id}")
        paths.extend(
            [
                f"/xyn/api/dev-tasks/{normalized_task_id}",
                f"/xyn/api/dev_tasks/{normalized_task_id}",
                f"/xyn/api/tasks/{normalized_task_id}",
                f"/api/v1/dev-tasks/{normalized_task_id}",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths)
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {"dev_task": self._normalize_dev_task_row(body)}
        return result

    def list_dev_tasks_for_change_session(
        self,
        *,
        application_id: str = "",
        session_id: str = "",
        limit: int = 100,
        status: str = "",
    ) -> Dict[str, Any]:
        normalized_application_id = str(application_id or "").strip()
        normalized_session_id = str(session_id or "").strip()
        paths: list[str] = []
        if normalized_application_id and normalized_session_id:
            paths.append(f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/dev-tasks")
            paths.append(f"/xyn/api/applications/{normalized_application_id}/change-sessions/{normalized_session_id}/tasks")
        if normalized_session_id:
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/dev-tasks")
            paths.append(f"/xyn/api/change-sessions/{normalized_session_id}/tasks")
        paths.extend(
            [
                "/xyn/api/dev-tasks",
                "/xyn/api/dev_tasks",
                "/xyn/api/tasks",
                "/api/v1/dev-tasks",
            ]
        )
        params = {"limit": int(limit), "status": str(status or "").strip() or None}
        if normalized_application_id:
            params["application_id"] = normalized_application_id
        if normalized_session_id:
            params["session_id"] = normalized_session_id
        result = self._request_with_fallback_paths(method="GET", paths=paths, params=params)
        if not result.get("ok"):
            return result
        body = result.get("response")
        rows = body if isinstance(body, list) else []
        if isinstance(body, dict):
            if isinstance(body.get("dev_tasks"), list):
                rows = body.get("dev_tasks") or []
            elif isinstance(body.get("tasks"), list):
                rows = body.get("tasks") or []
            elif isinstance(body.get("items"), list):
                rows = body.get("items") or []
        normalized = [self._normalize_dev_task_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {
            "application_id": normalized_application_id,
            "session_id": normalized_session_id,
            "dev_tasks": normalized,
            "count": len(normalized),
        }
        return result

    def inspect_change_session_control(
        self,
        *,
        application_id: str = "",
        session_id: str = "",
        handle: Optional[ChangeSessionHandle] = None,
    ) -> Dict[str, Any]:
        resolved_handle = handle or self._build_change_session_handle(
            application_id=application_id,
            session_id=session_id,
        )
        paths = self._change_session_control_paths(resolved_handle, suffix="/control")
        result = self._request_with_fallback_paths(
            method="GET",
            paths=paths,
            base_urls=self._planner_base_urls(),
        )
        return self._normalize_planner_route_error(result, attempted_paths=paths)

    @staticmethod
    def _extract_pending_checkpoints_from_control_response(response_body: Dict[str, Any]) -> list[Dict[str, Any]]:
        control = response_body.get("control") if isinstance(response_body.get("control"), dict) else {}
        session = control.get("session") if isinstance(control.get("session"), dict) else {}
        planning = session.get("planning") if isinstance(session.get("planning"), dict) else {}
        pending = planning.get("pending_checkpoints")
        if not isinstance(pending, list):
            return []
        rows: list[Dict[str, Any]] = []
        for item in pending:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "id": str(item.get("id") or "").strip(),
                    "checkpoint_key": str(item.get("checkpoint_key") or "").strip(),
                    "label": str(item.get("label") or "").strip(),
                    "status": str(item.get("status") or "").strip(),
                    "required_before": str(item.get("required_before") or "").strip(),
                    "payload": item.get("payload") if isinstance(item.get("payload"), dict) else {},
                }
            )
        return rows

    @staticmethod
    def _extract_planner_prompt_contract(response_body: Dict[str, Any]) -> Dict[str, Any]:
        control = response_body.get("control") if isinstance(response_body.get("control"), dict) else {}
        session = control.get("session") if isinstance(control.get("session"), dict) else {}
        planning = session.get("planning") if isinstance(session.get("planning"), dict) else {}

        prompt_candidates: list[Dict[str, Any]] = []
        for candidate in (
            planning.get("pending_prompt"),
            planning.get("planner_prompt"),
            planning.get("prompt"),
            response_body.get("pending_prompt"),
            response_body.get("planner_prompt"),
            response_body.get("prompt"),
        ):
            if isinstance(candidate, dict):
                prompt_candidates.append(candidate)

        prompt = prompt_candidates[0] if prompt_candidates else {}
        if not prompt and isinstance(planning.get("prompts"), list) and planning.get("prompts"):
            first = planning.get("prompts")[0]
            if isinstance(first, dict):
                prompt = first

        status_token = str(prompt.get("status") or planning.get("prompt_status") or "").strip().lower()
        pending = bool(prompt) and status_token not in {"resolved", "dismissed", "answered", "complete"}
        if not prompt and str(planning.get("pending_planner_prompt") or "").strip():
            pending = True

        response_schema = prompt.get("response_schema")
        if not isinstance(response_schema, dict):
            response_schema = {}
        response_examples = prompt.get("response_examples")
        if not isinstance(response_examples, list):
            response_examples = []
        option_set = prompt.get("option_set") if isinstance(prompt.get("option_set"), dict) else {}
        options = option_set.get("options") if isinstance(option_set.get("options"), list) else []
        canonical_option_ids = [
            str(item.get("id") or item.get("option_id") or "").strip()
            for item in options
            if isinstance(item, dict) and str(item.get("id") or item.get("option_id") or "").strip()
        ]
        canonical_options: list[Dict[str, Any]] = []
        for item in options:
            if not isinstance(item, dict):
                continue
            option_id = str(item.get("id") or item.get("option_id") or "").strip()
            if not option_id:
                continue
            canonical_options.append(
                {
                    "id": option_id,
                    "label": str(item.get("label") or item.get("title") or option_id),
                    "description": str(item.get("description") or item.get("summary") or ""),
                }
            )

        expected_response_kind = str(
            prompt.get("expected_response_kind")
            or option_set.get("expected_response_kind")
            or ("option_set" if canonical_option_ids else "")
        ).strip()
        allows_multiple = bool(
            prompt.get("allows_multiple")
            or option_set.get("allows_multiple")
            or response_schema.get("allows_multiple")
        )
        prompt_kind = str(prompt.get("kind") or expected_response_kind or "").strip()
        if not prompt_kind:
            prompt_kind = "option_set" if canonical_option_ids else "freeform"
        prompt_id = str(prompt.get("id") or prompt.get("prompt_id") or "").strip()
        return {
            "pending": pending,
            "prompt_id": prompt_id,
            "kind": prompt_kind,
            "message": str(prompt.get("message") or prompt.get("text") or "").strip(),
            "expected_response_kind": expected_response_kind,
            "allows_multiple": allows_multiple,
            "options": canonical_options,
            "canonical_option_identifiers": canonical_option_ids,
            "response_schema": response_schema,
            "response_examples": response_examples,
            "answer_payload_schema": {
                "type": "object",
                "required": ["prompt_id", "response"],
                "properties": {
                    "prompt_id": {"type": "string", "const": prompt_id},
                    "response": response_schema if isinstance(response_schema, dict) else {},
                    "metadata": {"type": "object"},
                },
                "accepted_legacy_fields": ["selected_option_id", "selected_option_ids", "option_id"],
            },
        }

    @staticmethod
    def _validate_prompt_response_schema(*, response_value: Any, response_schema: Dict[str, Any]) -> list[str]:
        if not isinstance(response_schema, dict) or not response_schema:
            return []
        errors: list[str] = []
        schema_type = str(response_schema.get("type") or "").strip().lower()
        if schema_type == "object" and not isinstance(response_value, dict):
            errors.append("response must be an object")
            return errors
        if schema_type == "array" and not isinstance(response_value, list):
            errors.append("response must be an array")
            return errors
        if schema_type == "string" and not isinstance(response_value, str):
            errors.append("response must be a string")
            return errors
        if isinstance(response_value, dict):
            required = response_schema.get("required")
            if isinstance(required, list):
                for field in required:
                    token = str(field or "").strip()
                    if token and token not in response_value:
                        errors.append(f"response.{token} is required")
            properties = response_schema.get("properties")
            if isinstance(properties, dict):
                for key, descriptor in properties.items():
                    if key not in response_value or not isinstance(descriptor, dict):
                        continue
                    expected = str(descriptor.get("type") or "").strip().lower()
                    value = response_value.get(key)
                    if expected == "string" and not isinstance(value, str):
                        errors.append(f"response.{key} must be a string")
                    elif expected == "array" and not isinstance(value, list):
                        errors.append(f"response.{key} must be an array")
                    elif expected == "object" and not isinstance(value, dict):
                        errors.append(f"response.{key} must be an object")
                    enum_values = descriptor.get("enum")
                    if isinstance(enum_values, list) and value not in enum_values:
                        errors.append(f"response.{key} must be one of {enum_values}")
        return errors

    @staticmethod
    def _normalize_planner_prompt_answer_payload(
        *,
        payload: Dict[str, Any],
        prompt_contract: Dict[str, Any],
    ) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        prompt_id = str(payload.get("prompt_id") or "").strip()
        response_value = payload.get("response")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        compatibility_notes: list[str] = []

        if response_value is None:
            if "selected_option_id" in payload:
                response_value = {"selected_option_id": payload.get("selected_option_id")}
                compatibility_notes.append("selected_option_id")
            elif "option_id" in payload:
                response_value = {"selected_option_id": payload.get("option_id")}
                compatibility_notes.append("option_id")
            elif "selected_option_ids" in payload:
                response_value = {"selected_option_ids": payload.get("selected_option_ids")}
                compatibility_notes.append("selected_option_ids")

        if not prompt_id and compatibility_notes:
            prompt_id = str(prompt_contract.get("prompt_id") or "").strip()
            if prompt_id:
                compatibility_notes.append("prompt_id_from_pending_prompt")

        if not prompt_id:
            return None, {
                "error": "invalid_prompt_response",
                "detail": "prompt_id is required",
                "blocked_reason": "prompt_response_invalid",
                "required_fields": ["prompt_id", "response"],
                "response_schema": prompt_contract.get("response_schema") if isinstance(prompt_contract.get("response_schema"), dict) else {},
                "response_examples": prompt_contract.get("response_examples") if isinstance(prompt_contract.get("response_examples"), list) else [],
            }
        if response_value is None:
            return None, {
                "error": "invalid_prompt_response",
                "detail": "response is required",
                "blocked_reason": "prompt_response_invalid",
                "required_fields": ["prompt_id", "response"],
                "response_schema": prompt_contract.get("response_schema") if isinstance(prompt_contract.get("response_schema"), dict) else {},
                "response_examples": prompt_contract.get("response_examples") if isinstance(prompt_contract.get("response_examples"), list) else [],
            }

        schema_errors = XynApiAdapter._validate_prompt_response_schema(
            response_value=response_value,
            response_schema=prompt_contract.get("response_schema") if isinstance(prompt_contract.get("response_schema"), dict) else {},
        )
        if schema_errors:
            return None, {
                "error": "invalid_prompt_response_schema",
                "detail": "response did not satisfy planner prompt response schema",
                "blocked_reason": "prompt_response_invalid",
                "validation_errors": schema_errors,
                "required_fields": ["prompt_id", "response"],
                "response_schema": prompt_contract.get("response_schema") if isinstance(prompt_contract.get("response_schema"), dict) else {},
                "response_examples": prompt_contract.get("response_examples") if isinstance(prompt_contract.get("response_examples"), list) else [],
            }

        canonical_option_ids = prompt_contract.get("canonical_option_identifiers")
        if isinstance(canonical_option_ids, list) and canonical_option_ids:
            selected: list[str] = []
            if isinstance(response_value, dict):
                if isinstance(response_value.get("selected_option_id"), str):
                    selected = [str(response_value.get("selected_option_id") or "").strip()]
                elif isinstance(response_value.get("selected_option_ids"), list):
                    selected = [str(item).strip() for item in response_value.get("selected_option_ids") if str(item).strip()]
            if selected:
                invalid = [item for item in selected if item not in canonical_option_ids]
                if invalid:
                    return None, {
                        "error": "invalid_prompt_option",
                        "detail": "response references unknown prompt option id",
                        "blocked_reason": "prompt_response_invalid",
                        "invalid_option_ids": invalid,
                        "canonical_option_ids": canonical_option_ids,
                    }

        normalized: Dict[str, Any] = {
            "prompt_id": prompt_id,
            "response": response_value,
        }
        if metadata:
            normalized["metadata"] = metadata
        if compatibility_notes:
            normalized["compatibility_notes"] = compatibility_notes
        return normalized, None

    @staticmethod
    def _assessment_state(
        *,
        error_classification: str,
        blocked_reason: str,
        prompt_pending: bool,
        status_code: int,
        continuity_retry_reason: str,
    ) -> str:
        if error_classification == "auth_expired":
            return "auth_expired"
        if error_classification == "workspace_forbidden":
            return "workspace_forbidden"
        if error_classification == "binding_rotated":
            return "binding_rotated"
        if error_classification == "empty_tool_surface":
            return "empty_tool_surface"
        if error_classification == "contract_mismatch":
            return "control_contract_failure"
        if error_classification == "backend_validation_error":
            return "backend_validation_error"
        if error_classification in {"backend_server_error", "transient_transport_failure"}:
            return "backend_server_error"
        if status_code == 404 or blocked_reason in {"not_found", "session_not_found", "session_stale"}:
            return "session_stale"
        if prompt_pending:
            return "planner_prompt_pending"
        if continuity_retry_reason == "binding_rotated":
            return "binding_rotated"
        return "ready"

    def assess_change_session_readiness(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        inspected = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
        normalized = self._normalize_change_session_result(
            inspected,
            application_id=application_id,
            session_id=session_id,
            default_next_allowed_actions=[
                "run_change_session_control_action",
                "get_application_change_session_plan",
                "stage_apply_application_change_session",
            ],
        )
        body = normalized.get("response") if isinstance(normalized.get("response"), dict) else {}
        raw = body.get("raw") if isinstance(body.get("raw"), dict) else {}
        continuity = body.get("continuity") if isinstance(body.get("continuity"), dict) else {}
        prompt = self._extract_planner_prompt_contract(raw)
        status_code = int(normalized.get("status_code") or 0)
        error_classification = str(body.get("error_classification") or normalized.get("error_classification") or "").strip()
        blocked_reason = str(body.get("blocked_reason") or "").strip()
        retry_reason = str(continuity.get("retry_reason") or "").strip()
        session_key = self._session_state_key(application_id=application_id, session_id=session_id)

        auth_state = "fresh" if normalized.get("ok") else ("expired" if error_classification == "auth_expired" else "unknown")
        assessment_state = self._assessment_state(
            error_classification=error_classification,
            blocked_reason=blocked_reason,
            prompt_pending=bool(prompt.get("pending")),
            status_code=status_code,
            continuity_retry_reason=retry_reason,
        )
        stale_session_context = assessment_state == "session_stale"
        session_readable = bool(normalized.get("ok")) and assessment_state not in {
            "auth_expired",
            "backend_server_error",
            "control_contract_failure",
            "session_stale",
        }
        now_ts = self._now_ts()
        if session_readable:
            self._session_last_success_read_at[session_key] = now_ts
            self._session_last_failure_classification[session_key] = ""
        elif error_classification:
            self._session_last_failure_classification[session_key] = error_classification
        tools_discoverable = bool(int(self._planner_binding_state.tool_surface_count or 0) > 0)
        last_success_read_ts = float(self._session_last_success_read_at.get(session_key) or 0.0)
        last_failure_classification = str(
            self._session_last_failure_classification.get(session_key)
            or error_classification
            or ""
        ).strip()
        auto_retry_safe = assessment_state in {
            "binding_rotated",
            "empty_tool_surface",
            "transient_transport_failure",
            "ready",
            "planner_prompt_pending",
        }
        session_recreation_recommended = assessment_state in {"session_stale", "control_contract_failure"}
        binding_base_url = str(normalized.get("base_url") or self._config.control_api_base_url).rstrip("/")
        control_paths = self._change_session_control_paths(
            self._build_change_session_handle(application_id=application_id, session_id=session_id),
            suffix="/control",
        )
        binding_path = str(control_paths[0] if control_paths else f"/xyn/api/change-sessions/{session_id}/control")
        scope = body.get("scope") if isinstance(body.get("scope"), dict) else {}
        resolved_application_id = str(body.get("application_id") or scope.get("application_id") or application_id or "")
        resolved_session_id = str(body.get("session_id") or session_id or "")
        assessment = {
            "assessment_state": assessment_state,
            "binding": {
                "tool_identity": "inspect_change_session_control",
                "control_path": binding_path,
                "binding_id": self._binding_id(binding_base_url, binding_path),
                "base_url": binding_base_url,
                "previous_binding_id": str(continuity.get("previous_binding_id") or ""),
                "new_binding_id": str(continuity.get("new_binding_id") or ""),
                "retry_reason": retry_reason,
                "auth_refreshed": bool(continuity.get("auth_refreshed")),
                "action_reissued": bool(continuity.get("action_reissued")),
            },
            "binding_state": self._planner_binding_snapshot(),
            "tools_discoverable": tools_discoverable,
            "auth_session": {
                "state": auth_state,
                "request_bearer_present": bool(get_request_bearer_token()),
                "configured_bearer_present": bool(str(self._config.bearer_token or "").strip()),
            },
            "context": {
                "application_id": resolved_application_id,
                "session_id": resolved_session_id,
                "scope_type": str(body.get("scope_type") or ""),
                "scope": scope,
                "stale_session_context": stale_session_context,
            },
            "session_readability": {
                "readable": session_readable,
                "last_successful_read_timestamp": last_success_read_ts,
            },
            "planner_prompt": prompt,
            "allowed_next_actions": body.get("next_allowed_actions") if isinstance(body.get("next_allowed_actions"), list) else [],
            "retry_safety": {
                "idempotent_reads_safe": True,
                "control_actions_safe_when_idempotency_key_present": True,
                "session_create_safe_for_auto_retry": False,
                "automatic_retry_safe": auto_retry_safe,
            },
            "last_known_error_classification": last_failure_classification,
            "blocked_reason": blocked_reason,
            "session_recreation_recommended": session_recreation_recommended,
        }
        normalized["response"] = assessment
        return normalized

    def list_change_session_pending_checkpoints(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        result = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
        response_body = result.get("response") if isinstance(result.get("response"), dict) else {}
        scope = response_body.get("scope") if isinstance(response_body.get("scope"), dict) else {}
        pending = self._extract_pending_checkpoints_from_control_response(response_body)
        result["response"] = {
            "application_id": str(response_body.get("application_id") or scope.get("application_id") or application_id or ""),
            "session_id": str(response_body.get("session_id") or session_id or ""),
            "scope_type": str(response_body.get("scope_type") or ""),
            "scope": scope,
            "pending_checkpoints": pending,
            "count": len(pending),
            "raw": response_body,
        }
        return result

    def decide_change_session_checkpoint(
        self,
        *,
        application_id: str = "",
        session_id: str = "",
        checkpoint_id: str = "",
        decision: str = "approved",
        notes: str = "",
    ) -> Dict[str, Any]:
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision in {"approve", "approved", "accept"}:
            normalized_decision = "approved"
        elif normalized_decision in {"reject", "rejected", "deny"}:
            normalized_decision = "rejected"
        if normalized_decision not in {"approved", "rejected"}:
            handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
            decision_paths = self._change_session_control_paths(handle, suffix="/checkpoints/<checkpoint_id>/decision")
            return {
                "ok": False,
                "status_code": 400,
                "method": "POST",
                "path": decision_paths[0] if decision_paths else "/xyn/api/change-sessions/<session_id>/checkpoints/<checkpoint_id>/decision",
                "base_url": str(self._config.control_api_base_url).rstrip("/"),
                "response": {
                    "error": "invalid_decision",
                    "detail": "decision must be one of: approved, rejected",
                    "blocked_reason": "invalid_request",
                },
            }

        resolved_checkpoint_id = str(checkpoint_id or "").strip()
        if not resolved_checkpoint_id:
            inspect_result = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
            inspect_body = inspect_result.get("response") if isinstance(inspect_result.get("response"), dict) else {}
            pending = self._extract_pending_checkpoints_from_control_response(inspect_body)
            if not pending:
                handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
                decision_paths = self._change_session_control_paths(handle, suffix="/checkpoints/<checkpoint_id>/decision")
                return {
                    "ok": False,
                    "status_code": 409,
                    "method": "POST",
                    "path": decision_paths[0] if decision_paths else "/xyn/api/change-sessions/<session_id>/checkpoints/<checkpoint_id>/decision",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "error": "checkpoint_not_pending",
                        "detail": "No pending planning checkpoint was found for this change session.",
                        "blocked_reason": "checkpoint_not_pending",
                        "next_allowed_actions": ["inspect_change_session_control", "stage_apply_application_change_session"],
                    },
                }
            resolved_checkpoint_id = str((pending[0] or {}).get("id") or "").strip()
            if not resolved_checkpoint_id:
                handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
                decision_paths = self._change_session_control_paths(handle, suffix="/checkpoints/<checkpoint_id>/decision")
                return {
                    "ok": False,
                    "status_code": 409,
                    "method": "POST",
                    "path": decision_paths[0] if decision_paths else "/xyn/api/change-sessions/<session_id>/checkpoints/<checkpoint_id>/decision",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "error": "checkpoint_not_resolved",
                        "detail": "A pending checkpoint exists but checkpoint id could not be resolved.",
                        "blocked_reason": "checkpoint_not_resolved",
                        "next_allowed_actions": ["list_change_session_pending_checkpoints", "inspect_change_session_control"],
                    },
                }

        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        suffix = f"/checkpoints/{resolved_checkpoint_id}/decision"
        paths = self._change_session_control_paths(handle, suffix=suffix)
        return self._request_with_fallback_paths(
            method="POST",
            paths=paths,
            json_payload={
                "decision": normalized_decision,
                "notes": str(notes or "").strip(),
            },
            base_urls=self._planner_base_urls(),
        )

    def run_change_session_control_action(
        self,
        *,
        application_id: str = "",
        session_id: str = "",
        operation: str = "",
        action_payload: Optional[Dict[str, Any]] = None,
        handle: Optional[ChangeSessionHandle] = None,
    ) -> Dict[str, Any]:
        normalized_operation = str(operation or "").strip().lower()
        if not normalized_operation:
            handle_for_error = handle or self._build_change_session_handle(
                application_id=application_id,
                session_id=session_id,
            )
            paths = self._change_session_control_paths(handle_for_error, suffix="/control/actions")
            return {
                "ok": False,
                "status_code": 400,
                "method": "POST",
                "path": paths[0] if paths else "/xyn/api/change-sessions/<session_id>/control/actions",
                "base_url": str(self._config.control_api_base_url).rstrip("/"),
                "response": {
                    "error": "operation is required",
                    "blocked_reason": "backend_validation_error",
                    "next_allowed_actions": ["inspect_change_session_control"],
                },
                "error_classification": "backend_validation_error",
            }
        if normalized_operation in {"decide_checkpoint", "approve_checkpoint"}:
            payload = dict(action_payload or {})
            checkpoint_id = str(payload.get("checkpoint_id") or payload.get("id") or "").strip()
            decision = str(payload.get("decision") or "").strip()
            if not decision and normalized_operation == "approve_checkpoint":
                decision = "approved"
            notes = str(payload.get("notes") or "").strip()
            return self.decide_change_session_checkpoint(
                application_id=application_id,
                session_id=session_id,
                checkpoint_id=checkpoint_id,
                decision=decision or "approved",
                notes=notes,
            )
        resolved_handle = handle or self._build_change_session_handle(
            application_id=application_id,
            session_id=session_id,
        )
        payload = dict(action_payload or {})
        payload["operation"] = str(operation or "").strip()
        if normalized_operation == "respond_to_planner_prompt":
            control = self.inspect_change_session_control(handle=resolved_handle)
            if not bool(control.get("ok")):
                return self._normalize_planner_route_error(
                    control,
                    attempted_paths=self._change_session_control_paths(resolved_handle, suffix="/control"),
                )
            control_body = control.get("response") if isinstance(control.get("response"), dict) else {}
            prompt_contract = self._extract_planner_prompt_contract(control_body)
            if not bool(prompt_contract.get("pending")):
                return {
                    "ok": False,
                    "status_code": 409,
                    "method": "POST",
                    "path": self._change_session_control_paths(resolved_handle, suffix="/control/actions")[0],
                    "base_url": str(control.get("base_url") or self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "error": "planner_prompt_not_pending",
                        "detail": "No pending planner prompt is available for this change session.",
                        "blocked_reason": "planner_prompt_not_pending",
                        "next_allowed_actions": ["inspect_change_session_control", "get_application_change_session_plan"],
                    },
                    "error_classification": "backend_validation_error",
                }
            normalized_prompt_payload, validation_error = self._normalize_planner_prompt_answer_payload(
                payload=payload,
                prompt_contract=prompt_contract,
            )
            if validation_error:
                return {
                    "ok": False,
                    "status_code": 400,
                    "method": "POST",
                    "path": self._change_session_control_paths(resolved_handle, suffix="/control/actions")[0],
                    "base_url": str(control.get("base_url") or self._config.control_api_base_url).rstrip("/"),
                    "response": validation_error,
                    "error_classification": "backend_validation_error",
                }
            expected_prompt_id = str(prompt_contract.get("prompt_id") or "").strip()
            supplied_prompt_id = str((normalized_prompt_payload or {}).get("prompt_id") or "").strip()
            if expected_prompt_id and supplied_prompt_id and expected_prompt_id != supplied_prompt_id:
                return {
                    "ok": False,
                    "status_code": 409,
                    "method": "POST",
                    "path": self._change_session_control_paths(resolved_handle, suffix="/control/actions")[0],
                    "base_url": str(control.get("base_url") or self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "error": "planner_prompt_superseded",
                        "detail": "Provided prompt_id does not match current pending planner prompt.",
                        "blocked_reason": "planner_prompt_superseded",
                        "provided_prompt_id": supplied_prompt_id,
                        "current_prompt_id": expected_prompt_id,
                        "next_allowed_actions": ["inspect_change_session_control"],
                    },
                    "error_classification": "backend_validation_error",
                }
            payload = {
                "operation": "respond_to_planner_prompt",
                "prompt_id": supplied_prompt_id,
                "response": (normalized_prompt_payload or {}).get("response"),
            }
            metadata = (normalized_prompt_payload or {}).get("metadata")
            if isinstance(metadata, dict) and metadata:
                payload["metadata"] = metadata
            compatibility_notes = (normalized_prompt_payload or {}).get("compatibility_notes")
            if isinstance(compatibility_notes, list) and compatibility_notes:
                payload["client_compatibility_notes"] = compatibility_notes

        # Mutating control actions may only retry when we can prove the prior request
        # did not reach a viable route (e.g. stale binding 404/405). Transport errors
        # are reported as unknown delivery state unless idempotency is handled upstream.
        allow_reissue_on_transport_error = False
        paths = self._change_session_control_paths(resolved_handle, suffix="/control/actions")
        result = self._request_with_fallback_paths(
            method="POST",
            paths=paths,
            json_payload=payload,
            base_urls=self._planner_base_urls(),
            allow_reissue_on_transport_error=allow_reissue_on_transport_error,
        )
        return self._normalize_planner_route_error(result, attempted_paths=paths)

    def get_change_session_promotion_evidence(self, *, application_id: str = "", session_id: str = "") -> Dict[str, Any]:
        handle = self._build_change_session_handle(application_id=application_id, session_id=session_id)
        return self._request_with_fallback_paths(
            method="GET",
            paths=self._change_session_control_paths(handle, suffix="/promotion-evidence"),
            base_urls=self._planner_base_urls(),
        )

    def _commit_evidence_result_from_control(
        self,
        control_result: Dict[str, Any],
        *,
        application_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        if not bool(control_result.get("ok")):
            return None
        body = control_result.get("response") if isinstance(control_result.get("response"), dict) else {}
        commit_shas = self._extract_commit_shas(body)
        if not commit_shas:
            return None
        commits = [{"commit_sha": sha} for sha in commit_shas]
        return {
            "ok": True,
            "status_code": 200,
            "method": "GET",
            "path": f"/xyn/api/change-sessions/{session_id}/commits",
            "base_url": str(control_result.get("base_url") or self._config.control_api_base_url).rstrip("/"),
            "response": {
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
                "status": "committed",
                "commits": commits,
                "commit_shas": commit_shas,
                "changed_files": self._extract_changed_files(body),
                "raw": body,
            },
        }

    def _promotion_evidence_result_from_control(
        self,
        control_result: Dict[str, Any],
        *,
        application_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        if not bool(control_result.get("ok")):
            return None
        body = control_result.get("response") if isinstance(control_result.get("response"), dict) else {}
        evidence_ids = self._extract_promotion_evidence_ids(body)
        if not evidence_ids:
            return None
        return {
            "ok": True,
            "status_code": 200,
            "method": "GET",
            "path": f"/xyn/api/change-sessions/{session_id}/promotion-evidence",
            "base_url": str(control_result.get("base_url") or self._config.control_api_base_url).rstrip("/"),
            "response": {
                "application_id": str(application_id or ""),
                "session_id": str(session_id or ""),
                "status": "promoted",
                "promotion_evidence_ids": evidence_ids,
                "raw": body,
            },
        }

    def get_release_target_deployment_plan(self, *, target_id: str) -> Dict[str, Any]:
        result = self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/deployment_plan",
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def create_release_target_deployment_preparation_evidence(
        self, *, target_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        result = self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/deployment_preparation_evidence",
            json_payload=dict(payload or {}),
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def get_release_target_deployment_preparation_evidence(self, *, target_id: str, limit: int = 10) -> Dict[str, Any]:
        result = self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/deployment_preparation_evidence",
            params={"limit": int(limit)},
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def create_release_target_execution_preparation_handoff(
        self, *, target_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        result = self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_preparation_handoff",
            json_payload=dict(payload or {}),
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def get_release_target_execution_preparation_handoff(self, *, target_id: str, limit: int = 10) -> Dict[str, Any]:
        result = self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/execution_preparation_handoff",
            params={"limit": int(limit)},
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def approve_release_target_execution_preparation(
        self, *, target_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        result = self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_preparation_approval",
            json_payload=dict(payload or {}),
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def consume_release_target_execution_preparation(
        self, *, target_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        result = self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_preparation_consume",
            json_payload=dict(payload or {}),
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def run_release_target_execution_step(self, *, target_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_step",
            json_payload=dict(payload or {}),
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def approve_release_target_execution_step(self, *, target_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_step_approval",
            json_payload=dict(payload or {}),
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def get_release_target_execution_step_history(self, *, target_id: str, limit: int = 10) -> Dict[str, Any]:
        result = self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/execution_step",
            params={"limit": int(limit)},
        )
        return self._with_release_target_not_found_hint(result, target_id=target_id)

    def list_release_targets(self) -> Dict[str, Any]:
        result = self._request(method="GET", path="/xyn/api/release-targets")
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("release_targets") if isinstance(body.get("release_targets"), list) else []
        normalized = [self._release_target_discovery_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {"release_targets": normalized, "count": len(normalized)}
        return result

    def get_release_target(self, *, target_id: str) -> Dict[str, Any]:
        result = self._request(method="GET", path=f"/xyn/api/release-targets/{target_id}")
        if not result.get("ok"):
            return self._with_release_target_not_found_hint(result, target_id=target_id)
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {"release_target": self._release_target_discovery_row(body)}
        return result

    def list_artifacts(self, *, limit: Optional[int] = None, offset: Optional[int] = None) -> Dict[str, Any]:
        resolved_limit = 100 if limit is None else int(limit)
        resolved_offset = 0 if offset is None else int(offset)
        if resolved_limit < 1 or resolved_limit > 500:
            return {
                "ok": False,
                "status_code": 400,
                "method": "GET",
                "path": "/xyn/api/artifacts",
                "response": {"error": "invalid_pagination", "detail": "limit must be between 1 and 500"},
            }
        if resolved_offset < 0:
            return {
                "ok": False,
                "status_code": 400,
                "method": "GET",
                "path": "/xyn/api/artifacts",
                "response": {"error": "invalid_pagination", "detail": "offset must be >= 0"},
            }

        # Compatibility strategy:
        # - Prefer control-plane /xyn/api/artifacts so MCP discovery mirrors
        #   production/UI artifact registry results (xyn-api, Workbench, etc).
        # - Some handlers reject offset-style params; retry same path without offset.
        # - Fall back to /api/v1/artifacts when control routes are unavailable.
        explicit_params = {"limit": resolved_limit, "offset": resolved_offset}
        no_offset_params = {"limit": resolved_limit}
        params_variants = [explicit_params]
        if resolved_offset == 0:
            params_variants.append(no_offset_params)

        last_result: Dict[str, Any] = {"ok": False, "status_code": 404, "response": {"error": "not_found"}}
        # First pass: control API base (UI/production registry view).
        for base_url in [self._config.control_api_base_url]:
            for path in ["/xyn/api/artifacts", "/api/v1/artifacts"]:
                for params in params_variants:
                    result = self._request(method="GET", path=path, params=params, base_url=base_url)
                    last_result = result
                    if bool(result.get("ok")):
                        body = result.get("response") if isinstance(result.get("response"), dict) else {}
                        rows = (
                            body.get("artifacts")
                            if isinstance(body.get("artifacts"), list)
                            else (body.get("items") if isinstance(body.get("items"), list) else [])
                        )
                        normalized = [self._artifact_discovery_row(row) for row in rows if isinstance(row, dict)]
                        result["response"] = {
                            "artifacts": normalized,
                            "count": len(normalized),
                            "next_cursor": body.get("next_cursor"),
                        }
                        return result
                    code = int(result.get("status_code") or 0)
                    if code in {400, 401, 403, 404, 405, 503} | _REDIRECT_STATUS_CODES:
                        continue
                    return result
        # Second pass: additional code API bases as compatibility fallback.
        for base_url in self._code_api_base_urls():
            if str(base_url).strip() == str(self._config.control_api_base_url).strip():
                continue
            for path in ["/xyn/api/artifacts", "/api/v1/artifacts"]:
                for params in params_variants:
                    result = self._request(method="GET", path=path, params=params, base_url=base_url)
                    last_result = result
                    if bool(result.get("ok")):
                        body = result.get("response") if isinstance(result.get("response"), dict) else {}
                        rows = (
                            body.get("artifacts")
                            if isinstance(body.get("artifacts"), list)
                            else (body.get("items") if isinstance(body.get("items"), list) else [])
                        )
                        normalized = [self._artifact_discovery_row(row) for row in rows if isinstance(row, dict)]
                        result["response"] = {
                            "artifacts": normalized,
                            "count": len(normalized),
                            "next_cursor": body.get("next_cursor"),
                        }
                        return result
                    code = int(result.get("status_code") or 0)
                    if code in {400, 401, 403, 404, 405, 503} | _REDIRECT_STATUS_CODES:
                        continue
                    return result
        result = last_result
        if not result.get("ok"):
            return result
        return result

    def list_remote_artifact_candidates(
        self,
        *,
        manifest_source: str = "",
        package_source: str = "",
        artifact_slug: str = "",
        artifact_type: str = "",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if str(manifest_source or "").strip():
            params["manifest_source"] = str(manifest_source).strip()
        if str(package_source or "").strip():
            params["package_source"] = str(package_source).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_type or "").strip():
            params["artifact_type"] = str(artifact_type).strip()

        result = self._request_with_fallback_paths(
            method="GET",
            paths=["/xyn/api/artifacts/remote-candidates"],
            params=params,
            base_urls=[self._config.control_api_base_url],
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("candidates") if isinstance(body.get("candidates"), list) else []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            remote_source = row.get("remote_source") if isinstance(row.get("remote_source"), dict) else {}
            normalized.append(
                {
                    "artifact_slug": str(row.get("artifact_slug") or ""),
                    "title": str(row.get("title") or ""),
                    "artifact_type": str(row.get("artifact_type") or ""),
                    "summary": str(row.get("summary") or ""),
                    "installed": bool(row.get("installed")),
                    "artifact_origin": str(row.get("artifact_origin") or ""),
                    "source_ref_type": str(row.get("source_ref_type") or ""),
                    "source_ref_id": str(row.get("source_ref_id") or ""),
                    "manifest_source": str(remote_source.get("manifest_source") or ""),
                    "package_source": str(remote_source.get("package_source") or ""),
                    "remote_source": remote_source,
                }
            )
        result["response"] = {
            "candidates": normalized,
            "count": len(normalized),
            "selection_hint": "Remote candidates are separate from list_artifacts; pass artifact_source into change-session creation.",
        }
        return result

    def list_remote_artifact_sources(self) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=["/xyn/api/artifacts/remote-sources"],
            base_urls=[self._config.control_api_base_url],
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("sources") if isinstance(body.get("sources"), list) else []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "source": str(row.get("source") or ""),
                    "source_type": str(row.get("source_type") or ""),
                    "bucket": str(row.get("bucket") or ""),
                    "prefix": str(row.get("prefix") or ""),
                    "region": str(row.get("region") or ""),
                }
            )
        result["response"] = {
            "sources": normalized,
            "count": len(normalized),
            "source_mode": str(body.get("source_mode") or ""),
            "configured_region": str(body.get("configured_region") or ""),
        }
        return result

    def search_remote_artifact_catalog(
        self,
        *,
        query: str = "",
        artifact_slug: str = "",
        artifact_type: str = "",
        source_root: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if str(query or "").strip():
            params["q"] = str(query).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_type or "").strip():
            params["artifact_type"] = str(artifact_type).strip()
        if str(source_root or "").strip():
            params["source_root"] = str(source_root).strip()
        if int(limit or 0) > 0:
            params["limit"] = int(limit)
        if str(cursor or "").strip():
            params["cursor"] = str(cursor).strip()
        result = self._request_with_fallback_paths(
            method="GET",
            paths=["/xyn/api/artifacts/remote-catalog"],
            params=params,
            base_urls=[self._config.control_api_base_url],
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("candidates") if isinstance(body.get("candidates"), list) else []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            remote_source = row.get("remote_source") if isinstance(row.get("remote_source"), dict) else {}
            normalized.append(
                {
                    "artifact_slug": str(row.get("artifact_slug") or ""),
                    "title": str(row.get("title") or ""),
                    "artifact_type": str(row.get("artifact_type") or ""),
                    "summary": str(row.get("summary") or ""),
                    "installed": bool(row.get("installed")),
                    "artifact_origin": str(row.get("artifact_origin") or ""),
                    "source_ref_type": str(row.get("source_ref_type") or ""),
                    "source_ref_id": str(row.get("source_ref_id") or ""),
                    "manifest_source": str(remote_source.get("manifest_source") or ""),
                    "package_source": str(remote_source.get("package_source") or ""),
                    "remote_source": remote_source,
                }
            )
        result["response"] = {
            "candidates": normalized,
            "count": int(body.get("count") or len(normalized)),
            "total": int(body.get("total") or len(normalized)),
            "next_cursor": str(body.get("next_cursor") or ""),
            "source_roots": body.get("source_roots") if isinstance(body.get("source_roots"), list) else [],
            "errors": body.get("errors") if isinstance(body.get("errors"), list) else [],
        }
        return result

    def get_artifact(self, *, artifact_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/xyn/api/artifacts/{artifact_id}", f"/api/v1/artifacts/{artifact_id}"],
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {"artifact": self._artifact_discovery_row(body)}
        return result

    def get_artifact_source_tree(
        self,
        *,
        artifact_slug: str = "",
        artifact_id: str = "",
        include_line_counts: bool = True,
        max_files: Optional[int] = None,
        max_depth: Optional[int] = None,
        include_files: bool = True,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"include_line_counts": bool(include_line_counts)}
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if max_files is not None:
            params["max_files"] = int(max_files)
        if max_depth is not None:
            params["max_depth"] = int(max_depth)
        params["include_files"] = bool(include_files)
        result = self._request_with_fallback_paths(
            method="GET",
            paths=_ARTIFACT_SOURCE_TREE_PATHS,
            params=params,
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and self._should_source_fallback(int(result.get("status_code") or 0)):
            resolved = self._artifact_files_via_export_package(artifact_id=artifact_id, artifact_slug=artifact_slug)
            if resolved:
                if str(resolved.get("_resolution_error") or "") == "artifact_slug_ambiguous":
                    return self._slug_ambiguity_error(
                        artifact_slug=str(resolved.get("artifact_slug") or artifact_slug or ""),
                        matches=resolved.get("matches") if isinstance(resolved.get("matches"), list) else [],
                    )
                files = resolved.get("files") if isinstance(resolved.get("files"), dict) else {}
                index_rows = build_source_index(files, include_line_counts=bool(include_line_counts))
                if max_depth is not None:
                    max_depth_int = max(1, int(max_depth))
                    index_rows = [
                        row
                        for row in index_rows
                        if isinstance(row, dict) and len(str(row.get("path") or "").split("/")) <= max_depth_int
                    ]
                if max_files is not None:
                    index_rows = index_rows[: max(1, int(max_files))]
                tree = build_hierarchical_tree(index_rows)
                return {
                    "ok": True,
                    "status_code": 200,
                    "method": "GET",
                    "path": "/api/v1/artifacts/source-tree",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "artifact": {
                            "id": str(resolved.get("artifact_id") or artifact_id or ""),
                            "slug": str(resolved.get("artifact_slug") or artifact_slug or ""),
                        },
                        "source_mode": str(resolved.get("source_mode") or "packaged_fallback"),
                        "source_origin": str(resolved.get("source_origin") or "packaged_fallback"),
                        "resolution_branch": str(resolved.get("resolution_branch") or "packaged_fallback"),
                        "resolution_details": (
                            resolved.get("resolution_details")
                            if isinstance(resolved.get("resolution_details"), dict)
                            else {}
                        ),
                        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
                        "resolved_source_roots": list(resolved.get("resolved_source_roots") or []),
                        "warnings": list(resolved.get("warnings") or []),
                        "file_count": len(index_rows),
                        "files": index_rows if bool(include_files) else [],
                        "tree": tree,
                    },
                }
        return self._with_artifact_not_found_hint(result, artifact_id=artifact_id, artifact_slug=artifact_slug)

    def read_artifact_source_file(
        self,
        *,
        path: str,
        artifact_slug: str = "",
        artifact_id: str = "",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "path": str(path or ""),
        }
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if start_line is not None:
            params["start_line"] = int(start_line)
        if end_line is not None:
            params["end_line"] = int(end_line)
        result = self._request_with_fallback_paths(
            method="GET",
            paths=_ARTIFACT_SOURCE_FILE_PATHS,
            params=params,
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and self._should_source_fallback(int(result.get("status_code") or 0)):
            resolved = self._artifact_files_via_export_package(artifact_id=artifact_id, artifact_slug=artifact_slug)
            if resolved:
                if str(resolved.get("_resolution_error") or "") == "artifact_slug_ambiguous":
                    return self._slug_ambiguity_error(
                        artifact_slug=str(resolved.get("artifact_slug") or artifact_slug or ""),
                        matches=resolved.get("matches") if isinstance(resolved.get("matches"), list) else [],
                    )
                files = resolved.get("files") if isinstance(resolved.get("files"), dict) else {}
                try:
                    payload = read_file_chunk(
                        files=files,
                        path=path,
                        start_line=start_line,
                        end_line=end_line,
                    )
                except FilePathNotFoundError as exc:
                    response: Dict[str, Any] = {"error": "file not found"}
                    if exc.candidate_paths:
                        response["candidate_paths"] = exc.candidate_paths
                    return {
                        "ok": False,
                        "status_code": 404,
                        "method": "GET",
                        "path": "/api/v1/artifacts/source-file",
                        "base_url": str(self._config.control_api_base_url).rstrip("/"),
                        "response": response,
                    }
                except (KeyError, ValueError) as exc:
                    return {
                        "ok": False,
                        "status_code": 404 if isinstance(exc, KeyError) else 400,
                        "method": "GET",
                        "path": "/api/v1/artifacts/source-file",
                        "base_url": str(self._config.control_api_base_url).rstrip("/"),
                        "response": {"error": str(exc)},
                    }
                return {
                    "ok": True,
                    "status_code": 200,
                    "method": "GET",
                    "path": "/api/v1/artifacts/source-file",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "artifact": {
                            "id": str(resolved.get("artifact_id") or artifact_id or ""),
                            "slug": str(resolved.get("artifact_slug") or artifact_slug or ""),
                        },
                        "source_mode": str(resolved.get("source_mode") or "packaged_fallback"),
                        "source_origin": str(resolved.get("source_origin") or "packaged_fallback"),
                        "resolution_branch": str(resolved.get("resolution_branch") or "packaged_fallback"),
                        "resolution_details": (
                            resolved.get("resolution_details")
                            if isinstance(resolved.get("resolution_details"), dict)
                            else {}
                        ),
                        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
                        "resolved_source_roots": list(resolved.get("resolved_source_roots") or []),
                        "warnings": list(resolved.get("warnings") or []),
                        **payload,
                    },
                }
        return self._with_artifact_not_found_hint(result, artifact_id=artifact_id, artifact_slug=artifact_slug)

    def search_artifact_source(
        self,
        *,
        query: str,
        artifact_slug: str = "",
        artifact_id: str = "",
        path_glob: str = "",
        file_extensions: str = "",
        regex: bool = False,
        case_sensitive: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "query": str(query or ""),
            "regex": bool(regex),
            "case_sensitive": bool(case_sensitive),
            "limit": int(limit),
        }
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if str(path_glob or "").strip():
            params["path_glob"] = str(path_glob).strip()
        if str(file_extensions or "").strip():
            params["file_extensions"] = str(file_extensions).strip()
        result = self._request_with_fallback_paths(
            method="GET",
            paths=_ARTIFACT_SOURCE_SEARCH_PATHS,
            params=params,
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and self._should_source_fallback(int(result.get("status_code") or 0)):
            resolved = self._artifact_files_via_export_package(artifact_id=artifact_id, artifact_slug=artifact_slug)
            if resolved:
                if str(resolved.get("_resolution_error") or "") == "artifact_slug_ambiguous":
                    return self._slug_ambiguity_error(
                        artifact_slug=str(resolved.get("artifact_slug") or artifact_slug or ""),
                        matches=resolved.get("matches") if isinstance(resolved.get("matches"), list) else [],
                    )
                files = resolved.get("files") if isinstance(resolved.get("files"), dict) else {}
                extensions = [part.strip() for part in str(file_extensions or "").split(",") if part.strip()]
                try:
                    payload = search_files(
                        files=files,
                        query=str(query or ""),
                        path_glob=str(path_glob or "") or None,
                        file_extensions=extensions or None,
                        regex=bool(regex),
                        case_sensitive=bool(case_sensitive),
                        limit=int(limit),
                    )
                except ValueError as exc:
                    return {
                        "ok": False,
                        "status_code": 400,
                        "method": "GET",
                        "path": "/api/v1/artifacts/source-search",
                        "base_url": str(self._config.control_api_base_url).rstrip("/"),
                        "response": {"error": str(exc)},
                    }
                return {
                    "ok": True,
                    "status_code": 200,
                    "method": "GET",
                    "path": "/api/v1/artifacts/source-search",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "artifact": {
                            "id": str(resolved.get("artifact_id") or artifact_id or ""),
                            "slug": str(resolved.get("artifact_slug") or artifact_slug or ""),
                        },
                        "source_mode": str(resolved.get("source_mode") or "packaged_fallback"),
                        "source_origin": str(resolved.get("source_origin") or "packaged_fallback"),
                        "resolution_branch": str(resolved.get("resolution_branch") or "packaged_fallback"),
                        "resolution_details": (
                            resolved.get("resolution_details")
                            if isinstance(resolved.get("resolution_details"), dict)
                            else {}
                        ),
                        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
                        "resolved_source_roots": list(resolved.get("resolved_source_roots") or []),
                        "warnings": list(resolved.get("warnings") or []),
                        **payload,
                    },
                }
        return self._with_artifact_not_found_hint(result, artifact_id=artifact_id, artifact_slug=artifact_slug)

    def analyze_artifact_codebase(
        self,
        *,
        artifact_slug: str = "",
        artifact_id: str = "",
        mode: str = "general",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"mode": str(mode or "general").strip().lower() or "general"}
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        result = self._request_with_fallback_paths(
            method="GET",
            paths=_ARTIFACT_ANALYZE_CODEBASE_PATHS,
            params=params,
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and self._should_source_fallback(int(result.get("status_code") or 0)):
            resolved = self._artifact_files_via_export_package(artifact_id=artifact_id, artifact_slug=artifact_slug)
            if resolved:
                if str(resolved.get("_resolution_error") or "") == "artifact_slug_ambiguous":
                    return self._slug_ambiguity_error(
                        artifact_slug=str(resolved.get("artifact_slug") or artifact_slug or ""),
                        matches=resolved.get("matches") if isinstance(resolved.get("matches"), list) else [],
                    )
                files = resolved.get("files") if isinstance(resolved.get("files"), dict) else {}
                payload = analyze_codebase(files, mode=params["mode"])
                return {
                    "ok": True,
                    "status_code": 200,
                    "method": "GET",
                    "path": "/api/v1/artifacts/analyze-codebase",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "artifact": {
                            "id": str(resolved.get("artifact_id") or artifact_id or ""),
                            "slug": str(resolved.get("artifact_slug") or artifact_slug or ""),
                        },
                        "source_mode": str(resolved.get("source_mode") or "packaged_fallback"),
                        "source_origin": str(resolved.get("source_origin") or "packaged_fallback"),
                        "resolution_branch": str(resolved.get("resolution_branch") or "packaged_fallback"),
                        "resolution_details": (
                            resolved.get("resolution_details")
                            if isinstance(resolved.get("resolution_details"), dict)
                            else {}
                        ),
                        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
                        "resolved_source_roots": list(resolved.get("resolved_source_roots") or []),
                        "warnings": list(resolved.get("warnings") or []),
                        **payload,
                    },
                }
        return self._with_artifact_not_found_hint(result, artifact_id=artifact_id, artifact_slug=artifact_slug)

    def analyze_python_api_artifact(
        self,
        *,
        artifact_slug: str = "",
        artifact_id: str = "",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        result = self._request_with_fallback_paths(
            method="GET",
            paths=_ARTIFACT_ANALYZE_PYTHON_API_PATHS,
            params=params,
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and self._should_source_fallback(int(result.get("status_code") or 0)):
            resolved = self._artifact_files_via_export_package(artifact_id=artifact_id, artifact_slug=artifact_slug)
            if resolved:
                if str(resolved.get("_resolution_error") or "") == "artifact_slug_ambiguous":
                    return self._slug_ambiguity_error(
                        artifact_slug=str(resolved.get("artifact_slug") or artifact_slug or ""),
                        matches=resolved.get("matches") if isinstance(resolved.get("matches"), list) else [],
                    )
                files = resolved.get("files") if isinstance(resolved.get("files"), dict) else {}
                payload = analyze_codebase(files, mode="python_api")
                return {
                    "ok": True,
                    "status_code": 200,
                    "method": "GET",
                    "path": "/api/v1/artifacts/analyze-python-api",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "artifact": {
                            "id": str(resolved.get("artifact_id") or artifact_id or ""),
                            "slug": str(resolved.get("artifact_slug") or artifact_slug or ""),
                        },
                        "source_mode": str(resolved.get("source_mode") or "packaged_fallback"),
                        "source_origin": str(resolved.get("source_origin") or "packaged_fallback"),
                        "resolution_branch": str(resolved.get("resolution_branch") or "packaged_fallback"),
                        "resolution_details": (
                            resolved.get("resolution_details")
                            if isinstance(resolved.get("resolution_details"), dict)
                            else {}
                        ),
                        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
                        "resolved_source_roots": list(resolved.get("resolved_source_roots") or []),
                        "warnings": list(resolved.get("warnings") or []),
                        **payload,
                    },
                }
        return self._with_artifact_not_found_hint(result, artifact_id=artifact_id, artifact_slug=artifact_slug)

    def get_artifact_module_metrics(
        self,
        *,
        artifact_slug: str = "",
        artifact_id: str = "",
        top_n: int = 200,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"top_n": int(top_n)}
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        result = self._request_with_fallback_paths(
            method="GET",
            paths=_ARTIFACT_MODULE_METRICS_PATHS,
            params=params,
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and self._should_source_fallback(int(result.get("status_code") or 0)):
            resolved = self._artifact_files_via_export_package(artifact_id=artifact_id, artifact_slug=artifact_slug)
            if resolved:
                if str(resolved.get("_resolution_error") or "") == "artifact_slug_ambiguous":
                    return self._slug_ambiguity_error(
                        artifact_slug=str(resolved.get("artifact_slug") or artifact_slug or ""),
                        matches=resolved.get("matches") if isinstance(resolved.get("matches"), list) else [],
                    )
                files = resolved.get("files") if isinstance(resolved.get("files"), dict) else {}
                metrics = compute_module_metrics(files)[: max(1, int(top_n))]
                return {
                    "ok": True,
                    "status_code": 200,
                    "method": "GET",
                    "path": "/api/v1/artifacts/module-metrics",
                    "base_url": str(self._config.control_api_base_url).rstrip("/"),
                    "response": {
                        "artifact": {
                            "id": str(resolved.get("artifact_id") or artifact_id or ""),
                            "slug": str(resolved.get("artifact_slug") or artifact_slug or ""),
                        },
                        "source_mode": str(resolved.get("source_mode") or "packaged_fallback"),
                        "source_origin": str(resolved.get("source_origin") or "packaged_fallback"),
                        "resolution_branch": str(resolved.get("resolution_branch") or "packaged_fallback"),
                        "resolution_details": (
                            resolved.get("resolution_details")
                            if isinstance(resolved.get("resolution_details"), dict)
                            else {}
                        ),
                        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
                        "resolved_source_roots": list(resolved.get("resolved_source_roots") or []),
                        "warnings": list(resolved.get("warnings") or []),
                        "metrics": metrics,
                        "count": len(metrics),
                    },
                }
        return self._with_artifact_not_found_hint(result, artifact_id=artifact_id, artifact_slug=artifact_slug)

    def list_deployment_providers(self) -> Dict[str, Any]:
        return self._request(method="GET", path="/xyn/api/deployment-providers")

    def get_provider_capabilities(self, *, provider_key: str) -> Dict[str, Any]:
        return self._request(method="GET", path=f"/xyn/api/deployment-providers/{provider_key}")

    def create_release_target(self, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request(
            method="POST",
            path="/xyn/api/release-targets",
            json_payload=dict(payload or {}),
        )

    def list_blueprints(self) -> Dict[str, Any]:
        return self._request(method="GET", path="/xyn/api/blueprints")

    def get_blueprint(self, *, blueprint_id: str) -> Dict[str, Any]:
        return self._request(method="GET", path=f"/xyn/api/blueprints/{blueprint_id}")

    def create_blueprint(self, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request(
            method="POST",
            path="/xyn/api/blueprints",
            json_payload=dict(payload or {}),
        )

    def create_change_effort(self, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_payload = dict(payload or {})
        workspace_intent = self._workspace_intent_for_artifact_scope(
            artifact_id=str(request_payload.get("artifact_id") or "").strip(),
            artifact_slug=str(request_payload.get("artifact_slug") or "").strip(),
        )
        resolved = self._resolve_workspace_for_request(
            explicit_workspace_id=str(request_payload.get("workspace_id") or "").strip(),
            require_workspace=True,
            intent=workspace_intent,
        )
        if not resolved.get("ok"):
            return self._workspace_resolution_error_result(
                method="POST",
                path="/api/v1/change-efforts",
                error=str(resolved.get("error") or "workspace_required"),
                detail=str(resolved.get("detail") or ""),
                candidate_workspaces=resolved.get("candidate_workspaces") if isinstance(resolved.get("candidate_workspaces"), list) else [],
                status_code=int(resolved.get("status_code") or 400),
            )
        resolved_workspace_id = str(resolved.get("workspace_id") or "").strip()
        request_payload["workspace_id"] = resolved_workspace_id
        result = self._request_with_fallback_paths(
            method="POST",
            paths=["/api/v1/change-efforts"],
            json_payload=request_payload,
            base_urls=self._code_api_base_urls(),
        )
        normalized = self._normalize_change_effort_result(
            result,
            default_next_allowed_actions=[
                "get_change_effort",
                "resolve_effort_source",
                "allocate_effort_branch",
            ],
        )
        if isinstance(normalized.get("response"), dict):
            normalized["response"]["resolved_workspace_id"] = resolved_workspace_id
        return normalized

    def get_change_effort(self, *, effort_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/api/v1/change-efforts/{effort_id}"],
            base_urls=self._code_api_base_urls(),
        )
        return self._normalize_change_effort_result(
            result,
            default_next_allowed_actions=[
                "resolve_effort_source",
                "allocate_effort_branch",
                "allocate_effort_worktree",
                "get_effort_git_status",
                "get_effort_changed_files",
            ],
        )

    def list_change_efforts(
        self,
        *,
        workspace_id: str = "",
        artifact_slug: str = "",
        status: str = "",
        limit: int = 100,
    ) -> Dict[str, Any]:
        resolved = self._resolve_workspace_for_request(
            explicit_workspace_id=workspace_id,
            require_workspace=True,
            intent="user",
        )
        if not resolved.get("ok"):
            return self._workspace_resolution_error_result(
                method="GET",
                path="/api/v1/change-efforts",
                error=str(resolved.get("error") or "workspace_required"),
                detail=str(resolved.get("detail") or ""),
                candidate_workspaces=resolved.get("candidate_workspaces") if isinstance(resolved.get("candidate_workspaces"), list) else [],
                status_code=int(resolved.get("status_code") or 400),
            )
        resolved_workspace_id = str(resolved.get("workspace_id") or "").strip()
        params: Dict[str, Any] = {"limit": int(limit), "workspace_id": resolved_workspace_id}
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(status or "").strip():
            params["status"] = str(status).strip()
        result = self._request_with_fallback_paths(
            method="GET",
            paths=["/api/v1/change-efforts"],
            params=params,
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok"):
            if int(result.get("status_code") or 0) in {404, 405}:
                result["response"] = {
                    "error": "not_supported",
                    "detail": "List change-efforts endpoint is not available on this backend.",
                    "blocked_reason": "not_supported",
                }
            return result
        body = result.get("response")
        rows = body if isinstance(body, list) else []
        if isinstance(body, dict):
            if isinstance(body.get("change_efforts"), list):
                rows = body.get("change_efforts") or []
            elif isinstance(body.get("items"), list):
                rows = body.get("items") or []
        normalized_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_result = {"ok": True, "status_code": 200, "response": {"change_effort": row}}
            normalized_rows.append(self._normalize_change_effort_result(row_result).get("response"))
        result["response"] = {
            "change_efforts": normalized_rows,
            "count": len(normalized_rows),
            "resolved_workspace_id": resolved_workspace_id,
        }
        return result

    def resolve_effort_source(self, *, effort_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="POST",
            paths=[f"/api/v1/change-efforts/{effort_id}/resolve-source"],
            json_payload={},
            base_urls=self._code_api_base_urls(),
        )
        return self._normalize_change_effort_result(
            result,
            default_next_allowed_actions=[
                "allocate_effort_branch",
                "allocate_effort_worktree",
                "inspect_decomposition_guardrails",
            ],
        )

    def allocate_effort_branch(self, *, effort_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="POST",
            paths=[f"/api/v1/change-efforts/{effort_id}/allocate-branch"],
            json_payload=dict(payload or {}),
            base_urls=self._code_api_base_urls(),
        )
        return self._normalize_change_effort_result(
            result,
            default_next_allowed_actions=[
                "allocate_effort_worktree",
                "get_effort_git_status",
                "get_effort_diff",
            ],
        )

    def allocate_effort_worktree(self, *, effort_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="POST",
            paths=[f"/api/v1/change-efforts/{effort_id}/allocate-worktree"],
            json_payload=dict(payload or {}),
            base_urls=self._code_api_base_urls(),
        )
        return self._normalize_change_effort_result(
            result,
            default_next_allowed_actions=[
                "get_effort_git_status",
                "get_effort_changed_files",
                "get_effort_diff",
                "promote_change_effort",
            ],
        )

    def promote_change_effort(self, *, effort_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="POST",
            paths=[f"/api/v1/change-efforts/{effort_id}/promote"],
            json_payload=dict(payload or {}),
            base_urls=self._code_api_base_urls(),
        )
        return self._normalize_change_effort_result(
            result,
            default_next_allowed_actions=[
                "get_effort_preview_binding",
                "get_application_change_session_preview_status",
                "declare_release",
            ],
        )

    def get_effort_diff(self, *, effort_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/api/v1/change-efforts/{effort_id}/diff"],
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            base = self.get_change_effort(effort_id=effort_id)
            if not base.get("ok"):
                return base
            body = base.get("response") if isinstance(base.get("response"), dict) else {}
            result = {
                "ok": True,
                "status_code": 200,
                "method": "GET",
                "path": f"/api/v1/change-efforts/{effort_id}/diff",
                "base_url": str(self._config.control_api_base_url).rstrip("/"),
                "response": {
                    "effort_id": str(body.get("effort_id") or effort_id),
                    "summary": "Backend diff endpoint unavailable; returning metadata-backed placeholder.",
                    "diff": "",
                    "changed_files": body.get("changed_files") if isinstance(body.get("changed_files"), list) else [],
                    "blocked_reason": "not_supported",
                },
            }
        return result

    def get_effort_changed_files(self, *, effort_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/api/v1/change-efforts/{effort_id}/changed-files"],
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            base = self.get_change_effort(effort_id=effort_id)
            if not base.get("ok"):
                return base
            body = base.get("response") if isinstance(base.get("response"), dict) else {}
            return {
                "ok": True,
                "status_code": 200,
                "method": "GET",
                "path": f"/api/v1/change-efforts/{effort_id}/changed-files",
                "base_url": str(self._config.control_api_base_url).rstrip("/"),
                "response": {
                    "effort_id": str(body.get("effort_id") or effort_id),
                    "changed_files": body.get("changed_files") if isinstance(body.get("changed_files"), list) else [],
                    "count": len(body.get("changed_files") or []) if isinstance(body.get("changed_files"), list) else 0,
                    "blocked_reason": "not_supported",
                },
            }
        body = result.get("response")
        files = body if isinstance(body, list) else []
        if isinstance(body, dict):
            if isinstance(body.get("changed_files"), list):
                files = body.get("changed_files") or []
            elif isinstance(body.get("items"), list):
                files = body.get("items") or []
        normalized_files = [str(item).strip() for item in files if str(item).strip()]
        result["response"] = {"effort_id": effort_id, "changed_files": normalized_files, "count": len(normalized_files)}
        return result

    def get_effort_git_status(self, *, effort_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/api/v1/change-efforts/{effort_id}/git-status"],
            base_urls=self._code_api_base_urls(),
        )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            base = self.get_change_effort(effort_id=effort_id)
            if not base.get("ok"):
                return base
            body = base.get("response") if isinstance(base.get("response"), dict) else {}
            return {
                "ok": True,
                "status_code": 200,
                "method": "GET",
                "path": f"/api/v1/change-efforts/{effort_id}/git-status",
                "base_url": str(self._config.control_api_base_url).rstrip("/"),
                "response": {
                    "effort_id": str(body.get("effort_id") or effort_id),
                    "branch_name": str(body.get("branch_name") or ""),
                    "worktree_path": str(body.get("worktree_path") or ""),
                    "clean": False,
                    "status_summary": "unknown",
                    "blocked_reason": "not_supported",
                },
            }
        return result

    def get_effort_preview_binding(self, *, effort_id: str) -> Dict[str, Any]:
        effort = self.get_change_effort(effort_id=effort_id)
        if not effort.get("ok"):
            return effort
        body = effort.get("response") if isinstance(effort.get("response"), dict) else {}
        linked = body.get("linked_change_session") if isinstance(body.get("linked_change_session"), dict) else {}
        application_id = str(linked.get("application_id") or "")
        session_id = str(linked.get("session_id") or "")
        if not application_id or not session_id:
            return {
                "ok": True,
                "status_code": 200,
                "method": "GET",
                "path": f"/api/v1/change-efforts/{effort_id}/preview-binding",
                "base_url": str(self._config.control_api_base_url).rstrip("/"),
                "response": {
                    "effort_id": effort_id,
                    "linked_change_session": linked,
                    "preview_binding": {},
                    "blocked_reason": "not_linked_to_change_session",
                },
            }
        preview = self.get_application_change_session_preview_status(application_id=application_id, session_id=session_id)
        preview_body = preview.get("response") if isinstance(preview.get("response"), dict) else {}
        return {
            "ok": bool(preview.get("ok")),
            "status_code": int(preview.get("status_code") or 200),
            "method": "GET",
            "path": f"/api/v1/change-efforts/{effort_id}/preview-binding",
            "base_url": str(self._config.control_api_base_url).rstrip("/"),
            "response": {
                "effort_id": effort_id,
                "linked_change_session": linked,
                "preview_binding": preview_body.get("preview") if isinstance(preview_body.get("preview"), dict) else {},
                "current_status": str(preview_body.get("current_status") or ""),
                "blocked_reason": str(preview_body.get("blocked_reason") or ""),
            },
        }

    def declare_release(self, *, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        request_payload = dict(payload or {})
        workspace_intent = self._workspace_intent_for_artifact_scope(
            artifact_slug=str(request_payload.get("artifact_slug") or "").strip(),
        )
        resolved = self._resolve_workspace_for_request(
            explicit_workspace_id=str(request_payload.get("workspace_id") or "").strip(),
            require_workspace=True,
            intent=workspace_intent,
        )
        if not resolved.get("ok"):
            return self._workspace_resolution_error_result(
                method="POST",
                path="/api/v1/releases/declare",
                error=str(resolved.get("error") or "workspace_required"),
                detail=str(resolved.get("detail") or ""),
                candidate_workspaces=resolved.get("candidate_workspaces") if isinstance(resolved.get("candidate_workspaces"), list) else [],
                status_code=int(resolved.get("status_code") or 400),
            )
        resolved_workspace_id = str(resolved.get("workspace_id") or "").strip()
        request_payload["workspace_id"] = resolved_workspace_id
        result = self._request_with_fallback_paths(
            method="POST",
            paths=["/api/v1/releases/declare"],
            json_payload=request_payload,
            base_urls=self._code_api_base_urls(),
        )
        if isinstance(result.get("response"), dict):
            result["response"]["resolved_workspace_id"] = resolved_workspace_id
        return result

    def get_artifact_provenance(self, *, artifact_slug: str, workspace_id: str = "") -> Dict[str, Any]:
        resolved = self._resolve_workspace_for_request(
            explicit_workspace_id=workspace_id,
            require_workspace=True,
            intent=self._workspace_intent_for_artifact_scope(artifact_slug=artifact_slug),
        )
        if not resolved.get("ok"):
            return self._workspace_resolution_error_result(
                method="GET",
                path=f"/api/v1/provenance/{artifact_slug}",
                error=str(resolved.get("error") or "workspace_required"),
                detail=str(resolved.get("detail") or ""),
                candidate_workspaces=resolved.get("candidate_workspaces") if isinstance(resolved.get("candidate_workspaces"), list) else [],
                status_code=int(resolved.get("status_code") or 400),
            )
        resolved_workspace_id = str(resolved.get("workspace_id") or "").strip()
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/api/v1/provenance/{artifact_slug}"],
            params={"workspace_id": resolved_workspace_id},
            base_urls=self._code_api_base_urls(),
        )
        if isinstance(result.get("response"), dict):
            result["response"]["resolved_workspace_id"] = resolved_workspace_id
        return result
