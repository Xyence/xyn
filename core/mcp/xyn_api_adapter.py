from __future__ import annotations

import os
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
            default_workspace_id=str(os.getenv("XYN_MCP_WORKSPACE_ID", "")).strip()
            or str(os.getenv("XYN_WORKSPACE_ID", "")).strip(),
        )


class XynApiAdapter:
    """Thin HTTP adapter over existing Xyn API/control/evidence endpoints."""

    def __init__(self, config: XynApiAdapterConfig):
        self._config = config

    @property
    def config(self) -> XynApiAdapterConfig:
        return self._config

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
    ) -> Dict[str, Any]:
        resolved_base_url = str(base_url or self._config.control_api_base_url).rstrip("/")
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
            return {
                "ok": False,
                "status_code": 503,
                "method": method.upper(),
                "path": path,
                "base_url": resolved_base_url,
                "response": {"error": "upstream_unreachable", "detail": str(exc)},
            }
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
                return redirect_error
            try:
                retry_response = _do_request(prefer_request_bearer=False)
            except httpx.RequestError:
                return redirect_error
            retry_redirect = self._api_redirect_as_json_error(
                status_code=int(retry_response.status_code),
                path=path,
                response=retry_response,
            )
            if isinstance(retry_redirect, dict):
                retry_redirect["method"] = method.upper()
                retry_redirect["base_url"] = resolved_base_url
                return retry_redirect
            response = retry_response
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
            return {
                "ok": bool(200 <= retry_response.status_code < 300),
                "status_code": int(retry_response.status_code),
                "method": method.upper(),
                "path": path,
                "base_url": resolved_base_url,
                "response": retry_body if isinstance(retry_body, (dict, list)) else {"value": retry_body},
            }
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
    ) -> Dict[str, Any]:
        last_result: Dict[str, Any] = {"ok": False, "status_code": 404, "response": {"error": "not_found"}}
        deduped_base_urls: list[str] = []
        for candidate in (base_urls or [self._config.control_api_base_url]):
            base = str(candidate or "").strip()
            if not base or base in deduped_base_urls:
                continue
            deduped_base_urls.append(base)
        if not deduped_base_urls:
            deduped_base_urls = [self._config.control_api_base_url]
        for base_url in deduped_base_urls:
            for path in paths:
                result = self._request(
                    method=method,
                    path=path,
                    json_payload=json_payload,
                    params=params,
                    base_url=base_url,
                )
                last_result = result
                if bool(result.get("ok")):
                    return result
                code = int(result.get("status_code") or 0)
                # Continue searching across endpoint/base-url variants for compatibility.
                blocked_reason = str(
                    ((result.get("response") or {}).get("blocked_reason") if isinstance(result.get("response"), dict) else "")
                    or ""
                ).strip()
                if code in {400, 401, 403, 404, 405, 503} or blocked_reason == "interactive_login_redirect":
                    continue
                return result
        return last_result

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

        next_allowed_actions = body.get("next_allowed_actions") if isinstance(body.get("next_allowed_actions"), list) else []
        if not next_allowed_actions:
            next_allowed_actions = list(default_next_allowed_actions or [])

        normalized = {
            "application_id": str(application_id or body.get("application_id") or ""),
            "session_id": str(session_id or body.get("session_id") or ""),
            "current_status": current_status,
            "next_allowed_actions": next_allowed_actions,
            "blocked_reason": blocked_reason,
            "preview_urls": XynApiAdapter._extract_preview_urls(body),
            "preview": XynApiAdapter._extract_preview_compact(body),
            "commit_shas": XynApiAdapter._extract_commit_shas(body),
            "changed_files": XynApiAdapter._extract_changed_files(body),
            "promotion_evidence_ids": XynApiAdapter._extract_promotion_evidence_ids(body),
            "decomposition_campaign": XynApiAdapter._extract_decomposition_campaign(body),
            "guardrails": XynApiAdapter._extract_decomposition_guardrails(body),
            "raw": response,
        }
        result["response"] = normalized
        return result

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
        blocked_reason = ""
        if not result.get("ok"):
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

    def _list_accessible_workspaces(self) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=["/xyn/api/workspaces"],
            base_urls=[self._config.control_api_base_url],
        )
        workspace_rows: list[dict[str, str]] = []
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
                }
            )
        return {"ok": True, "result": result, "workspaces": workspace_rows}

    @staticmethod
    def _workspace_resolution_error_payload(
        *,
        error: str,
        detail: str,
        candidate_workspaces: list[dict[str, str]],
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
        candidate_workspaces: list[dict[str, str]],
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
    ) -> Dict[str, Any]:
        explicit = str(explicit_workspace_id or "").strip()
        if explicit:
            return {"ok": True, "workspace_id": explicit, "source": "explicit", "candidate_workspaces": []}

        configured_default = str(self._config.default_workspace_id or "").strip()
        if configured_default:
            accessible = self._list_accessible_workspaces()
            candidate_workspaces = accessible.get("workspaces") if isinstance(accessible.get("workspaces"), list) else []
            accessible_ids = {
                str(row.get("id") or "").strip()
                for row in candidate_workspaces
                if isinstance(row, dict) and str(row.get("id") or "").strip()
            }
            if accessible.get("ok") and accessible_ids and configured_default not in accessible_ids:
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

        accessible = self._list_accessible_workspaces()
        candidate_workspaces = accessible.get("workspaces") if isinstance(accessible.get("workspaces"), list) else []
        if not accessible.get("ok"):
            return {
                "ok": False,
                "status_code": 400,
                "error": "workspace_required",
                "detail": "workspace_id is required and no default workspace could be resolved.",
                "candidate_workspaces": candidate_workspaces,
            }

        if len(candidate_workspaces) == 1:
            return {
                "ok": True,
                "workspace_id": str(candidate_workspaces[0].get("id") or "").strip(),
                "source": "single_accessible_workspace",
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
        resolved = self._resolve_workspace_for_request(explicit_workspace_id=workspace_id, require_workspace=True)
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
        result = self._request_with_fallback_paths(
            method="GET",
            paths=["/xyn/api/applications"],
            base_urls=[self._config.control_api_base_url],
            params=params,
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("applications") if isinstance(body.get("applications"), list) else (body.get("items") if isinstance(body.get("items"), list) else [])
        normalized = [self._application_discovery_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {
            "applications": normalized,
            "count": len(normalized),
            "resolved_workspace_id": resolved_workspace_id,
        }
        return result

    def get_application(self, *, application_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/xyn/api/applications/{application_id}"],
            base_urls=[self._config.control_api_base_url],
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {"application": self._application_discovery_row(body)}
        return result

    def list_application_change_sessions(self, *, application_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/xyn/api/applications/{application_id}/change-sessions"],
            base_urls=[self._config.control_api_base_url],
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("change_sessions") if isinstance(body.get("change_sessions"), list) else (body.get("items") if isinstance(body.get("items"), list) else [])
        normalized = [self._change_session_discovery_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {
            "application_id": str(application_id),
            "change_sessions": normalized,
            "count": len(normalized),
        }
        return result

    def get_application_change_session(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        result = self._request_with_fallback_paths(
            method="GET",
            paths=[f"/xyn/api/applications/{application_id}/change-sessions/{session_id}"],
            base_urls=[self._config.control_api_base_url],
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {
            "change_session": self._change_session_discovery_row(body),
            "application_id": str(application_id),
        }
        return result

    def create_application_change_session(self, *, application_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = self._request(
            method="POST",
            path=f"/xyn/api/applications/{application_id}/change-sessions",
            json_payload=dict(payload or {}),
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

    def create_decomposition_campaign(
        self,
        *,
        application_id: str,
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
        result = self.create_application_change_session(application_id=application_id, payload=request_payload)
        if isinstance(result.get("response"), dict):
            result["response"]["decomposition_campaign"] = dict(request_payload["decomposition_campaign"])
        return result

    def get_decomposition_campaign(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        result = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
        normalized = self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
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
        normalized["response"] = {
            "application_id": str(application_id),
            "session_id": str(session_id),
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

    def inspect_decomposition_guardrails(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        result = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
        normalized = self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            default_next_allowed_actions=[
                "stage_apply_application_change_session",
                "prepare_preview_application_change_session",
                "validate_application_change_session",
                "commit_application_change_session",
            ],
        )
        body = normalized.get("response") if isinstance(normalized.get("response"), dict) else {}
        guardrails = body.get("guardrails") if isinstance(body.get("guardrails"), dict) else {}
        normalized["response"] = {
            "application_id": str(application_id),
            "session_id": str(session_id),
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
        application_id: str,
        session_id: str,
        artifact_id: str = "",
        artifact_slug: str = "",
        top_n: int = 50,
    ) -> Dict[str, Any]:
        session_status = self.get_decomposition_campaign(application_id=application_id, session_id=session_id)
        body = session_status.get("response") if isinstance(session_status.get("response"), dict) else {}
        raw = body.get("raw") if isinstance(body.get("raw"), dict) else {}

        resolved_artifact_id = str(artifact_id or "").strip()
        resolved_artifact_slug = str(artifact_slug or "").strip()
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
            "path": f"/xyn/api/applications/{application_id}/change-sessions/{session_id}/decomposition-observability",
            "base_url": str(self._config.control_api_base_url).rstrip("/"),
            "response": {
                "application_id": str(application_id),
                "session_id": str(session_id),
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

    def get_application_change_session_plan(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        # Canonical workflow route is POST-only in xyn-api.
        result = self._request(
            method="POST",
            path=f"/xyn/api/applications/{application_id}/change-sessions/{session_id}/plan",
            json_payload={},
        )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            # Back-compat for older deployments that exposed GET /plan.
            result = self._request(
                method="GET",
                path=f"/xyn/api/applications/{application_id}/change-sessions/{session_id}/plan",
            )
        if not result.get("ok") and int(result.get("status_code") or 0) in {404, 405}:
            # Compatibility fallback to canonical control inspection when explicit /plan route is unavailable.
            result = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            default_next_allowed_actions=[
                "stage_apply_application_change_session",
                "prepare_preview_application_change_session",
                "validate_application_change_session",
            ],
        )

    def _run_application_change_session_operation(
        self,
        *,
        application_id: str,
        session_id: str,
        operation: str,
        payload: Optional[Dict[str, Any]] = None,
        next_allowed_actions: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        result = self.run_change_session_control_action(
            application_id=application_id,
            session_id=session_id,
            operation=operation,
            action_payload=payload,
        )
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            default_next_allowed_actions=next_allowed_actions,
        )

    def stage_apply_application_change_session(
        self, *, application_id: str, session_id: str, payload: Optional[Dict[str, Any]] = None
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
        self, *, application_id: str, session_id: str, payload: Optional[Dict[str, Any]] = None
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
        self, *, application_id: str, session_id: str, payload: Optional[Dict[str, Any]] = None
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
        self, *, application_id: str, session_id: str, payload: Optional[Dict[str, Any]] = None
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
        self, *, application_id: str, session_id: str, payload: Optional[Dict[str, Any]] = None
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
        self, *, application_id: str, session_id: str, payload: Optional[Dict[str, Any]] = None
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

    def get_application_change_session_commits(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        result = self._request(
            method="GET",
            path=f"/xyn/api/applications/{application_id}/change-sessions/{session_id}/commits",
        )
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            default_next_allowed_actions=[
                "promote_application_change_session",
                "rollback_application_change_session",
            ],
        )

    def get_application_change_session_promotion_evidence(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        result = self.get_change_session_promotion_evidence(application_id=application_id, session_id=session_id)
        return self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
            default_next_allowed_actions=[
                "rollback_application_change_session",
                "inspect_change_session_control",
            ],
        )

    def get_application_change_session_preview_status(self, *, application_id: str, session_id: str) -> Dict[str, Any]:
        result = self.inspect_change_session_control(application_id=application_id, session_id=session_id)
        normalized = self._normalize_change_session_result(
            result,
            application_id=application_id,
            session_id=session_id,
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
                "/runtime/runs",
                "/runs",
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
                f"/runs/{normalized_run_id}",
                f"/api/v1/runs/{normalized_run_id}",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths)
        return self._normalize_runtime_run_result(
            result,
            default_next_allowed_actions=[
                "get_runtime_run_logs",
                "get_runtime_run_artifacts",
                "get_runtime_run_commands",
                "cancel_runtime_run",
                "rerun_runtime_run",
            ],
        )

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
                f"/runs/{normalized_run_id}/steps",
                f"/api/v1/runs/{normalized_run_id}/steps",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths)
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
                f"/runs/{normalized_run_id}/artifacts",
                f"/api/v1/runs/{normalized_run_id}/artifacts",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths)
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
                f"/runs/{normalized_run_id}/steps",
                f"/api/v1/runs/{normalized_run_id}/steps",
            ]
        )
        result = self._request_with_fallback_paths(method="GET", paths=paths)
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
                f"/runs/{normalized_run_id}/cancel",
                f"/api/v1/runs/{normalized_run_id}/cancel",
            ]
        )
        result = self._request_with_fallback_paths(method="POST", paths=paths, json_payload={})
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
                f"/runs/{normalized_run_id}/retry",
                f"/api/v1/runs/{normalized_run_id}/retry",
            ]
        )
        result = self._request_with_fallback_paths(method="POST", paths=paths, json_payload={})
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
            paths=[
                "/api/v1/artifacts/source-tree",
            ],
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
            paths=[
                "/api/v1/artifacts/source-file",
            ],
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
            paths=[
                "/api/v1/artifacts/source-search",
            ],
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
            paths=[
                "/api/v1/artifacts/analyze-codebase",
            ],
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
            paths=[
                "/api/v1/artifacts/analyze-python-api",
            ],
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
            paths=[
                "/api/v1/artifacts/module-metrics",
            ],
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
        resolved = self._resolve_workspace_for_request(
            explicit_workspace_id=str(request_payload.get("workspace_id") or "").strip(),
            require_workspace=True,
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
        resolved = self._resolve_workspace_for_request(explicit_workspace_id=workspace_id, require_workspace=True)
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
        resolved = self._resolve_workspace_for_request(
            explicit_workspace_id=str(request_payload.get("workspace_id") or "").strip(),
            require_workspace=True,
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
        resolved = self._resolve_workspace_for_request(explicit_workspace_id=workspace_id, require_workspace=True)
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
