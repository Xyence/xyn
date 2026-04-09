from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

_REQUEST_BEARER_TOKEN: ContextVar[str] = ContextVar("xyn_mcp_request_bearer_token", default="")


def set_request_bearer_token(token: str) -> Token:
    return _REQUEST_BEARER_TOKEN.set(str(token or "").strip())


def reset_request_bearer_token(token: Token) -> None:
    _REQUEST_BEARER_TOKEN.reset(token)


def get_request_bearer_token() -> str:
    return _REQUEST_BEARER_TOKEN.get()


@dataclass(frozen=True)
class XynApiAdapterConfig:
    api_base_url: str
    bearer_token: str
    internal_token: str
    cookie: str
    timeout_seconds: float
    upstream_host_header: str = ""
    upstream_forwarded_proto: str = ""

    @classmethod
    def from_env(cls) -> "XynApiAdapterConfig":
        public_base_url = str(os.getenv("XYN_PUBLIC_BASE_URL", "")).strip()
        parsed_public = urlparse(public_base_url) if public_base_url else None
        derived_host = str(parsed_public.netloc or "").strip() if parsed_public else ""
        if ":" in derived_host:
            derived_host = derived_host.split(":", 1)[0].strip()
        derived_proto = str(parsed_public.scheme or "").strip() if parsed_public else ""
        return cls(
            api_base_url=str(os.getenv("XYN_MCP_XYN_API_BASE_URL", "http://localhost:8001")).strip(),
            bearer_token=str(os.getenv("XYN_MCP_XYN_API_BEARER_TOKEN", "")).strip()
            or str(os.getenv("XYN_MCP_AUTH_BEARER_TOKEN", "")).strip(),
            internal_token=str(os.getenv("XYN_MCP_INTERNAL_TOKEN", "")).strip(),
            cookie=str(os.getenv("XYN_MCP_COOKIE", "")).strip(),
            timeout_seconds=float(os.getenv("XYN_MCP_TIMEOUT_SECONDS", "30").strip() or "30"),
            upstream_host_header=str(os.getenv("XYN_MCP_UPSTREAM_HOST_HEADER", "")).strip() or derived_host,
            upstream_forwarded_proto=str(os.getenv("XYN_MCP_UPSTREAM_FORWARDED_PROTO", "")).strip() or derived_proto,
        )


