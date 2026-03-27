from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from core.appspec.normalization import _safe_slug


@dataclass
class EntityContractValidationResult:
    contracts: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_field(row: dict[str, Any], *, contract_key: str, index: int) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    name = str(row.get("name") or "").strip()
    field_type = str(row.get("type") or "").strip()
    if not name or not field_type:
        warnings.append(f"Contract '{contract_key}' field[{index}] missing required name/type; dropped.")
        return None, warnings
    normalized_name = _safe_slug(name, default="").replace("-", "_")
    if not normalized_name:
        warnings.append(f"Contract '{contract_key}' field[{index}] has invalid name '{name}'; dropped.")
        return None, warnings
    normalized = copy.deepcopy(row)
    normalized["name"] = normalized_name
    normalized["type"] = field_type
    return normalized, warnings


def validate_and_normalize_entity_contracts(contracts: list[dict[str, Any]] | None) -> EntityContractValidationResult:
    warnings: list[str] = []
    errors: list[str] = []
    normalized_contracts: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for index, raw in enumerate(contracts or []):
        if not isinstance(raw, dict):
            warnings.append(f"Contract[{index}] is not an object; dropped.")
            continue
        key = _safe_slug(str(raw.get("key") or "").strip(), default="").replace("-", "_")
        if not key:
            errors.append(f"Contract[{index}] missing required key.")
            continue
        if key in seen_keys:
            warnings.append(f"Duplicate contract '{key}' detected; keeping first occurrence.")
            continue
        seen_keys.add(key)

        normalized = copy.deepcopy(raw)
        normalized["key"] = key
        if not str(normalized.get("singular_label") or "").strip():
            normalized["singular_label"] = key.rstrip("s") or key
        if not str(normalized.get("plural_label") or "").strip():
            normalized["plural_label"] = key
        if not str(normalized.get("collection_path") or "").strip():
            normalized["collection_path"] = f"/{key}"
        if not str(normalized.get("item_path_template") or "").strip():
            normalized["item_path_template"] = f"/{key}" + "/{id}"

        raw_fields = normalized.get("fields")
        if not isinstance(raw_fields, list):
            warnings.append(f"Contract '{key}' has non-list fields; normalizing to empty list.")
            raw_fields = []
        field_rows: list[dict[str, Any]] = []
        seen_fields: dict[str, str] = {}
        for field_index, field_raw in enumerate(raw_fields):
            if not isinstance(field_raw, dict):
                warnings.append(f"Contract '{key}' field[{field_index}] is not an object; dropped.")
                continue
            field, field_warnings = _normalize_field(field_raw, contract_key=key, index=field_index)
            warnings.extend(field_warnings)
            if not field:
                continue
            field_name = str(field["name"])
            field_type = str(field["type"])
            previous_type = seen_fields.get(field_name)
            if previous_type is not None:
                if previous_type != field_type:
                    warnings.append(
                        f"Contract '{key}' has contradictory duplicate field '{field_name}' types "
                        f"('{previous_type}' vs '{field_type}'); keeping first."
                    )
                else:
                    warnings.append(
                        f"Contract '{key}' has duplicate field '{field_name}'; keeping first."
                    )
                continue
            seen_fields[field_name] = field_type
            field_rows.append(field)
        normalized["fields"] = field_rows

        relationships = normalized.get("relationships")
        if relationships is None:
            normalized["relationships"] = []
        elif not isinstance(relationships, list):
            warnings.append(f"Contract '{key}' has non-list relationships; normalizing to empty list.")
            normalized["relationships"] = []

        for optional_dict_key in ("operations", "presentation", "validation"):
            value = normalized.get(optional_dict_key)
            if value is not None and not isinstance(value, dict):
                warnings.append(
                    f"Contract '{key}' has non-object '{optional_dict_key}'; normalizing to empty object."
                )
                normalized[optional_dict_key] = {}

        normalized_contracts.append(normalized)

    return EntityContractValidationResult(
        contracts=normalized_contracts,
        warnings=warnings,
        errors=errors,
    )

