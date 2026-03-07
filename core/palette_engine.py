"""Palette prompt execution via command registry."""
from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from core.context_packs import resolve_bound_context_pack_artifacts
from core.net_inventory_client import deployment_request_json, http_request_json, latest_deployment_for_workspace
from core.palette_commands import build_palette_result_from_items, resolve_palette_command
from core.workspaces import resolve_workspace_by_context


def _resolve_value(
    value: Any,
    *,
    workspace_id: uuid.UUID,
    workspace_slug: str,
    deployment: dict[str, Any] | None,
) -> Any:
    if isinstance(value, str):
        token = value.strip()
        if token == "$workspace_id":
            return str(workspace_id)
        if token == "$workspace_slug":
            return workspace_slug
        if token == "$deployment.app_url":
            return str((deployment or {}).get("app_url") or "")
    return value


def execute_palette_prompt(
    db: Session,
    *,
    prompt: str,
    workspace_id: uuid.UUID | None,
    workspace_slug: str | None,
) -> dict[str, Any]:
    workspace = resolve_workspace_by_context(
        db,
        workspace_id=workspace_id,
        workspace_slug=workspace_slug,
    )
    context_packs, context_warnings = resolve_bound_context_pack_artifacts(db, workspace=workspace)
    command = resolve_palette_command(db, workspace_id=workspace.id, prompt=prompt)
    if not command:
        return {
            "kind": "text",
            "columns": [],
            "rows": [],
            "text": "No matching palette command found.",
            "meta": {
                "workspace_id": str(workspace.id),
                "context_pack_artifact_ids": [str(pack.id) for pack in context_packs],
                "context_pack_slugs": [str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                "context_warnings": context_warnings,
            },
        }

    config = command.handler_config_json if isinstance(command.handler_config_json, dict) else {}
    handler_type = str(command.handler_type or "").strip().lower()
    if handler_type != "http_json":
        return {
            "kind": "text",
            "columns": [],
            "rows": [],
            "text": f"Unsupported handler_type: {handler_type or '<empty>'}",
            "meta": {
                "workspace_id": str(workspace.id),
                "command_id": str(command.id),
                "context_pack_artifact_ids": [str(pack.id) for pack in context_packs],
                "context_pack_slugs": [str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                "context_warnings": context_warnings,
            },
        }

    method = str(config.get("method") or "GET").upper()
    path = str(config.get("path") or "/").strip() or "/"
    query_map = config.get("query_map") if isinstance(config.get("query_map"), dict) else {}
    adapter = config.get("response_adapter") if isinstance(config.get("response_adapter"), dict) else {}

    deployment: dict[str, Any] | None = None
    base_url = _resolve_value(
        config.get("base_url"),
        workspace_id=workspace.id,
        workspace_slug=workspace.slug,
        deployment=None,
    )
    if base_url == "$deployment.app_url" or str(base_url).strip() == "":
        deployment = latest_deployment_for_workspace(db, workspace_id=workspace.id)
        base_url = str(deployment.get("app_url") or "").rstrip("/")
    else:
        base_url = str(base_url).rstrip("/")

    resolved_query = {
        key: _resolve_value(value, workspace_id=workspace.id, workspace_slug=workspace.slug, deployment=deployment)
        for key, value in query_map.items()
    }

    if deployment is not None and str(base_url).rstrip("/") == str(deployment.get("app_url") or "").rstrip("/"):
        code, body, raw = deployment_request_json(
            deployment=deployment,
            method=method,
            path=path,
            query=resolved_query,
        )
    else:
        url = f"{base_url}{path}"
        if resolved_query:
            url = f"{url}?{urlencode({k: str(v) for k, v in resolved_query.items() if v is not None})}"
        code, body, raw = http_request_json(url, method=method)

    if code != 200:
        return {
            "kind": "text",
            "columns": [],
            "rows": [],
            "text": f"Command failed ({code}): {raw}",
            "meta": {
                "workspace_id": str(workspace.id),
                "command_id": str(command.id),
                "context_pack_artifact_ids": [str(pack.id) for pack in context_packs],
                "context_pack_slugs": [str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                "context_warnings": context_warnings,
            },
        }

    items = body.get("items") if isinstance(body.get("items"), list) else []
    columns = adapter.get("columns") if isinstance(adapter.get("columns"), list) else ["id", "name"]
    text_template = str(adapter.get("text_template") or "{{count}} rows")
    result = build_palette_result_from_items(
        items=[row for row in items if isinstance(row, dict)],
        columns=[str(col) for col in columns],
        text_template=text_template,
    )
    result["kind"] = str(adapter.get("kind") or result.get("kind") or "table")
    result["meta"] = {
        "workspace_id": str(workspace.id),
        "workspace_slug": workspace.slug,
        "command_id": str(command.id),
        "command_key": command.command_key,
        "base_url": base_url,
        "context_pack_artifact_ids": [str(pack.id) for pack in context_packs],
        "context_pack_slugs": [str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
        "context_warnings": context_warnings,
    }
    return result
