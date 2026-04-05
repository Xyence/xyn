from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx


@dataclass(frozen=True)
class XynApiAdapterConfig:
    api_base_url: str
    bearer_token: str
    internal_token: str
    cookie: str
    timeout_seconds: float

    @classmethod
    def from_env(cls) -> "XynApiAdapterConfig":
        return cls(
            api_base_url=str(os.getenv("XYN_MCP_XYN_API_BASE_URL", "http://localhost:8001")).strip(),
            bearer_token=str(os.getenv("XYN_MCP_AUTH_BEARER_TOKEN", "")).strip(),
            internal_token=str(os.getenv("XYN_MCP_INTERNAL_TOKEN", "")).strip(),
            cookie=str(os.getenv("XYN_MCP_COOKIE", "")).strip(),
            timeout_seconds=float(os.getenv("XYN_MCP_TIMEOUT_SECONDS", "30").strip() or "30"),
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
        if self._config.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        if self._config.internal_token:
            headers["X-Internal-Token"] = self._config.internal_token
        if self._config.cookie:
            headers["Cookie"] = self._config.cookie
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

    @staticmethod
    def _release_target_discovery_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
        dns = payload.get("dns") if isinstance(payload.get("dns"), dict) else {}
        return {
            "id": str(payload.get("id") or ""),
            "provider": {
                "runtime_transport": str(runtime.get("transport") or ""),
                "runtime_type": str(runtime.get("type") or ""),
                "dns_provider": str(dns.get("provider") or ""),
            },
            "artifact_reference": {
                "blueprint_id": str(payload.get("blueprint_id") or ""),
            },
            "configuration_summary": {
                "name": str(payload.get("name") or ""),
                "environment": str(payload.get("environment") or ""),
                "fqdn": str(payload.get("fqdn") or ""),
                "target_instance_id": str(payload.get("target_instance_id") or ""),
            },
            "status": str(payload.get("status") or payload.get("execution_status") or payload.get("state") or ""),
        }

    @staticmethod
    def _artifact_discovery_row(payload: Dict[str, Any]) -> Dict[str, Any]:
        artifact_type = payload.get("artifact_type") if isinstance(payload.get("artifact_type"), dict) else {}
        return {
            "id": str(payload.get("id") or ""),
            "slug": str(payload.get("slug") or ""),
            "title": str(payload.get("title") or ""),
            "artifact_type": str(
                artifact_type.get("slug")
                or payload.get("kind")
                or payload.get("type")
                or ""
            ),
            "status": str(payload.get("artifact_state") or payload.get("status") or ""),
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
        return self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/deployment_plan",
        )

    def create_release_target_deployment_preparation_evidence(
        self, *, target_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/deployment_preparation_evidence",
            json_payload=dict(payload or {}),
        )

    def get_release_target_deployment_preparation_evidence(self, *, target_id: str, limit: int = 10) -> Dict[str, Any]:
        return self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/deployment_preparation_evidence",
            params={"limit": int(limit)},
        )

    def create_release_target_execution_preparation_handoff(
        self, *, target_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_preparation_handoff",
            json_payload=dict(payload or {}),
        )

    def get_release_target_execution_preparation_handoff(self, *, target_id: str, limit: int = 10) -> Dict[str, Any]:
        return self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/execution_preparation_handoff",
            params={"limit": int(limit)},
        )

    def consume_release_target_execution_preparation(
        self, *, target_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_preparation_consume",
            json_payload=dict(payload or {}),
        )

    def run_release_target_execution_step(self, *, target_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request(
            method="POST",
            path=f"/xyn/api/release-targets/{target_id}/execution_step",
            json_payload=dict(payload or {}),
        )

    def get_release_target_execution_step_history(self, *, target_id: str, limit: int = 10) -> Dict[str, Any]:
        return self._request(
            method="GET",
            path=f"/xyn/api/release-targets/{target_id}/execution_step",
            params={"limit": int(limit)},
        )

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
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {"release_target": self._release_target_discovery_row(body)}
        return result

    def list_artifacts(self, *, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        result = self._request(
            method="GET",
            path="/xyn/api/artifacts",
            params={"limit": int(limit), "offset": int(offset)},
        )
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        rows = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
        normalized = [self._artifact_discovery_row(row) for row in rows if isinstance(row, dict)]
        result["response"] = {"artifacts": normalized, "count": len(normalized)}
        return result

    def get_artifact(self, *, artifact_id: str) -> Dict[str, Any]:
        result = self._request(method="GET", path=f"/xyn/api/artifacts/{artifact_id}")
        if not result.get("ok"):
            return result
        body = result.get("response") if isinstance(result.get("response"), dict) else {}
        result["response"] = {"artifact": self._artifact_discovery_row(body)}
        return result

    def list_deployment_providers(self) -> Dict[str, Any]:
        return self._request(method="GET", path="/xyn/api/deployment-providers")

    def get_provider_capabilities(self, *, provider_key: str) -> Dict[str, Any]:
        return self._request(method="GET", path=f"/xyn/api/deployment-providers/{provider_key}")
