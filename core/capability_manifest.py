"""Resolved capability manifest helpers for generated Xyn applications."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from core.models import Artifact
from core.primitives import get_primitive_catalog

CAPABILITY_MANIFEST_SCHEMA_VERSION = "xyn.capability_manifest.v1"

_LATENT_COMMAND_SPECS: dict[str, dict[str, Any]] = {
    "show devices by status": {
        "name": "Devices by Status",
        "group": "Reports",
        "kind": "report",
        "resource": "devices_by_status",
        "report_kind": "bar_chart",
        "handler": {
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
        "visibility": ["capability", "landing", "palette"],
        "order": 130,
        "route_id": "devices-by-status",
    },
    "show interfaces by status": {
        "name": "Interfaces by Status",
        "group": "Reports",
        "kind": "report",
        "resource": "interfaces_by_status",
        "report_kind": "bar_chart",
        "handler": {
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
        "visibility": ["capability", "landing", "palette"],
        "order": 150,
        "route_id": "interfaces-by-status",
    },
}

_ROUTE_SPECS: dict[str, dict[str, Any]] = {
    "devices-by-status": {"path": "/reports/devices-by-status", "kind": "report"},
    "interfaces-by-status": {"path": "/reports/interfaces-by-status", "kind": "report"},
}

_ENTITY_BASE_SPECS: dict[str, dict[str, Any]] = {
    "devices": {
        "singular_label": "device",
        "plural_label": "devices",
        "collection_path": "/devices",
        "item_path_template": "/devices/{id}",
        "default_list_fields": ["name", "kind", "status", "location_id"],
        "default_detail_fields": ["id", "name", "kind", "status", "location_id", "workspace_id", "created_at", "updated_at"],
        "title_field": "name",
        "fields": [
            {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
            {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
            {"name": "name", "type": "string", "required": True, "readable": True, "writable": True, "identity": True},
            {
                "name": "kind",
                "type": "string",
                "required": True,
                "readable": True,
                "writable": True,
                "identity": False,
                "options": ["device", "router", "switch"],
            },
            {
                "name": "status",
                "type": "string",
                "required": True,
                "readable": True,
                "writable": True,
                "identity": False,
                "options": ["unknown", "online", "offline"],
            },
            {
                "name": "location_id",
                "type": "uuid|null",
                "required": False,
                "readable": True,
                "writable": True,
                "identity": True,
                "relation": {
                    "target_entity": "locations",
                    "target_field": "id",
                    "relation_kind": "belongs_to",
                },
            },
            {"name": "created_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
            {"name": "updated_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
        ],
        "relationships": [
            {
                "field": "location_id",
                "target_entity": "locations",
                "target_field": "id",
                "relation_kind": "belongs_to",
                "required": False,
            }
        ],
        "required_on_create": ["workspace_id", "name"],
        "allowed_on_update": ["name", "kind", "status", "location_id"],
    },
    "interfaces": {
        "singular_label": "interface",
        "plural_label": "interfaces",
        "collection_path": "/interfaces",
        "item_path_template": "/interfaces/{id}",
        "default_list_fields": ["name", "device_id", "status"],
        "default_detail_fields": ["id", "name", "device_id", "status", "workspace_id", "created_at", "updated_at"],
        "title_field": "name",
        "fields": [
            {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
            {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
            {
                "name": "device_id",
                "type": "uuid",
                "required": True,
                "readable": True,
                "writable": True,
                "identity": True,
                "relation": {
                    "target_entity": "devices",
                    "target_field": "id",
                    "relation_kind": "belongs_to",
                },
            },
            {"name": "name", "type": "string", "required": True, "readable": True, "writable": True, "identity": True},
            {
                "name": "status",
                "type": "string",
                "required": True,
                "readable": True,
                "writable": True,
                "identity": False,
                "options": ["unknown", "up", "down"],
            },
            {"name": "created_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
            {"name": "updated_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
        ],
        "relationships": [
            {
                "field": "device_id",
                "target_entity": "devices",
                "target_field": "id",
                "relation_kind": "belongs_to",
                "required": True,
            }
        ],
        "required_on_create": ["workspace_id", "device_id", "name"],
        "allowed_on_update": ["name", "status", "device_id"],
    },
}


def _location_entity_spec() -> dict[str, Any]:
    primitive = next((row for row in get_primitive_catalog() if str(row.get("slug") or "").strip() == "location"), {})
    fields = primitive.get("fields") if isinstance(primitive.get("fields"), dict) else {}
    field_rows: list[dict[str, Any]] = []
    for name, type_name in fields.items():
        entry: dict[str, Any] = {
            "name": str(name),
            "type": str(type_name),
            "required": str(name) in {"id", "workspace_id", "name", "kind", "created_at", "updated_at"},
            "readable": True,
            "writable": str(name) not in {"id", "created_at", "updated_at"},
            "identity": str(name) in {"id", "name", "city", "region", "country"},
        }
        if str(name) == "parent_location_id":
            entry["relation"] = {
                "target_entity": "locations",
                "target_field": "id",
                "relation_kind": "belongs_to",
            }
        field_rows.append(entry)
    return {
        "singular_label": "location",
        "plural_label": "locations",
        "collection_path": "/locations",
        "item_path_template": "/locations/{id}",
        "default_list_fields": ["name", "kind", "city", "region", "country"],
        "default_detail_fields": ["id", "name", "kind", "city", "region", "country", "workspace_id", "created_at", "updated_at"],
        "title_field": "name",
        "fields": field_rows,
        "relationships": [
            {
                "field": "parent_location_id",
                "target_entity": "locations",
                "target_field": "id",
                "relation_kind": "belongs_to",
                "required": False,
            }
        ],
        "required_on_create": ["workspace_id", "name"],
        "allowed_on_update": [
            "name",
            "kind",
            "parent_location_id",
            "address_line1",
            "address_line2",
            "city",
            "region",
            "postal_code",
            "country",
            "notes",
            "tags_json",
        ],
    }


def _entity_contract_specs() -> dict[str, dict[str, Any]]:
    return {
        "devices": json.loads(json.dumps(_ENTITY_BASE_SPECS["devices"])),
        "locations": _location_entity_spec(),
        "interfaces": json.loads(json.dumps(_ENTITY_BASE_SPECS["interfaces"])),
    }


def _title_case_words(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value or "").replace("_", " ").split())


def _entity_command_order(entity_key: str, operation: str) -> int:
    entity_order = {key: index for index, key in enumerate(_entity_contract_specs().keys())}
    base = 100 + (entity_order.get(entity_key, 50) * 20)
    offsets = {"list": 0, "create": 5, "update": 10, "delete": 15}
    return base + offsets.get(operation, 0)


def _entity_response_columns(contract: dict[str, Any], *, detail: bool) -> list[str]:
    field_names = {
        str(field.get("name") or "").strip()
        for field in (contract.get("fields") if isinstance(contract.get("fields"), list) else [])
        if isinstance(field, dict)
    }
    preferred = contract.get("presentation", {}).get("default_detail_fields" if detail else "default_list_fields") or []
    columns = ["id"] if "id" in field_names else []
    for name in preferred:
        token = str(name or "").strip()
        if token and token in field_names and token not in columns:
            columns.append(token)
    if "workspace_id" in field_names and "workspace_id" not in columns:
        columns.append("workspace_id")
    return columns


def _entity_command_entries(contract: dict[str, Any]) -> list[dict[str, Any]]:
    entity_key = str(contract.get("key") or "").strip()
    singular = str(contract.get("singular_label") or entity_key.rstrip("s")).strip() or entity_key.rstrip("s")
    plural = str(contract.get("plural_label") or entity_key).strip() or entity_key
    collection_path = str(contract.get("collection_path") or f"/{entity_key}")
    item_path = str(contract.get("item_path_template") or f"/{entity_key}" + "/{id}")
    operations = contract.get("operations") if isinstance(contract.get("operations"), dict) else {}
    commands: list[dict[str, Any]] = []
    for operation, spec in operations.items():
        if not isinstance(spec, dict) or not bool(spec.get("declared")):
            continue
        if operation not in {"list", "create", "update", "delete"}:
            continue
        prompt = {
            "list": f"show {plural}",
            "create": f"create {singular}",
            "update": f"update {singular}",
            "delete": f"delete {singular}",
        }[operation]
        verb = operation.capitalize() if operation != "list" else "Show"
        path = str(spec.get("path") or (collection_path if operation in {"list", "create"} else item_path)).strip()
        handler: dict[str, Any] = {
            "base_url": "$deployment.app_url",
            "method": str(spec.get("method") or ("GET" if operation == "list" else "POST")).upper(),
            "path": path,
            "response_adapter": {
                "kind": "table",
                "columns": _entity_response_columns(contract, detail=(operation != "list")),
                "text_template": {
                    "list": f"{{{{count}}}} {plural} found",
                    "create": f"Created {{{{count}}}} {singular}",
                    "update": f"Updated {{{{count}}}} {singular}",
                    "delete": f"Deleted {{{{count}}}} {singular}",
                }[operation],
            },
            "entity_key": entity_key,
            "entity_operation": operation,
        }
        if operation == "list":
            handler["query_map"] = {"workspace_id": "$workspace_id"}
        elif operation == "create":
            handler["body_map"] = {"workspace_id": "$workspace_id"}
        else:
            handler["query_map"] = {"workspace_id": "$workspace_id"}
        commands.append(
            {
                "key": prompt,
                "prompt": prompt,
                "name": f"{verb} {_title_case_words(plural if operation == 'list' else singular)}",
                "group": _title_case_words(plural),
                "kind": "command" if operation == "list" else "operation",
                "resource": entity_key,
                "operation_kind": operation,
                "order": _entity_command_order(entity_key, operation),
                "visibility": ["capability", "palette"] if operation != "list" else ["capability", "landing", "palette"],
                "handler": handler,
                "route_id": entity_key,
            }
        )
    return commands


def _entity_operation_payload(*, declared: bool, method: str, path: str) -> dict[str, Any]:
    return {
        "declared": bool(declared),
        "method": method,
        "path": path,
    }


def _build_entity_contracts(app_spec: dict[str, Any], *, enabled_keys: set[str]) -> list[dict[str, Any]]:
    explicit_contracts = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    if explicit_contracts:
        rows: list[dict[str, Any]] = []
        for contract in explicit_contracts:
            if not isinstance(contract, dict):
                continue
            row = json.loads(json.dumps(contract))
            entity_key = str(row.get("key") or "").strip()
            if not entity_key:
                continue
            row.setdefault("collection_path", f"/{entity_key}")
            row.setdefault("item_path_template", f"/{entity_key}" + "/{id}")
            operations = row.get("operations") if isinstance(row.get("operations"), dict) else {}
            normalized_operations: dict[str, dict[str, Any]] = {}
            for operation in ("list", "get", "create", "update", "delete"):
                spec = operations.get(operation) if isinstance(operations.get(operation), dict) else {}
                default_path = row["collection_path"] if operation in {"list", "create"} else row["item_path_template"]
                normalized_operations[operation] = {
                    "declared": bool(spec.get("declared", True)),
                    "method": str(spec.get("method") or ("GET" if operation in {"list", "get"} else "POST" if operation == "create" else "PATCH" if operation == "update" else "DELETE")).upper(),
                    "path": str(spec.get("path") or default_path),
                }
            row["operations"] = normalized_operations
            rows.append(row)
        rows.sort(key=lambda row: str(row.get("key") or ""))
        return rows

    entities = _infer_entities_from_app_spec(app_spec)
    specs = _entity_contract_specs()
    rows: list[dict[str, Any]] = []
    for entity_key in entities:
        spec = specs.get(entity_key)
        if not isinstance(spec, dict):
            continue
        collection_path = str(spec.get("collection_path") or f"/{entity_key}")
        item_path_template = str(spec.get("item_path_template") or f"/{entity_key}" + "/{id}")
        singular_label = str(spec.get("singular_label") or entity_key.rstrip("s"))
        plural_label = str(spec.get("plural_label") or entity_key)
        rows.append(
            {
                "key": entity_key,
                "singular_label": singular_label,
                "plural_label": plural_label,
                "collection_path": collection_path,
                "item_path_template": item_path_template,
                "operations": {
                    "list": _entity_operation_payload(
                        declared=(f"show {plural_label}" in enabled_keys) or (entity_key == "interfaces" and "show interfaces" in enabled_keys),
                        method="GET",
                        path=collection_path,
                    ),
                    "get": _entity_operation_payload(
                        declared=True,
                        method="GET",
                        path=item_path_template,
                    ),
                    "create": _entity_operation_payload(
                        declared=True,
                        method="POST",
                        path=collection_path,
                    ),
                    "update": _entity_operation_payload(
                        declared=True,
                        method="PATCH",
                        path=item_path_template,
                    ),
                    "delete": _entity_operation_payload(
                        declared=True,
                        method="DELETE",
                        path=item_path_template,
                    ),
                },
                "fields": json.loads(json.dumps(spec.get("fields") or [])),
                "presentation": {
                    "default_list_fields": list(spec.get("default_list_fields") or []),
                    "default_detail_fields": list(spec.get("default_detail_fields") or []),
                    "title_field": str(spec.get("title_field") or "").strip() or None,
                },
                "validation": {
                    "required_on_create": list(spec.get("required_on_create") or []),
                    "allowed_on_update": list(spec.get("allowed_on_update") or []),
                },
                "relationships": json.loads(json.dumps(spec.get("relationships") or [])),
            }
        )
    rows.sort(key=lambda row: str(row.get("key") or ""))
    return rows


def normalize_palette_prompt(prompt: str) -> str:
    key = " ".join(str(prompt or "").strip().lower().split())
    if key.startswith("create location"):
        return "create location"
    if key.startswith("create device"):
        return "create device"
    return key


def _normalize_unique(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        token = text.casefold()
        if token in seen:
            continue
        seen.add(token)
        result.append(text)
    return result


def _infer_entities_from_app_spec(app_spec: dict[str, Any]) -> list[str]:
    explicit_contracts = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    explicit_keys = _normalize_unique(
        [str(row.get("key") or "").strip() for row in explicit_contracts if isinstance(row, dict)]
    )
    if explicit_keys:
        return explicit_keys
    entities = _normalize_unique(app_spec.get("entities") if isinstance(app_spec.get("entities"), list) else [])
    if entities:
        return entities
    inferred: list[str] = []
    reports = _normalize_unique(app_spec.get("reports") if isinstance(app_spec.get("reports"), list) else [])
    if "devices_by_status" in reports:
        inferred.append("devices")
    if "interfaces_by_status" in reports:
        inferred.append("interfaces")
    source_prompt = str(app_spec.get("source_prompt") or "").lower()
    if any(token in source_prompt for token in ("device", "devices", "inventory", "network")):
        inferred.append("devices")
    if any(token in source_prompt for token in ("location", "locations", "site", "sites")):
        inferred.append("locations")
    if any(token in source_prompt for token in ("interface", "interfaces")):
        inferred.append("interfaces")
    return _normalize_unique(inferred)


def _enabled_command_keys(app_spec: dict[str, Any]) -> set[str]:
    entities = {item.casefold() for item in _infer_entities_from_app_spec(app_spec)}
    reports = {item.casefold() for item in _normalize_unique(app_spec.get("reports") if isinstance(app_spec.get("reports"), list) else [])}
    enabled: set[str] = set()
    explicit_contracts = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    explicit_map = {
        str(contract.get("key") or "").strip().casefold(): contract
        for contract in explicit_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    specs = _entity_contract_specs()
    for entity_key in entities:
        contract = explicit_map.get(entity_key) or specs.get(entity_key)
        if not isinstance(contract, dict):
            continue
        singular = str(contract.get("singular_label") or entity_key.rstrip("s")).strip() or entity_key.rstrip("s")
        plural = str(contract.get("plural_label") or entity_key).strip() or entity_key
        enabled.update(
            {
                f"show {plural}",
                f"create {singular}",
                f"update {singular}",
                f"delete {singular}",
            }
        )
    if "devices_by_status" in reports:
        enabled.add("show devices by status")
    if "interfaces_by_status" in reports:
        enabled.add("show interfaces by status")
    return enabled


def build_resolved_capability_manifest(app_spec: dict[str, Any]) -> dict[str, Any]:
    app_slug = str(app_spec.get("app_slug") or "generated-app").strip() or "generated-app"
    title = str(app_spec.get("title") or app_slug).strip() or app_slug
    workspace_id = str(app_spec.get("workspace_id") or "").strip()
    enabled_keys = _enabled_command_keys(app_spec)
    commands: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    latent_commands: list[dict[str, Any]] = []
    latent_routes: list[dict[str, Any]] = []
    latent_reports: list[dict[str, Any]] = []
    latent_operations: list[dict[str, Any]] = []
    enabled_route_ids: set[str] = set()
    entities = _build_entity_contracts(app_spec, enabled_keys=enabled_keys)
    enabled_entity_keys = {str(row.get("key") or "").strip() for row in entities if isinstance(row, dict)}
    all_entity_specs = _entity_contract_specs()
    explicit_contracts = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []

    for contract in entities:
        if not isinstance(contract, dict):
            continue
        route_entry = {
            "id": str(contract.get("key") or "").strip(),
            "path": str(contract.get("collection_path") or ""),
            "kind": "collection",
        }
        if route_entry["id"] and route_entry["path"]:
            routes.append(route_entry)
            enabled_route_ids.add(route_entry["id"])
        for command in _entity_command_entries(contract):
            commands.append(command)
            if command.get("kind") == "operation":
                operations.append(
                    {
                        "id": str(command.get("key") or "").replace(" ", "-"),
                        "name": str(command.get("name") or ""),
                        "kind": str(command.get("operation_kind") or "operation"),
                        "path": str(((command.get("handler") or {}).get("path")) or ""),
                        "method": str(((command.get("handler") or {}).get("method")) or ""),
                        "command_key": str(command.get("key") or ""),
                        "entity_key": str(command.get("resource") or ""),
                    }
                )
    for entity_key, spec in all_entity_specs.items():
        if explicit_contracts:
            break
        if entity_key in enabled_entity_keys:
            continue
        latent_contract = {
            "key": entity_key,
            "singular_label": str(spec.get("singular_label") or entity_key.rstrip("s")),
            "plural_label": str(spec.get("plural_label") or entity_key),
            "collection_path": str(spec.get("collection_path") or f"/{entity_key}"),
            "item_path_template": str(spec.get("item_path_template") or f"/{entity_key}" + "/{id}"),
            "operations": {
                "list": _entity_operation_payload(declared=True, method="GET", path=str(spec.get("collection_path") or f"/{entity_key}")),
                "get": _entity_operation_payload(declared=True, method="GET", path=str(spec.get("item_path_template") or f"/{entity_key}" + "/{id}")),
                "create": _entity_operation_payload(declared=True, method="POST", path=str(spec.get("collection_path") or f"/{entity_key}")),
                "update": _entity_operation_payload(declared=True, method="PATCH", path=str(spec.get("item_path_template") or f"/{entity_key}" + "/{id}")),
                "delete": _entity_operation_payload(declared=True, method="DELETE", path=str(spec.get("item_path_template") or f"/{entity_key}" + "/{id}")),
            },
            "fields": json.loads(json.dumps(spec.get("fields") or [])),
            "presentation": {
                "default_list_fields": list(spec.get("default_list_fields") or []),
                "default_detail_fields": list(spec.get("default_detail_fields") or []),
                "title_field": str(spec.get("title_field") or "").strip() or None,
            },
            "validation": {
                "required_on_create": list(spec.get("required_on_create") or []),
                "allowed_on_update": list(spec.get("allowed_on_update") or []),
            },
            "relationships": json.loads(json.dumps(spec.get("relationships") or [])),
        }
        latent_routes.append({"id": entity_key, "path": latent_contract["collection_path"], "kind": "collection"})
        latent_commands.extend(_entity_command_entries(latent_contract))

    for key, spec in _LATENT_COMMAND_SPECS.items():
        entry = {
            "key": key,
            "prompt": key,
            "name": spec["name"],
            "group": spec["group"],
            "kind": spec["kind"],
            "resource": spec["resource"],
            "visibility": list(spec["visibility"]),
            "order": int(spec["order"]),
            "handler": json.loads(json.dumps(spec["handler"])),
        }
        route_id = str(spec["route_id"])
        if key in enabled_keys:
            commands.append(entry)
            enabled_route_ids.add(route_id)
            if spec["kind"] == "report":
                reports.append(
                    {
                        "id": route_id,
                        "name": spec["name"],
                        "path": spec["handler"]["path"],
                        "chart_kind": spec.get("report_kind") or "",
                        "command_key": key,
                    }
                )
            if spec["kind"] == "operation":
                operations.append(
                    {
                        "id": key.replace(" ", "-"),
                        "name": spec["name"],
                        "kind": spec.get("operation_kind") or "operation",
                        "path": spec["handler"]["path"],
                        "method": spec["handler"]["method"],
                        "command_key": key,
                    }
                )
        else:
            latent_commands.append(entry)
            if spec["kind"] == "report":
                latent_reports.append(
                    {
                        "id": route_id,
                        "name": spec["name"],
                        "path": spec["handler"]["path"],
                        "chart_kind": spec.get("report_kind") or "",
                        "command_key": key,
                    }
                )
            if spec["kind"] == "operation":
                latent_operations.append(
                    {
                        "id": key.replace(" ", "-"),
                        "name": spec["name"],
                        "kind": spec.get("operation_kind") or "operation",
                        "path": spec["handler"]["path"],
                        "method": spec["handler"]["method"],
                        "command_key": key,
                    }
                )

    for route_id, route_spec in _ROUTE_SPECS.items():
        route_entry = {"id": route_id, "path": route_spec["path"], "kind": route_spec["kind"]}
        if route_id in enabled_route_ids:
            routes.append(route_entry)
        else:
            latent_routes.append(route_entry)

    commands.sort(key=lambda row: (int(row["order"]), row["prompt"]))
    latent_commands.sort(key=lambda row: (int(row["order"]), row["prompt"]))
    routes.sort(key=lambda row: row["path"])
    latent_routes.sort(key=lambda row: row["path"])
    reports.sort(key=lambda row: row["path"])
    latent_reports.sort(key=lambda row: row["path"])
    operations.sort(key=lambda row: (row["path"], row["method"]))
    latent_operations.sort(key=lambda row: (row["path"], row["method"]))
    return {
        "schema_version": CAPABILITY_MANIFEST_SCHEMA_VERSION,
        "app": {
            "app_slug": app_slug,
            "title": title,
            "workspace_id": workspace_id,
        },
        "entities": entities,
        "views": [
            {"id": "workbench-manage", "label": "Workbench", "path": "/app/workbench", "surface": "manage"},
            {"id": "workbench-docs", "label": "Workbench", "path": "/app/workbench", "surface": "docs"},
        ],
        "commands": commands,
        "routes": routes,
        "reports": reports,
        "operations": operations,
        "diagnostics": {
            "latent_commands": latent_commands,
            "latent_routes": latent_routes,
            "latent_reports": latent_reports,
            "latent_operations": latent_operations,
        },
    }


def build_manifest_suggestions(*, artifact_slug: str, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for command in manifest.get("commands") if isinstance(manifest.get("commands"), list) else []:
        if not isinstance(command, dict):
            continue
        suggestions.append(
            {
                "id": f"{artifact_slug}-{str(command.get('key') or '').replace(' ', '-')}",
                "name": str(command.get("name") or command.get("prompt") or "").strip(),
                "prompt": str(command.get("prompt") or "").strip(),
                "description": str(command.get("name") or "").strip(),
                "visibility": list(command.get("visibility") or ["capability", "palette"]),
                "group": str(command.get("group") or "").strip(),
                "order": int(command.get("order") or 1000),
            }
        )
    return suggestions


def manifest_enabled_command_keys(manifest: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for command in manifest.get("commands") if isinstance(manifest.get("commands"), list) else []:
        if not isinstance(command, dict):
            continue
        key = normalize_palette_prompt(str(command.get("key") or command.get("prompt") or ""))
        if key:
            result.add(key)
    return result


def manifest_command_entry(manifest: dict[str, Any], prompt: str) -> dict[str, Any] | None:
    key = normalize_palette_prompt(prompt)
    for command in manifest.get("commands") if isinstance(manifest.get("commands"), list) else []:
        if not isinstance(command, dict):
            continue
        command_key = normalize_palette_prompt(str(command.get("key") or command.get("prompt") or ""))
        if command_key == key:
            return command
    return None


def manifest_latent_command_keys(manifest: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    diagnostics = manifest.get("diagnostics") if isinstance(manifest.get("diagnostics"), dict) else {}
    rows = diagnostics.get("latent_commands") if isinstance(diagnostics.get("latent_commands"), list) else []
    for command in rows:
        if not isinstance(command, dict):
            continue
        key = normalize_palette_prompt(str(command.get("key") or command.get("prompt") or ""))
        if key:
            result.add(key)
    return result


def load_workspace_app_spec(db: Session, *, workspace_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        db.query(Artifact)
        .filter(Artifact.workspace_id == workspace_id, Artifact.kind == "app_spec")
        .order_by(Artifact.created_at.desc())
        .first()
    )
    if row is None or not row.storage_path:
        return None
    path = Path(str(row.storage_path))
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_workspace_capability_manifest(db: Session, *, workspace_id: uuid.UUID) -> dict[str, Any] | None:
    app_spec = load_workspace_app_spec(db, workspace_id=workspace_id)
    if not isinstance(app_spec, dict):
        return None
    return build_resolved_capability_manifest(app_spec)
