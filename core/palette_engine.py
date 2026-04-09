"""Palette prompt execution via command registry."""
from __future__ import annotations

import os
import uuid as uuidlib
import uuid
import re
from typing import Any
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from core.context_packs import resolve_bound_context_pack_artifacts
from core.net_inventory_client import deployment_request_json, http_request_json, latest_deployment_for_workspace
from core.palette_commands import build_palette_result_from_items, resolve_palette_command, workspace_palette_capability_diagnostics
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
        if token == "$generated.location_name":
            return f"demo-location-{uuidlib.uuid4().hex[:8]}"
        if token == "$xyn.control_api_url":
            return str(
                os.getenv("XYN_CONTROL_API_BASE_URL")
                or os.getenv("XYN_API_BASE_URL")
                or "http://localhost:8000"
            ).rstrip("/")
        if token == "$parsed.artifact_slug":
            return ""
    return value


def _normalize_prompt_key(prompt: str) -> str:
    return " ".join(str(prompt or "").strip().lower().split())


def _parse_location_create_prompt(prompt: str) -> tuple[dict[str, Any], list[str]]:
    normalized = str(prompt or "").strip()
    remainder = normalized[len("create location"):].strip() if normalized.lower().startswith("create location") else normalized
    if remainder.lower().startswith("named "):
        remainder = remainder[6:].strip()
    if not remainder:
        return {}, ["name", "city"]

    name_part = remainder
    location_part = ""
    split_match = re.split(r"\s+in\s+", remainder, maxsplit=1, flags=re.IGNORECASE)
    if len(split_match) == 2:
        name_part, location_part = split_match[0].strip(), split_match[1].strip()

    if not name_part:
        return {}, ["name", "city"] if not location_part else ["name"]

    payload: dict[str, Any] = {"name": name_part, "kind": "site"}
    if location_part:
        tokens = [token.strip(", ") for token in location_part.split() if token.strip(", ")]
        if len(tokens) >= 3 and len(tokens[-1]) >= 2 and len(tokens[-2]) <= 3:
            payload["country"] = tokens[-1]
            payload["region"] = tokens[-2]
            payload["city"] = " ".join(tokens[:-2]).strip()
        else:
            payload["city"] = location_part.strip()
    missing = []
    if not str(payload.get("name") or "").strip():
        missing.append("name")
    if not str(payload.get("city") or "").strip():
        missing.append("city")
    return payload, missing


def _parse_device_create_prompt(prompt: str) -> tuple[dict[str, Any], list[str]]:
    normalized = str(prompt or "").strip()
    remainder = normalized[len("create device"):].strip() if normalized.lower().startswith("create device") else normalized
    if remainder.lower().startswith("named "):
        remainder = remainder[6:].strip()
    if not remainder:
        return {}, ["name"]
    payload = {
        "name": remainder,
        "kind": "router",
        "status": "online",
    }
    missing = [] if str(payload.get("name") or "").strip() else ["name"]
    return payload, missing


def _parse_start_effort_prompt(prompt: str) -> tuple[dict[str, Any], list[str]]:
    normalized = str(prompt or "").strip()
    base = "start development effort on artifact"
    remainder = normalized[len(base):].strip() if normalized.lower().startswith(base) else normalized
    remainder = remainder.strip(": ").strip()
    if not remainder:
        return {}, ["artifact_slug"]
    return {"artifact_slug": remainder}, []


def _parse_effort_id_prompt(prompt: str, prefix: str) -> tuple[dict[str, Any], list[str]]:
    normalized = str(prompt or "").strip()
    remainder = normalized[len(prefix):].strip() if normalized.lower().startswith(prefix) else normalized
    if not remainder:
        return {}, ["effort_id"]
    return {"effort_id": remainder.split()[0]}, []


def _parse_provenance_prompt(prompt: str) -> tuple[dict[str, Any], list[str]]:
    normalized = str(prompt or "").strip()
    base = "show artifact provenance"
    remainder = normalized[len(base):].strip() if normalized.lower().startswith(base) else normalized
    if not remainder:
        return {}, ["artifact_slug"]
    return {"artifact_slug": remainder.split()[0]}, []


