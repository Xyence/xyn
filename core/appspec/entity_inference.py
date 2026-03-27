from __future__ import annotations

import copy
import re
from typing import Any

from core.appspec.normalization import (
    _normalize_unique_strings,
    _pluralize_label,
    _safe_slug,
    _title_case_words,
)
from core.appspec.prompt_sections import _extract_objective_sections


def _extract_app_name_from_prompt(raw_prompt: str, *, fallback: str) -> str:
    text = str(raw_prompt or "").strip()
    patterns = [
        re.compile(r'called\s+[“"]([^”"]+)[”"]', re.IGNORECASE),
        re.compile(r'build\s+(?:a|an)\s+.*?\s+called\s+([^.;]+)', re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value = str(match.group(1) or "").strip().strip(".")
            if value:
                return value
    return str(fallback or "").strip() or "Generated App"


def _extract_objective_entities(raw_prompt: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _extract_objective_sections(raw_prompt).get("core_entities", []):
        cleaned_line = re.sub(r"^\s*\d+\.\s*", "", line).strip()
        if not cleaned_line:
            continue
        parts = [part.strip() for part in re.split(r"\s+-\s+", cleaned_line) if part.strip()]
        if not parts:
            continue
        label = _title_case_words(parts[0])
        fields = [part.strip() for part in parts[1:] if part.strip()]
        if label:
            rows.append({"label": label, "fields": fields})
    return rows


def _field_options_from_token(token: str) -> list[str]:
    match = re.search(r"\(([^)]+)\)", token)
    if not match:
        return []
    return _normalize_unique_strings(
        part.strip()
        for part in re.split(r"[,/]|\\bor\\b", str(match.group(1) or ""), flags=re.IGNORECASE)
    )


def _sanitize_field_label(token: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", "", str(token or "")).strip()
    return cleaned


def _field_key(token: str) -> str:
    return _safe_slug(str(token or "").replace("/", " ").replace("-", " "), default="field").replace("-", "_")


def _field_type_for_token(token: str, *, options: list[str]) -> str:
    key = _field_key(token)
    if key.endswith("_id"):
        return "uuid"
    if key in {"created_at", "updated_at", "poll_date", "date"}:
        return "datetime" if key in {"created_at", "updated_at"} else "string"
    if options and {item.casefold() for item in options} <= {"yes", "no", "true", "false"}:
        return "string"
    return "string"


def _augment_contracts_with_inferred_selection_flags(
    *,
    raw_prompt: str,
    contracts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prompt = str(raw_prompt or "").lower()
    if "selected" not in prompt:
        return contracts
    updated = copy.deepcopy(contracts)
    for contract in updated:
        if not isinstance(contract, dict):
            continue
        relationships = contract.get("relationships") if isinstance(contract.get("relationships"), list) else []
        if not relationships:
            continue
        fields = contract.get("fields") if isinstance(contract.get("fields"), list) else []
        field_names = {str(field.get("name") or "").strip() for field in fields if isinstance(field, dict)}
        if "selected" in field_names:
            continue
        singular = str(contract.get("singular_label") or str(contract.get("key") or "").rstrip("s")).strip().lower()
        plural = str(contract.get("plural_label") or contract.get("key") or "").strip().lower()
        if singular not in prompt and plural not in prompt:
            continue
        if not any(token in prompt for token in (f"selected {singular}", f"selected {plural}", f"{singular} is selected", f"{plural} is selected", f"mark one {singular}", f"mark {singular}")):
            continue
        fields.append(
            {
                "name": "selected",
                "type": "string",
                "required": False,
                "readable": True,
                "writable": True,
                "identity": False,
                "options": ["yes", "no"],
            }
        )
        validation = contract.get("validation") if isinstance(contract.get("validation"), dict) else {}
        validation["allowed_on_update"] = _normalize_unique_strings(list(validation.get("allowed_on_update") or []) + ["selected"])
        contract["validation"] = validation
        presentation = contract.get("presentation") if isinstance(contract.get("presentation"), dict) else {}
        presentation["default_list_fields"] = _normalize_unique_strings(list(presentation.get("default_list_fields") or []) + ["selected"])
        presentation["default_detail_fields"] = _normalize_unique_strings(list(presentation.get("default_detail_fields") or []) + ["selected"])
        contract["presentation"] = presentation
    return updated


def _build_entity_contracts_from_prompt(raw_prompt: str) -> list[dict[str, Any]]:
    entity_rows = _extract_objective_entities(raw_prompt)
    if not entity_rows:
        return []

    singular_index: dict[str, tuple[str, str]] = {}
    for row in entity_rows:
        label = str(row["label"])
        singular_label = label.lower()
        plural_label = _pluralize_label(singular_label)
        entity_key = _safe_slug(plural_label, default="records").replace("-", "_")
        singular_index[_safe_slug(singular_label, default="record").replace("-", "_")] = (entity_key, singular_label)

    contracts: list[dict[str, Any]] = []
    for row in entity_rows:
        label = str(row["label"])
        singular_label = label.lower()
        plural_label = _pluralize_label(singular_label)
        entity_key = _safe_slug(plural_label, default="records").replace("-", "_")
        field_rows: list[dict[str, Any]] = [
            {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
            {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
        ]
        seen_field_names = {"id", "workspace_id"}
        relationships: list[dict[str, Any]] = []
        required_on_create: list[str] = ["workspace_id"]
        allowed_on_update: list[str] = []

        raw_fields = row.get("fields") if isinstance(row.get("fields"), list) else []
        for token in raw_fields:
            cleaned = _sanitize_field_label(str(token))
            options = _field_options_from_token(str(token))
            normalized = _field_key(cleaned)
            relation_target = singular_index.get(normalized)
            if relation_target:
                field_name = f"{normalized}_id"
                relation = {
                    "target_entity": relation_target[0],
                    "target_field": "id",
                    "relation_kind": "belongs_to",
                }
                field_rows.append(
                    {
                        "name": field_name,
                        "type": "uuid",
                        "required": True,
                        "readable": True,
                        "writable": True,
                        "identity": False,
                        "relation": relation,
                    }
                )
                seen_field_names.add(field_name)
                relationships.append(
                    {
                        "field": field_name,
                        "target_entity": relation_target[0],
                        "target_field": "id",
                        "relation_kind": "belongs_to",
                        "required": True,
                    }
                )
                required_on_create.append(field_name)
                allowed_on_update.append(field_name)
                continue

            field_name = normalized
            if field_name in seen_field_names:
                continue
            field: dict[str, Any] = {
                "name": field_name,
                "type": _field_type_for_token(field_name, options=options),
                "required": field_name not in {"notes"},
                "readable": True,
                "writable": field_name not in {"created_at", "updated_at"},
                "identity": field_name in {"name", "title", "voter_name"},
            }
            if options:
                field["options"] = options
            field_rows.append(field)
            seen_field_names.add(field_name)
            if field["writable"]:
                allowed_on_update.append(field_name)
            if field["required"] and field["writable"]:
                required_on_create.append(field_name)

        for standard_field in (
            {"name": "created_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
            {"name": "updated_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
        ):
            if standard_field["name"] in seen_field_names:
                continue
            field_rows.append(standard_field)
            seen_field_names.add(standard_field["name"])
        title_field = next(
            (
                candidate
                for candidate in ("title", "name", "voter_name")
                if any(str(field.get("name") or "") == candidate for field in field_rows)
            ),
            "id",
        )
        default_list_fields = [name for name in (title_field, "status", "poll_id", "lunch_option_id") if any(str(field.get("name") or "") == name for field in field_rows)]
        if not default_list_fields:
            default_list_fields = [str(field.get("name") or "") for field in field_rows if str(field.get("name") or "") not in {"id", "workspace_id", "created_at", "updated_at"}][:4]
        default_detail_fields = ["id", title_field]
        for name in [str(field.get("name") or "") for field in field_rows]:
            if name and name not in default_detail_fields and name not in {"updated_at"}:
                default_detail_fields.append(name)
        contracts.append(
            {
                "key": entity_key,
                "singular_label": singular_label,
                "plural_label": plural_label,
                "collection_path": f"/{entity_key}",
                "item_path_template": f"/{entity_key}" + "/{id}",
                "operations": {
                    "list": {"declared": True, "method": "GET", "path": f"/{entity_key}"},
                    "get": {"declared": True, "method": "GET", "path": f"/{entity_key}" + "/{id}"},
                    "create": {"declared": True, "method": "POST", "path": f"/{entity_key}"},
                    "update": {"declared": True, "method": "PATCH", "path": f"/{entity_key}" + "/{id}"},
                    "delete": {"declared": True, "method": "DELETE", "path": f"/{entity_key}" + "/{id}"},
                },
                "fields": field_rows,
                "presentation": {
                    "default_list_fields": _normalize_unique_strings(default_list_fields),
                    "default_detail_fields": _normalize_unique_strings(default_detail_fields),
                    "title_field": title_field,
                },
                "validation": {
                    "required_on_create": _normalize_unique_strings(required_on_create),
                    "allowed_on_update": _normalize_unique_strings(allowed_on_update),
                },
                "relationships": relationships,
            }
        )
    return _augment_contracts_with_inferred_selection_flags(raw_prompt=raw_prompt, contracts=contracts)


def _infer_entities_from_prompt(raw_prompt: str) -> list[str]:
    structured_contracts = _build_entity_contracts_from_prompt(raw_prompt)
    if structured_contracts:
        return [str(row.get("key") or "").strip() for row in structured_contracts if str(row.get("key") or "").strip()]
    prompt = str(raw_prompt or "").lower()
    entity_map = {
        "devices": ("device", "devices"),
        "locations": ("location", "locations", "site", "sites", "rack", "racks", "room", "rooms"),
        "interfaces": ("interface", "interfaces"),
        "ip_addresses": ("ip address", "ip addresses", "ip_address", "ip_addresses"),
        "vlans": ("vlan", "vlans"),
    }
    entities: list[str] = []
    for slug, tokens in entity_map.items():
        if any(token in prompt for token in tokens):
            entities.append(slug)
    if "devices" not in entities and any(token in prompt for token in ("inventory", "network")):
        entities.append("devices")
    return _normalize_unique_strings(entities)


def _infer_requested_visuals_from_prompt(raw_prompt: str) -> list[str]:
    prompt = str(raw_prompt or "").lower()
    visuals: list[str] = []
    if any(token in prompt for token in ("chart", "report")) and "devices" in prompt and "status" in prompt:
        visuals.append("devices_by_status_chart")
    if any(token in prompt for token in ("chart", "report")) and "interfaces" in prompt and "status" in prompt:
        visuals.append("interfaces_by_status_chart")
    return _normalize_unique_strings(visuals)


def _infer_entities_from_app_spec(app_spec: dict[str, Any]) -> list[str]:
    contract_rows = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    contract_keys = _normalize_unique_strings(
        [str(row.get("key") or "").strip() for row in contract_rows if isinstance(row, dict)]
    )
    if contract_keys:
        return contract_keys
    entities = _normalize_unique_strings(app_spec.get("entities") if isinstance(app_spec.get("entities"), list) else [])
    if entities:
        return entities
    inferred: list[str] = []
    service_names = {
        str(service.get("name") or "").strip().lower()
        for service in app_spec.get("services", [])
        if isinstance(service, dict)
    }
    if "net-inventory-api" in service_names:
        inferred.extend(["devices", "locations"])
    reports = _normalize_unique_strings(app_spec.get("reports") if isinstance(app_spec.get("reports"), list) else [])
    if any(report == "interfaces_by_status" for report in reports):
        inferred.append("interfaces")
    source_prompt = str(app_spec.get("source_prompt") or "")
    inferred.extend(_infer_entities_from_prompt(source_prompt))
    return _normalize_unique_strings(inferred)
