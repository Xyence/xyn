"""Palette prompt execution via command registry."""
from __future__ import annotations

import uuid as uuidlib
import uuid
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from core.context_packs import resolve_bound_context_pack_artifacts
from core.net_inventory_client import deployment_request_json, http_request_json, latest_deployment_for_workspace
from core.palette_commands import build_palette_result_from_items, resolve_palette_command
from core.workspaces import resolve_workspace_by_context


def build_palette_chart_result(
    *,
    labels: list[str],
    values: list[int | float],
    title: str,
    text_template: str,
    label_field: str,
    value_field: str,
) -> dict[str, Any]:
    rows = [{label_field: str(label), value_field: value} for label, value in zip(labels, values)]
    text = str(text_template or "{{count}} buckets").replace("{{count}}", str(len(rows)))
    return {
        "kind": "bar_chart",
        "columns": [label_field, value_field],
        "rows": rows,
        "labels": labels,
        "values": values,
        "title": title,
        "text": text,
    }


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
        if token == "$generated.device_name":
            return f"demo-device-{uuidlib.uuid4().hex[:8]}"
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
    body_map = config.get("body_map") if isinstance(config.get("body_map"), dict) else {}
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
    resolved_body = {
        key: _resolve_value(value, workspace_id=workspace.id, workspace_slug=workspace.slug, deployment=deployment)
        for key, value in body_map.items()
    }

    if deployment is not None and str(base_url).rstrip("/") == str(deployment.get("app_url") or "").rstrip("/"):
        code, body, raw = deployment_request_json(
            deployment=deployment,
            method=method,
            path=path,
            query=resolved_query,
            payload=resolved_body or None,
        )
    else:
        url = f"{base_url}{path}"
        if resolved_query:
            url = f"{url}?{urlencode({k: str(v) for k, v in resolved_query.items() if v is not None})}"
        code, body, raw = http_request_json(url, method=method, payload=resolved_body or None)

    if code < 200 or code >= 300:
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

    kind = str(adapter.get("kind") or "table")
    text_template = str(adapter.get("text_template") or "{{count}} rows")
    if kind == "bar_chart":
        labels = [str(row) for row in (body.get("labels") if isinstance(body.get("labels"), list) else [])]
        raw_values = body.get("values") if isinstance(body.get("values"), list) else []
        values: list[int | float] = []
        for value in raw_values:
            if isinstance(value, (int, float)):
                values.append(value)
            else:
                try:
                    values.append(float(value))
                except Exception:
                    values.append(0)
        result = build_palette_chart_result(
            labels=labels,
            values=values,
            title=str(adapter.get("title") or "Report"),
            text_template=text_template,
            label_field=str(adapter.get("label_field") or "label"),
            value_field=str(adapter.get("value_field") or "value"),
        )
    else:
        items = body.get("items") if isinstance(body.get("items"), list) else None
        if items is None and isinstance(body, dict) and body:
            items = [body]
        if items is None:
            items = []
        columns = adapter.get("columns") if isinstance(adapter.get("columns"), list) else ["id", "name"]
        result = build_palette_result_from_items(
            items=[row for row in items if isinstance(row, dict)],
            columns=[str(col) for col in columns],
            text_template=text_template,
        )
        result["kind"] = kind or result.get("kind") or "table"
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
