"""Palette command registry helpers."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.capability_manifest import (
    load_workspace_capability_manifest,
    manifest_enabled_command_keys,
    manifest_latent_command_keys,
    normalize_palette_prompt,
)
from core.models import PaletteCommand


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_command_key(value: str) -> str:
    return normalize_palette_prompt(value)


def ensure_default_palette_commands(db: Session) -> None:
    """Seed global default commands used by Phase 2 validation."""
    defaults = [
        (
            "show devices",
            {
                "base_url": "$deployment.app_url",
                "method": "GET",
                "path": "/devices",
                "query_map": {"workspace_id": "$workspace_id"},
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "name", "kind", "status", "workspace_id", "location_id"],
                    "text_template": "{{count}} devices found",
                },
            },
        ),
        (
            "show locations",
            {
                "base_url": "$deployment.app_url",
                "method": "GET",
                "path": "/locations",
                "query_map": {"workspace_id": "$workspace_id"},
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "name", "kind", "city", "region", "country", "workspace_id"],
                    "text_template": "{{count}} locations found",
                },
            },
        ),
        (
            "create device",
            {
                "base_url": "$deployment.app_url",
                "method": "POST",
                "path": "/devices",
                "body_map": {
                    "workspace_id": "$workspace_id",
                    "name": "$generated.device_name",
                    "kind": "device",
                    "status": "online",
                },
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "name", "kind", "status", "workspace_id", "location_id"],
                    "text_template": "Created {{count}} device",
                },
            },
        ),
        (
            "create location",
            {
                "base_url": "$deployment.app_url",
                "method": "POST",
                "path": "/locations",
                "body_map": {
                    "workspace_id": "$workspace_id",
                    "name": "$generated.location_name",
                    "kind": "site",
                    "city": "Austin",
                    "region": "TX",
                    "country": "US",
                },
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "name", "kind", "city", "region", "country", "workspace_id"],
                    "text_template": "Created {{count}} location",
                },
            },
        ),
        (
            "show devices by status",
            {
                "base_url": "$deployment.app_url",
                "method": "GET",
                "path": "/reports/devices-by-status",
                "query_map": {"workspace_id": "$workspace_id"},
                "response_adapter": {
                    "kind": "bar_chart",
                    "label_field": "status",
                    "value_field": "count",
                    "title": "Devices by status",
                    "text_template": "{{count}} status buckets found",
                },
            },
        ),
        (
            "show interfaces",
            {
                "base_url": "$deployment.app_url",
                "method": "GET",
                "path": "/interfaces",
                "query_map": {"workspace_id": "$workspace_id"},
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "device_id", "name", "status", "workspace_id"],
                    "text_template": "{{count}} interfaces found",
                },
            },
        ),
        (
            "show interfaces by status",
            {
                "base_url": "$deployment.app_url",
                "method": "GET",
                "path": "/reports/interfaces-by-status",
                "query_map": {"workspace_id": "$workspace_id"},
                "response_adapter": {
                    "kind": "bar_chart",
                    "label_field": "status",
                    "value_field": "count",
                    "title": "Interfaces by status",
                    "text_template": "{{count}} interface status buckets found",
                },
            },
        ),
        (
            "start development effort on artifact",
            {
                "base_url": "$xyn.control_api_url",
                "method": "POST",
                "path": "/api/v1/change-efforts",
                "body_map": {
                    "workspace_id": "$workspace_id",
                    "artifact_slug": "$parsed.artifact_slug",
                    "base_branch": "develop",
                    "target_branch": "develop",
                },
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "artifact_slug", "base_branch", "work_branch", "status"],
                    "text_template": "Created development effort {{id}} for {{artifact_slug}}",
                },
            },
        ),
        (
            "open effort source",
            {
                "base_url": "$xyn.control_api_url",
                "method": "POST",
                "path": "/api/v1/change-efforts/{{effort_id}}/resolve-source",
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "artifact_slug", "repo_key", "repo_subpath", "status"],
                    "text_template": "Resolved effort source for {{id}}",
                },
            },
        ),
        (
            "promote effort to develop",
            {
                "base_url": "$xyn.control_api_url",
                "method": "POST",
                "path": "/api/v1/change-efforts/{{effort_id}}/promote",
                "body_map": {
                    "to_branch": "develop",
                    "strategy": "merge_commit",
                },
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "status", "target_branch", "work_branch"],
                    "text_template": "Promotion request submitted for effort {{id}}",
                },
            },
        ),
        (
            "declare release",
            {
                "base_url": "$xyn.control_api_url",
                "method": "POST",
                "path": "/api/v1/releases/declare",
                "response_adapter": {
                    "kind": "table",
                    "columns": ["id", "artifact_slug", "target_commit_sha", "pipeline_provider", "status"],
                    "text_template": "Declared release {{id}} for {{artifact_slug}}",
                },
            },
        ),
        (
            "show artifact provenance",
            {
                "base_url": "$xyn.control_api_url",
                "method": "GET",
                "path": "/api/v1/provenance/{{artifact_slug}}",
                "query_map": {"workspace_id": "$workspace_id"},
                "response_adapter": {
                    "kind": "table",
                    "columns": ["artifact_slug", "workspace_id"],
                    "text_template": "Provenance loaded for {{artifact_slug}}",
                },
            },
        ),
    ]
    changed = False
    for key, config in defaults:
        existing = (
            db.query(PaletteCommand)
            .filter(PaletteCommand.workspace_id.is_(None), func.lower(PaletteCommand.command_key) == key)
            .first()
        )
        if existing:
            current = existing.handler_config_json if isinstance(existing.handler_config_json, dict) else {}
            if current != config:
                existing.handler_config_json = config
                existing.updated_at = utc_now()
                db.add(existing)
                changed = True
            continue
        row = PaletteCommand(
            workspace_id=None,
            command_key=key,
            handler_type="http_json",
            handler_config_json=config,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        db.add(row)
        changed = True
    if changed:
        db.commit()


def resolve_palette_command(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    prompt: str,
) -> PaletteCommand | None:
    key = normalize_command_key(prompt)
    if not key:
        return None
    if key.startswith("create location"):
        key = "create location"
    elif key.startswith("create device"):
        key = "create device"
    elif key.startswith("start development effort on artifact"):
        key = "start development effort on artifact"
    elif key.startswith("open effort source"):
        key = "open effort source"
    elif key.startswith("promote effort to develop"):
        key = "promote effort to develop"
    elif key.startswith("declare release"):
        key = "declare release"
    elif key.startswith("show artifact provenance"):
        key = "show artifact provenance"

    workspace_match = (
        db.query(PaletteCommand)
        .filter(
            PaletteCommand.workspace_id == workspace_id,
            func.lower(PaletteCommand.command_key) == key,
        )
        .order_by(PaletteCommand.updated_at.desc())
        .first()
    )
    if workspace_match:
        manifest = load_workspace_capability_manifest(db, workspace_id=workspace_id)
        if manifest is None or normalize_command_key(workspace_match.command_key) in manifest_enabled_command_keys(manifest):
            return workspace_match
        return None

    global_match = (
        db.query(PaletteCommand)
        .filter(
            PaletteCommand.workspace_id.is_(None),
            func.lower(PaletteCommand.command_key) == key,
        )
        .order_by(PaletteCommand.updated_at.desc())
        .first()
    )
    if not global_match:
        return None
    manifest = load_workspace_capability_manifest(db, workspace_id=workspace_id)
    if manifest is None or normalize_command_key(global_match.command_key) in manifest_enabled_command_keys(manifest):
        return global_match
    return None


def list_palette_commands(
    db: Session,
    *,
    workspace_id: uuid.UUID,
) -> list[PaletteCommand]:
    rows = (
        db.query(PaletteCommand)
        .filter((PaletteCommand.workspace_id == workspace_id) | (PaletteCommand.workspace_id.is_(None)))
        .order_by(PaletteCommand.workspace_id.is_(None), PaletteCommand.command_key.asc())
        .all()
    )
    manifest = load_workspace_capability_manifest(db, workspace_id=workspace_id)
    if manifest is None:
        return rows
    enabled = manifest_enabled_command_keys(manifest)
    return [row for row in rows if normalize_command_key(row.command_key) in enabled]


def workspace_palette_capability_diagnostics(
    db: Session,
    *,
    workspace_id: uuid.UUID,
) -> dict[str, Any]:
    manifest = load_workspace_capability_manifest(db, workspace_id=workspace_id)
    if manifest is None:
        return {
            "source": "legacy-global-defaults",
            "enabled_command_keys": [],
            "latent_command_keys": [],
        }
    enabled = sorted(manifest_enabled_command_keys(manifest))
    latent = sorted(manifest_latent_command_keys(manifest))
    return {
        "source": "resolved-capability-manifest",
        "manifest": manifest,
        "enabled_command_keys": enabled,
        "latent_command_keys": latent,
    }


def build_palette_result_from_items(
    *,
    items: list[dict[str, Any]],
    columns: list[str],
    text_template: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {}
        for column in columns:
            value = item.get(column)
            row[column] = "" if value is None else value
        rows.append(row)
    text = str(text_template or "{{count}} rows").replace("{{count}}", str(len(rows)))
    first = rows[0] if rows else {}
    for key, value in first.items():
        text = text.replace(f"{{{{{key}}}}}", str(value))
    return {
        "kind": "table",
        "columns": columns,
        "rows": rows,
        "text": text,
    }
