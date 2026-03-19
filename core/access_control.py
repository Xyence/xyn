"""Compatibility role/capability access helpers for xyn/core runtime surfaces.

Canonical DealFinder-era platform/app authorization lives in xyn-platform:
`xyn_orchestrator.app_authorization`.

This module intentionally mirrors that shape for core-local compatibility paths
without being the canonical source of truth for new platform primitives.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from fastapi import HTTPException, Request


ROLE_APPLICATION_ADMIN = "application_admin"
ROLE_CAMPAIGN_OPERATOR = "campaign_operator"
ROLE_READ_ONLY_ANALYST = "read_only_analyst"


CAP_PLATFORM_ACCESS_READ = "platform.access.read"
CAP_JURISDICTIONS_MANAGE = "app.jurisdictions.manage"
CAP_SOURCES_MANAGE = "app.sources.manage"
CAP_MAPPINGS_INSPECT = "app.mappings.inspect"
CAP_REFRESHES_RUN = "app.refreshes.run"
CAP_INGEST_RUNS_READ = "app.ingest_runs.read"
CAP_FAILURES_READ = "app.failures.read"
CAP_ARTIFACTS_READ = "app.artifacts.read"
CAP_PROVENANCE_READ = "app.provenance.read"
CAP_DATASETS_PUBLISH = "app.datasets.publish"
CAP_SOURCE_DIAGNOSTICS_READ = "app.sources.diagnostics.read"
CAP_CAMPAIGNS_MANAGE = "app.campaigns.manage"
CAP_WATCHES_MANAGE = "app.watches.manage"
CAP_SUBSCRIBERS_MANAGE = "app.subscribers.manage"
CAP_NOTIFICATION_TARGETS_MANAGE = "app.notification_targets.manage"
CAP_MATCH_REVIEW = "app.matches.review"
CAP_SIGNALS_REVIEW = "app.signals.review"
CAP_CAMPAIGN_HISTORY_READ = "app.campaign_history.read"
CAP_NOTIFICATIONS_READ = "app.notifications.read"
CAP_APP_READ = "app.read"


ROLE_CAPABILITY_MAP: Mapping[str, set[str]] = {
    ROLE_APPLICATION_ADMIN: {
        CAP_PLATFORM_ACCESS_READ,
        CAP_JURISDICTIONS_MANAGE,
        CAP_SOURCES_MANAGE,
        CAP_MAPPINGS_INSPECT,
        CAP_REFRESHES_RUN,
        CAP_INGEST_RUNS_READ,
        CAP_FAILURES_READ,
        CAP_ARTIFACTS_READ,
        CAP_PROVENANCE_READ,
        CAP_DATASETS_PUBLISH,
        CAP_SOURCE_DIAGNOSTICS_READ,
        CAP_CAMPAIGNS_MANAGE,
        CAP_WATCHES_MANAGE,
        CAP_SUBSCRIBERS_MANAGE,
        CAP_NOTIFICATION_TARGETS_MANAGE,
        CAP_MATCH_REVIEW,
        CAP_SIGNALS_REVIEW,
        CAP_CAMPAIGN_HISTORY_READ,
        CAP_NOTIFICATIONS_READ,
        CAP_APP_READ,
    },
    ROLE_CAMPAIGN_OPERATOR: {
        CAP_PLATFORM_ACCESS_READ,
        CAP_CAMPAIGNS_MANAGE,
        CAP_WATCHES_MANAGE,
        CAP_SUBSCRIBERS_MANAGE,
        CAP_NOTIFICATION_TARGETS_MANAGE,
        CAP_MATCH_REVIEW,
        CAP_SIGNALS_REVIEW,
        CAP_CAMPAIGN_HISTORY_READ,
        CAP_NOTIFICATIONS_READ,
        CAP_INGEST_RUNS_READ,
        CAP_FAILURES_READ,
        CAP_APP_READ,
    },
    ROLE_READ_ONLY_ANALYST: {
        CAP_PLATFORM_ACCESS_READ,
        CAP_INGEST_RUNS_READ,
        CAP_FAILURES_READ,
        CAP_CAMPAIGN_HISTORY_READ,
        CAP_NOTIFICATIONS_READ,
        CAP_SIGNALS_REVIEW,
        CAP_MATCH_REVIEW,
        CAP_APP_READ,
    },
}


ALL_CAPABILITIES: set[str] = set().union(*ROLE_CAPABILITY_MAP.values())
KNOWN_ROLES: set[str] = set(ROLE_CAPABILITY_MAP.keys())


@dataclass(frozen=True)
class AccessPrincipal:
    subject_id: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    workspace_scope_id: Optional[uuid.UUID] = None
    application_scope: Optional[str] = None


class AccessDeniedError(PermissionError):
    """Raised when a principal cannot perform the requested action."""


def normalize_role_slug(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def normalize_capability_slug(value: str) -> str:
    return str(value or "").strip().lower()


def parse_csv_header(value: str | None) -> tuple[str, ...]:
    if not value:
        return tuple()
    tokens = [token.strip() for token in str(value).split(",") if token.strip()]
    return tuple(tokens)


def resolve_effective_capabilities(
    *,
    roles: Sequence[str],
    direct_capabilities: Sequence[str] = (),
) -> tuple[str, ...]:
    effective: set[str] = set()
    for role in roles:
        normalized = normalize_role_slug(role)
        effective.update(ROLE_CAPABILITY_MAP.get(normalized, set()))
    for capability in direct_capabilities:
        normalized = normalize_capability_slug(capability)
        if normalized:
            effective.add(normalized)
    return tuple(sorted(effective))


def _dev_default_roles() -> tuple[str, ...]:
    auth_mode = str(os.getenv("XYN_AUTH_MODE", "dev") or "dev").strip().lower()
    if auth_mode in {"dev", "simple", "local"}:
        return (ROLE_APPLICATION_ADMIN,)
    return tuple()


def principal_from_request(request: Request) -> AccessPrincipal:
    """Build a principal from request headers using a stable, explicit contract.

    Headers:
    - `X-User-Id`
    - `X-Roles`: comma-separated role slugs
    - `X-Capabilities`: comma-separated capability slugs
    - `X-Access-Workspace-Id`: optional UUID scope restriction
    - `X-Application-Slug`: optional app scope restriction
    """

    subject_id = str(request.headers.get("X-User-Id") or "").strip() or "anonymous"
    raw_roles = parse_csv_header(request.headers.get("X-Roles"))
    roles = tuple(sorted({normalize_role_slug(role) for role in raw_roles if normalize_role_slug(role)}))
    if not roles:
        roles = _dev_default_roles()
        if roles and subject_id == "anonymous":
            subject_id = "dev-user"

    direct_capabilities = tuple(
        sorted(
            {
                normalize_capability_slug(cap)
                for cap in parse_csv_header(request.headers.get("X-Capabilities"))
                if normalize_capability_slug(cap)
            }
        )
    )
    capabilities = resolve_effective_capabilities(roles=roles, direct_capabilities=direct_capabilities)

    workspace_scope_id: Optional[uuid.UUID] = None
    scope_header = str(request.headers.get("X-Access-Workspace-Id") or "").strip()
    if scope_header:
        try:
            workspace_scope_id = uuid.UUID(scope_header)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid X-Access-Workspace-Id header")

    application_scope = str(request.headers.get("X-Application-Slug") or "").strip().lower() or None

    return AccessPrincipal(
        subject_id=subject_id,
        roles=roles,
        capabilities=capabilities,
        workspace_scope_id=workspace_scope_id,
        application_scope=application_scope,
    )


def assert_access(
    principal: AccessPrincipal,
    *,
    required_capabilities: Sequence[str],
    workspace_id: Optional[uuid.UUID] = None,
    application_slug: Optional[str] = None,
    require_all: bool = True,
) -> None:
    required = [normalize_capability_slug(cap) for cap in required_capabilities if normalize_capability_slug(cap)]
    if required:
        principal_caps = set(principal.capabilities)
        allowed = all(cap in principal_caps for cap in required) if require_all else any(cap in principal_caps for cap in required)
        if not allowed:
            raise AccessDeniedError(
                f"Missing required capability: {', '.join(required)}"
            )

    if workspace_id and principal.workspace_scope_id and principal.workspace_scope_id != workspace_id:
        raise AccessDeniedError("Principal is not scoped to the requested workspace")

    normalized_app = str(application_slug or "").strip().lower() or None
    if normalized_app and principal.application_scope and principal.application_scope != normalized_app:
        raise AccessDeniedError("Principal is not scoped to the requested application")


def enforce_access_or_403(
    principal: AccessPrincipal,
    *,
    required_capabilities: Sequence[str],
    workspace_id: Optional[uuid.UUID] = None,
    application_slug: Optional[str] = None,
    require_all: bool = True,
) -> None:
    try:
        assert_access(
            principal,
            required_capabilities=required_capabilities,
            workspace_id=workspace_id,
            application_slug=application_slug,
            require_all=require_all,
        )
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def require_capabilities(*required_capabilities: str, require_all: bool = True, application_slug: Optional[str] = None):
    """FastAPI dependency factory for capability checks."""

    def _dependency(request: Request) -> AccessPrincipal:
        principal = principal_from_request(request)
        try:
            assert_access(
                principal,
                required_capabilities=required_capabilities,
                application_slug=application_slug,
                require_all=require_all,
            )
        except AccessDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return principal

    return _dependency


def role_capability_catalog() -> dict[str, list[str]]:
    return {role: sorted(capabilities) for role, capabilities in ROLE_CAPABILITY_MAP.items()}