class XynApiAdapter:
    """Thin HTTP adapter over existing Xyn API/control/evidence endpoints."""

    def __init__(self, config: XynApiAdapterConfig):
        self._config = config

    @property
    def config(self) -> XynApiAdapterConfig:
        return self._config

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        bearer = self._config.bearer_token or get_request_bearer_token()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        if self._config.internal_token:
            headers["X-Internal-Token"] = self._config.internal_token
        if self._config.cookie:
            headers["Cookie"] = self._config.cookie
        if self._config.upstream_host_header:
            headers["Host"] = self._config.upstream_host_header
            headers["X-Forwarded-Host"] = self._config.upstream_host_header
        if self._config.upstream_forwarded_proto:
            headers["X-Forwarded-Proto"] = self._config.upstream_forwarded_proto
        return headers

    def _request(
        self,
        *,
        method: str,
        path: str,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self._config.api_base_url.rstrip('/')}{path}"
        response = httpx.request(
            method=method.upper(),
            url=url,
            headers=self._headers(),
            json=json_payload,
            params=params,
            timeout=self._config.timeout_seconds,
        )
        body: Any
        try:
            body = response.json()
        except Exception:
            body = {"raw_text": response.text}
        return {
            "ok": bool(200 <= response.status_code < 300),
            "status_code": int(response.status_code),
            "method": method.upper(),
            "path": path,
            "response": body if isinstance(body, (dict, list)) else {"value": body},
        }

    def _request_with_fallback_paths(
        self,
        *,
        method: str,
        paths: list[str],
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        last_result: Dict[str, Any] = {"ok": False, "status_code": 404, "response": {"error": "not_found"}}
        for path in paths:
            result = self._request(method=method, path=path, json_payload=json_payload, params=params)
            last_result = result
            if bool(result.get("ok")):
                return result
            code = int(result.get("status_code") or 0)
            # If request is structurally invalid on one endpoint flavor,
            # continue to alternate path flavor for compatibility.
            if code in {400, 404, 405}:
                continue
            return result
        return last_result

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
        inferred_slug = str(
            payload.get("slug")
            or metadata.get("generated_artifact_slug")
            or payload.get("name")
            or payload.get("label")
            or ""
        )
        return {
            "id": str(payload.get("id") or ""),
            "slug": inferred_slug,
            "title": str(payload.get("title") or payload.get("name") or payload.get("label") or ""),
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

    def inspect_change_session_control(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        return self._request(
            method="GET",
            path=f"/xyn/api/applications/{application_id}/change-sessions/{session_id}/control",
        )

    def run_change_session_control_action(
        self,
        *,
        application_id: str,
        session_id: str,
        operation: str,
        action_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(action_payload or {})
        payload["operation"] = str(operation or "").strip()
        return self._request(
            method="POST",
            path=f"/xyn/api/applications/{application_id}/change-sessions/{session_id}/control/actions",
            json_payload=payload,
        )

    def get_change_session_promotion_evidence(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        return self._request(
            method="GET",
            path=f"/xyn/api/applications/{application_id}/change-sessions/{session_id}/promotion-evidence",
        )

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
        # - Prefer /xyn/api/artifacts for deployed MCP integrations.
        # - Some handlers reject offset-style params; retry same path without offset.
        # - Fall back to /api/v1/artifacts variants.
        explicit_params = {"limit": resolved_limit, "offset": resolved_offset}
        no_offset_params = {"limit": resolved_limit}
        params_variants = [explicit_params]
        if resolved_offset == 0:
            params_variants.append(no_offset_params)

        last_result: Dict[str, Any] = {"ok": False, "status_code": 404, "response": {"error": "not_found"}}
        for path in ["/xyn/api/artifacts", "/api/v1/artifacts"]:
            for params in params_variants:
                result = self._request(method="GET", path=path, params=params)
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
                if code in {400, 404, 405}:
                    continue
                return result
        result = last_result
        if not result.get("ok"):
            return result
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
        artifact_id: str = "",
        artifact_slug: str = "",
        include_line_counts: bool = True,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"include_line_counts": bool(include_line_counts)}
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        return self._request(
            method="GET",
            path="/api/v1/artifacts/source-tree",
            params=params,
        )

    def read_artifact_source_file(
        self,
        *,
        path: str,
        artifact_id: str = "",
        artifact_slug: str = "",
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "path": str(path or ""),
        }
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if start_line is not None:
            params["start_line"] = int(start_line)
        if end_line is not None:
            params["end_line"] = int(end_line)
        return self._request(method="GET", path="/api/v1/artifacts/source-file", params=params)

    def search_artifact_source(
        self,
        *,
        query: str,
        artifact_id: str = "",
        artifact_slug: str = "",
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
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        if str(path_glob or "").strip():
            params["path_glob"] = str(path_glob).strip()
        if str(file_extensions or "").strip():
            params["file_extensions"] = str(file_extensions).strip()
        return self._request(
            method="GET",
            path="/api/v1/artifacts/source-search",
            params=params,
        )

    def analyze_artifact_codebase(
        self,
        *,
        artifact_id: str = "",
        artifact_slug: str = "",
        mode: str = "general",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"mode": str(mode or "general").strip().lower() or "general"}
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        return self._request(
            method="GET",
            path="/api/v1/artifacts/analyze-codebase",
            params=params,
        )

    def analyze_python_api_artifact(
        self,
        *,
        artifact_id: str = "",
        artifact_slug: str = "",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        return self._request(
            method="GET",
            path="/api/v1/artifacts/analyze-python-api",
            params=params,
        )

    def get_artifact_module_metrics(
        self,
        *,
        artifact_id: str = "",
        artifact_slug: str = "",
        top_n: int = 200,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"top_n": int(top_n)}
        if str(artifact_id or "").strip():
            params["artifact_id"] = str(artifact_id).strip()
        if str(artifact_slug or "").strip():
            params["artifact_slug"] = str(artifact_slug).strip()
        return self._request(
            method="GET",
            path="/api/v1/artifacts/module-metrics",
            params=params,
        )

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
