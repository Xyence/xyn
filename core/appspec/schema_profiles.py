from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import ValidationError, validate


APPSPEC_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "appspec_v0.schema.json"


@dataclass
class StrictValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_appspec_default_schema(app_spec: dict[str, Any]) -> None:
    schema = json.loads(APPSPEC_SCHEMA_PATH.read_text(encoding="utf-8"))
    validate(instance=app_spec, schema=schema)


def validate_generated_appspec_strict_profile(app_spec: dict[str, Any]) -> StrictValidationResult:
    errors: list[str] = []
    try:
        validate_appspec_default_schema(app_spec)
    except ValidationError as exc:
        return StrictValidationResult(ok=False, errors=[f"default schema validation failed: {exc.message}"])

    contracts = app_spec.get("entity_contracts")
    if not isinstance(contracts, list):
        return StrictValidationResult(ok=True, errors=[])

    allowed_contract_keys = {
        "key",
        "singular_label",
        "plural_label",
        "collection_path",
        "item_path_template",
        "operations",
        "presentation",
        "validation",
        "fields",
        "relationships",
    }
    allowed_field_keys = {
        "name",
        "type",
        "required",
        "readable",
        "writable",
        "identity",
        "nullable",
        "default",
        "references_entity",
        "references_field",
        "options",
        "relation",
    }
    allowed_relation_keys = {
        "kind",
        "relation_kind",
        "related_entity",
        "target_entity",
        "source_field",
        "field",
        "target_field",
        "required",
    }

    seen_contract_keys: set[str] = set()
    for contract_index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            errors.append(f"entity_contracts[{contract_index}] must be an object.")
            continue
        unknown_contract_keys = sorted(set(contract.keys()) - allowed_contract_keys)
        if unknown_contract_keys:
            errors.append(
                f"entity_contracts[{contract_index}] has unknown keys not allowed in strict profile: {unknown_contract_keys}."
            )
        key = str(contract.get("key") or "").strip()
        if not key:
            errors.append(f"entity_contracts[{contract_index}] requires non-empty 'key'.")
        elif key in seen_contract_keys:
            errors.append(f"entity_contracts contains duplicate key '{key}'.")
        else:
            seen_contract_keys.add(key)

        fields = contract.get("fields")
        if not isinstance(fields, list):
            errors.append(f"entity_contracts[{contract_index}].fields must be an array.")
            fields = []
        seen_field_names: set[str] = set()
        for field_index, field in enumerate(fields):
            if not isinstance(field, dict):
                errors.append(f"entity_contracts[{contract_index}].fields[{field_index}] must be an object.")
                continue
            unknown_field_keys = sorted(set(field.keys()) - allowed_field_keys)
            if unknown_field_keys:
                errors.append(
                    "entity_contracts[{}].fields[{}] has unknown keys not allowed in strict profile: {}.".format(
                        contract_index, field_index, unknown_field_keys
                    )
                )
            field_name = str(field.get("name") or "").strip()
            field_type = str(field.get("type") or "").strip()
            if not field_name:
                errors.append(f"entity_contracts[{contract_index}].fields[{field_index}] requires non-empty 'name'.")
            if not field_type:
                errors.append(f"entity_contracts[{contract_index}].fields[{field_index}] requires non-empty 'type'.")
            if field_name:
                if field_name in seen_field_names:
                    errors.append(
                        f"entity_contracts[{contract_index}] has duplicate field name '{field_name}' in strict profile."
                    )
                seen_field_names.add(field_name)

            relation = field.get("relation")
            if relation is not None:
                if not isinstance(relation, dict):
                    errors.append(
                        f"entity_contracts[{contract_index}].fields[{field_index}].relation must be an object."
                    )
                else:
                    relation_kind = str(relation.get("relation_kind") or relation.get("kind") or "").strip()
                    relation_target = str(relation.get("target_entity") or relation.get("related_entity") or "").strip()
                    relation_field = str(relation.get("target_field") or "").strip()
                    if not relation_kind or not relation_target or not relation_field:
                        errors.append(
                            "entity_contracts[{}].fields[{}].relation requires kind/relation_kind, "
                            "target_entity/related_entity, and target_field in strict profile.".format(
                                contract_index, field_index
                            )
                        )

        relationships = contract.get("relationships")
        if relationships is not None:
            if not isinstance(relationships, list):
                errors.append(f"entity_contracts[{contract_index}].relationships must be an array.")
            else:
                for rel_index, relation in enumerate(relationships):
                    if not isinstance(relation, dict):
                        errors.append(
                            f"entity_contracts[{contract_index}].relationships[{rel_index}] must be an object."
                        )
                        continue
                    unknown_rel_keys = sorted(set(relation.keys()) - allowed_relation_keys)
                    if unknown_rel_keys:
                        errors.append(
                            "entity_contracts[{}].relationships[{}] has unknown keys not allowed in strict profile: {}.".format(
                                contract_index, rel_index, unknown_rel_keys
                            )
                        )
                    relation_kind = str(relation.get("relation_kind") or relation.get("kind") or "").strip()
                    relation_target = str(relation.get("target_entity") or relation.get("related_entity") or "").strip()
                    relation_source = str(relation.get("field") or relation.get("source_field") or "").strip()
                    relation_target_field = str(relation.get("target_field") or "").strip()
                    if not relation_kind or not relation_target or not relation_source or not relation_target_field:
                        errors.append(
                            "entity_contracts[{}].relationships[{}] requires kind/relation_kind, "
                            "target_entity/related_entity, field/source_field, and target_field in strict profile.".format(
                                contract_index, rel_index
                            )
                        )

    return StrictValidationResult(ok=not errors, errors=errors)