def _parse_declare_release_prompt(prompt: str) -> tuple[dict[str, Any], list[str]]:
    text = str(prompt or "").strip()
    pattern = re.compile(
        r"^declare\s+release(?:\s+for)?\s+(?P<artifact>[^\s]+)\s+commit\s+(?P<commit>[0-9a-fA-F]{7,40})\s+revision\s+(?P<revision>[^\s]+)\s+digest\s+(?P<digest>sha256:[0-9a-fA-F]{64})\s*$",
        re.IGNORECASE,
    )
    match = pattern.match(text)
    if not match:
        return {}, ["artifact_slug", "target_commit_sha", "revision_id", "image_digest"]
    artifact_slug = str(match.group("artifact") or "").strip()
    commit = str(match.group("commit") or "").strip().lower()
    revision = str(match.group("revision") or "").strip()
    digest = str(match.group("digest") or "").strip().lower()
    if not artifact_slug or not commit or not revision or not digest:
        return {}, ["artifact_slug", "target_commit_sha", "revision_id", "image_digest"]
    return {
        "artifact_slug": artifact_slug,
        "target_commit_sha": commit,
        "revision_id": revision,
        "image_digest": digest,
    }, []


def _render_path_template(path: str, values: dict[str, Any]) -> str:
    rendered = str(path or "/").strip() or "/"
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def _completion_response(
    *,
    workspace_id: uuid.UUID,
    command_id: uuid.UUID,
    command_key: str,
    missing_fields: list[str],
    example: str,
    context_pack_artifact_ids: list[str],
    context_pack_slugs: list[str],
    context_warnings: list[str],
) -> dict[str, Any]:
    missing_text = ", ".join(missing_fields)
    return {
        "kind": "text",
        "columns": [],
        "rows": [],
        "text": f"To {command_key}, provide: {missing_text}. Example: {example}",
        "meta": {
            "workspace_id": str(workspace_id),
            "command_id": str(command_id),
            "command_key": command_key,
            "missing_fields": missing_fields,
            "context_pack_artifact_ids": context_pack_artifact_ids,
            "context_pack_slugs": context_pack_slugs,
            "context_warnings": context_warnings,
        },
    }


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
        diagnostics = workspace_palette_capability_diagnostics(db, workspace_id=workspace.id)
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
                "capability_diagnostics": diagnostics,
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
    normalized_prompt = _normalize_prompt_key(prompt)
    parsed_tokens: dict[str, Any] = {}
    if command.command_key == "create location" and normalized_prompt.startswith("create location"):
        parsed_payload, missing_fields = _parse_location_create_prompt(prompt)
        if missing_fields:
            return _completion_response(
                workspace_id=workspace.id,
                command_id=command.id,
                command_key=command.command_key,
                missing_fields=missing_fields,
                example="create location named sibling-location-2 in Austin TX USA",
                context_pack_artifact_ids=[str(pack.id) for pack in context_packs],
                context_pack_slugs=[str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                context_warnings=context_warnings,
            )
        resolved_body.update(parsed_payload)
        resolved_body["workspace_id"] = str(workspace.id)
    elif command.command_key == "create device" and normalized_prompt.startswith("create device"):
        parsed_payload, missing_fields = _parse_device_create_prompt(prompt)
        if missing_fields:
            return _completion_response(
                workspace_id=workspace.id,
                command_id=command.id,
                command_key=command.command_key,
                missing_fields=missing_fields,
                example="create device named edge-router-1",
                context_pack_artifact_ids=[str(pack.id) for pack in context_packs],
                context_pack_slugs=[str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                context_warnings=context_warnings,
            )
        resolved_body.update(parsed_payload)
        resolved_body["workspace_id"] = str(workspace.id)
    elif command.command_key == "start development effort on artifact" and normalized_prompt.startswith("start development effort on artifact"):
        parsed_payload, missing_fields = _parse_start_effort_prompt(prompt)
        if missing_fields:
            return _completion_response(
                workspace_id=workspace.id,
                command_id=command.id,
                command_key=command.command_key,
                missing_fields=missing_fields,
                example="start development effort on artifact xyn-api",
                context_pack_artifact_ids=[str(pack.id) for pack in context_packs],
                context_pack_slugs=[str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                context_warnings=context_warnings,
            )
        parsed_tokens.update(parsed_payload)
        resolved_body["workspace_id"] = str(workspace.id)
        resolved_body["artifact_slug"] = str(parsed_payload.get("artifact_slug") or "")
        resolved_body.setdefault("base_branch", "develop")
        resolved_body.setdefault("target_branch", "develop")
    elif command.command_key == "open effort source" and normalized_prompt.startswith("open effort source"):
        parsed_payload, missing_fields = _parse_effort_id_prompt(prompt, "open effort source")
        if missing_fields:
            return _completion_response(
                workspace_id=workspace.id,
                command_id=command.id,
                command_key=command.command_key,
                missing_fields=missing_fields,
                example="open effort source <effort_id>",
                context_pack_artifact_ids=[str(pack.id) for pack in context_packs],
                context_pack_slugs=[str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                context_warnings=context_warnings,
            )
        parsed_tokens.update(parsed_payload)
    elif command.command_key == "promote effort to develop" and normalized_prompt.startswith("promote effort to develop"):
        parsed_payload, missing_fields = _parse_effort_id_prompt(prompt, "promote effort to develop")
        if missing_fields:
            return _completion_response(
                workspace_id=workspace.id,
                command_id=command.id,
                command_key=command.command_key,
                missing_fields=missing_fields,
                example="promote effort to develop <effort_id>",
                context_pack_artifact_ids=[str(pack.id) for pack in context_packs],
                context_pack_slugs=[str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                context_warnings=context_warnings,
            )
        parsed_tokens.update(parsed_payload)
        resolved_body.setdefault("to_branch", "develop")
    elif command.command_key == "show artifact provenance" and normalized_prompt.startswith("show artifact provenance"):
        parsed_payload, missing_fields = _parse_provenance_prompt(prompt)
        if missing_fields:
            return _completion_response(
                workspace_id=workspace.id,
                command_id=command.id,
                command_key=command.command_key,
                missing_fields=missing_fields,
                example="show artifact provenance xyn-api",
                context_pack_artifact_ids=[str(pack.id) for pack in context_packs],
                context_pack_slugs=[str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                context_warnings=context_warnings,
            )
        parsed_tokens.update(parsed_payload)
    elif command.command_key == "declare release" and normalized_prompt.startswith("declare release"):
        parsed_payload, missing_fields = _parse_declare_release_prompt(prompt)
        if missing_fields:
            return _completion_response(
                workspace_id=workspace.id,
                command_id=command.id,
                command_key=command.command_key,
                missing_fields=missing_fields,
                example=(
                    "declare release xyn-api commit abcdef0123456789 "
                    "revision rev-1 digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
                ),
                context_pack_artifact_ids=[str(pack.id) for pack in context_packs],
                context_pack_slugs=[str((pack.extra_metadata or {}).get("pack_slug") or pack.name) for pack in context_packs],
                context_warnings=context_warnings,
            )
        parsed_tokens.update(parsed_payload)
        artifact_slug = str(parsed_payload.get("artifact_slug") or "")
        revision_id = str(parsed_payload.get("revision_id") or "")
        image_digest = str(parsed_payload.get("image_digest") or "")
        resolved_body = {
            "workspace_id": str(workspace.id),
            "artifact_slug": artifact_slug,
            "target_commit_sha": str(parsed_payload.get("target_commit_sha") or ""),
            "artifact_revision_map": {artifact_slug: revision_id},
            "image_digest_map": {artifact_slug: image_digest},
            "pipeline_provider": "github_actions",
        }

    template_values = {**resolved_query, **resolved_body, **parsed_tokens}
    path = _render_path_template(path, template_values)

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

    # Create flows should return the refreshed collection so the visible table
    # stays representative of current state instead of collapsing to just the
    # newly-created row.
    if command.command_key == "create device":
        list_code, list_body, list_raw = (
            deployment_request_json(
                deployment=deployment,
                method="GET",
                path="/devices",
                query={"workspace_id": str(workspace.id)},
                payload=None,
            )
            if deployment is not None and str(base_url).rstrip("/") == str(deployment.get("app_url") or "").rstrip("/")
            else http_request_json(
                f"{base_url}/devices?{urlencode({'workspace_id': str(workspace.id)})}",
                "GET",
                payload=None,
            )
        )
        if 200 <= list_code < 300:
            items = list_body if isinstance(list_body, list) else (list_body.get("items") if isinstance(list_body, dict) and isinstance(list_body.get("items"), list) else [])
            result = build_palette_result_from_items(
                items=[row for row in items if isinstance(row, dict)],
                columns=["id", "name", "kind", "status", "location_id"],
                text_template=f"Created 1 device: {str(resolved_body.get('name') or 'unknown')}",
            )
            result["kind"] = "table"
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
    if command.command_key == "create location":
        list_code, list_body, list_raw = (
            deployment_request_json(
                deployment=deployment,
                method="GET",
                path="/locations",
                query={"workspace_id": str(workspace.id)},
                payload=None,
            )
            if deployment is not None and str(base_url).rstrip("/") == str(deployment.get("app_url") or "").rstrip("/")
            else http_request_json(
                f"{base_url}/locations?{urlencode({'workspace_id': str(workspace.id)})}",
                "GET",
                payload=None,
            )
        )
        if 200 <= list_code < 300:
            items = list_body if isinstance(list_body, list) else (list_body.get("items") if isinstance(list_body, dict) and isinstance(list_body.get("items"), list) else [])
            result = build_palette_result_from_items(
                items=[row for row in items if isinstance(row, dict)],
                columns=["id", "name", "kind", "city", "region", "country"],
                text_template=f"Created 1 location: {str(resolved_body.get('name') or 'unknown')}",
            )
            result["kind"] = "table"
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
