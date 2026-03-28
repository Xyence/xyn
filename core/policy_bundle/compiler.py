from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from typing import Any

from core.appspec import normalization as appspec_normalization
from core.appspec import prompt_sections as appspec_prompt_sections


def _safe_slug(value: str, *, default: str = "app") -> str:
    return appspec_normalization._safe_slug(value, default=default)


def _normalize_unique_strings(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    return appspec_normalization._normalize_unique_strings(values)


def _extract_objective_sections(objective: str) -> dict[str, list[str]]:
    return appspec_prompt_sections._extract_objective_sections(objective)


def _generated_artifact_slug(app_slug: str) -> str:
    return f"app.{_safe_slug(app_slug, default='generated-app')}"


def _policy_bundle_slug(app_slug: str) -> str:
    return f"policy.{_safe_slug(app_slug, default='generated-app')}"


def _policy_family_from_statement(statement: str) -> str:
    lowered = str(statement or "").strip().lower()
    if any(token in lowered for token in ("vote counts", "counts per", "count per", "rollup", "aggregate", "total")):
        return "derived_policies"
    if "selected" in lowered and any(token in lowered for token in ("exactly one", "more than one", "only one", "at most one", "at least one")):
        return "invariant_policies"
    if any(token in lowered for token in ("does not belong", "belong to", "exactly one", "more than one", "only one")):
        return "relation_constraints"
    if any(token in lowered for token in ("automatically", "when ", "upon ", "after ")) and any(
        token in lowered for token in ("become", "set ", "mark ", "selected")
    ):
        return "trigger_policies"
    if any(token in lowered for token in ("status", "state", "transition", "allows", "does not allow", "must have")):
        return "transition_policies"
    return "validation_policies"


def _policy_targets_from_statement(statement: str, *, entity_contracts: list[dict[str, Any]]) -> dict[str, Any]:
    lowered = str(statement or "").strip().lower()
    entity_keys: list[str] = []
    field_names: list[str] = []
    for contract in entity_contracts:
        if not isinstance(contract, dict):
            continue
        entity_key = str(contract.get("key") or "").strip()
        singular = str(contract.get("singular_label") or entity_key.rstrip("s")).strip().lower()
        plural = str(contract.get("plural_label") or entity_key).strip().lower()
        if singular and singular in lowered or plural and plural in lowered:
            entity_keys.append(entity_key)
        for field in contract.get("fields") if isinstance(contract.get("fields"), list) else []:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or "").strip()
            if field_name and field_name.replace("_", " ") in lowered:
                field_names.append(field_name)
    return {
        "entity_keys": _normalize_unique_strings(entity_keys),
        "field_names": _normalize_unique_strings(field_names),
    }


def _policy_entity_token_matches(contract: dict[str, Any], token: str) -> bool:
    normalized = str(token or "").strip().lower()
    if not normalized:
        return False
    singular = str(contract.get("singular_label") or str(contract.get("key") or "").rstrip("s")).strip().lower()
    plural = str(contract.get("plural_label") or contract.get("key") or "").strip().lower()
    candidates = {singular, plural}
    if singular.endswith("e"):
        candidates.add(f"{singular[:-1]}ing")
    if singular:
        candidates.add(f"{singular}ing")
    return normalized in {item for item in candidates if item}


def _policy_statement_entity_mentions(statement: str, *, entity_contracts: list[dict[str, Any]]) -> list[str]:
    lowered = str(statement or "").strip().lower()
    tokens = re.findall(r"[a-z_]+", lowered)
    mentions: list[str] = []
    for contract in entity_contracts:
        if not isinstance(contract, dict):
            continue
        singular = str(contract.get("singular_label") or str(contract.get("key") or "").rstrip("s")).strip().lower()
        plural = str(contract.get("plural_label") or contract.get("key") or "").strip().lower()
        phrase_match = any(candidate and candidate in lowered for candidate in (singular, plural))
        token_match = any(_policy_entity_token_matches(contract, token) for token in tokens)
        if phrase_match or token_match:
            mentions.append(str(contract.get("key") or "").strip())
    return _normalize_unique_strings(mentions)


def _policy_status_field(contract: dict[str, Any]) -> tuple[str | None, list[str]]:
    for field in contract.get("fields") if isinstance(contract.get("fields"), list) else []:
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("name") or "").strip()
        options = [str(option).strip() for option in field.get("options") if str(option).strip()] if isinstance(field.get("options"), list) else []
        if field_name == "status" and options:
            return field_name, options
    return None, []


def _compile_relation_constraint_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    sequence = start_sequence
    for entity_key, contract in contracts.items():
        relationships = contract.get("relationships") if isinstance(contract.get("relationships"), list) else []
        relation_rows = [row for row in relationships if isinstance(row, dict) and str(row.get("field") or "").strip()]
        if len(relation_rows) < 2:
            continue
        for relation in relation_rows:
            source_field = str(relation.get("field") or "").strip()
            related_entity = str(relation.get("target_entity") or "").strip()
            if not source_field or not related_entity:
                continue
            related_contract = contracts.get(related_entity)
            if not related_contract:
                continue
            related_relationships = related_contract.get("relationships") if isinstance(related_contract.get("relationships"), list) else []
            for sibling_relation in relation_rows:
                comparison_field = str(sibling_relation.get("field") or "").strip()
                comparison_entity = str(sibling_relation.get("target_entity") or "").strip()
                if not comparison_field or not comparison_entity or comparison_field == source_field:
                    continue
                backlink = next(
                    (
                        row
                        for row in related_relationships
                        if isinstance(row, dict)
                        and str(row.get("target_entity") or "").strip() == comparison_entity
                        and str(row.get("field") or "").strip()
                    ),
                    None,
                )
                if not backlink:
                    continue
                key = (entity_key, source_field, comparison_field, str(backlink.get("field") or "").strip())
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{entity_key}.{source_field} must align with {comparison_field}",
                        "description": (
                            f"Ensure the related {related_entity.rstrip('s')} referenced by {source_field} belongs to the same "
                            f"{comparison_entity.rstrip('s')} referenced by {comparison_field}."
                        ),
                        "family": "relation_constraints",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [entity_key, related_entity, comparison_entity],
                            "field_names": [source_field, comparison_field, str(backlink.get("field") or "").strip()],
                        },
                        "parameters": {
                            "runtime_rule": "match_related_field",
                            "entity_key": entity_key,
                            "source_field": source_field,
                            "related_entity": related_entity,
                            "related_lookup_field": str(relation.get("target_field") or "id").strip() or "id",
                            "related_match_field": str(backlink.get("field") or "").strip(),
                            "comparison_field": comparison_field,
                            "comparison_entity": comparison_entity,
                        },
                        "source": {
                            "kind": "derived_from_entity_contracts",
                            "reason": "multiple_relationship_consistency",
                        },
                        "explanation": {
                            "user_summary": (
                                f"{entity_key.rstrip('s').replace('_', ' ')} references must stay aligned across related records."
                            ),
                            "why_it_exists": "Derived from generated relationship structure so cross-parent mismatches are rejected generically.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _compile_parent_status_gate_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for section in ("behavior", "validation") for item in sections.get(section, []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        mentions = _policy_statement_entity_mentions(statement, entity_contracts=entity_contracts)
        if not mentions:
            continue
        child_contract = next(
            (
                contract
                for contract in entity_contracts
                if isinstance(contract, dict)
                and any(_policy_entity_token_matches(contract, token) for token in re.findall(r"[a-z_]+", lowered))
                and len(contract.get("relationships") or []) > 0
            ),
            None,
        )
        if not child_contract:
            continue
        child_entity = str(child_contract.get("key") or "").strip()
        for relation in child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []:
            if not isinstance(relation, dict):
                continue
            parent_entity = str(relation.get("target_entity") or "").strip()
            if parent_entity not in mentions:
                continue
            parent_contract = contracts.get(parent_entity)
            if not parent_contract:
                continue
            status_field, status_options = _policy_status_field(parent_contract)
            if not status_field or not status_options:
                continue
            mentioned_statuses = [option for option in status_options if re.search(rf"\b{re.escape(option.lower())}\b", lowered)]
            allowed_statuses: list[str] = []
            blocked_statuses: list[str] = []
            if re.search(r"\bnot\s+\w+\b", lowered) and "prevent" in lowered and mentioned_statuses:
                allowed_statuses = mentioned_statuses
            elif "does not allow" in lowered or "not allow" in lowered or "blocked" in lowered:
                blocked_statuses = mentioned_statuses
            elif "allow" in lowered and mentioned_statuses:
                allowed_statuses = mentioned_statuses
            if not allowed_statuses and not blocked_statuses:
                continue
            key = (child_entity, str(relation.get("field") or "").strip(), tuple(sorted(allowed_statuses)), tuple(sorted(blocked_statuses)))
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "id": f"{app_slug}-{sequence:03d}",
                    "name": f"{child_entity} writes must respect {parent_entity} status",
                    "description": statement,
                    "family": "validation_policies",
                    "status": "compiled",
                    "enforcement_stage": "runtime_enforced",
                    "targets": {
                        "entity_keys": [child_entity, parent_entity],
                        "field_names": [str(relation.get("field") or "").strip(), status_field],
                    },
                    "parameters": {
                        "runtime_rule": "parent_status_gate",
                        "entity_key": child_entity,
                        "parent_entity": parent_entity,
                        "parent_relation_field": str(relation.get("field") or "").strip(),
                        "parent_status_field": status_field,
                        "allowed_parent_statuses": allowed_statuses,
                        "blocked_parent_statuses": blocked_statuses,
                        "on_operations": ["create", "update"],
                    },
                    "source": {
                        "kind": "prompt_section",
                        "text": statement,
                    },
                    "explanation": {
                        "user_summary": statement,
                        "why_it_exists": "Derived from prompt-described status-gated write behavior.",
                    },
                }
            )
            sequence += 1
    return rows, sequence


def _compile_transition_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    statements = " ".join(str(item or "").strip().lower() for section in ("behavior", "validation") for item in sections.get(section, []))
    sequence = start_sequence
    rows: list[dict[str, Any]] = []
    for contract in entity_contracts:
        if not isinstance(contract, dict):
            continue
        entity_key = str(contract.get("key") or "").strip()
        if not entity_key:
            continue
        singular = str(contract.get("singular_label") or entity_key.rstrip("s")).strip().lower()
        status_field, status_options = _policy_status_field(contract)
        if not status_field or len(status_options) < 2:
            continue
        if singular not in statements and entity_key not in statements and "status" not in statements:
            continue
        allowed_transitions = {
            option: _normalize_unique_strings(
                [option]
                + ([status_options[index + 1]] if index + 1 < len(status_options) else [])
            )
            for index, option in enumerate(status_options)
        }
        rows.append(
            {
                "id": f"{app_slug}-{sequence:03d}",
                "name": f"{entity_key}.{status_field} transition guard",
                "description": f"Restrict {entity_key} {status_field} changes to the declared ordered states.",
                "family": "transition_policies",
                "status": "compiled",
                "enforcement_stage": "runtime_enforced",
                "targets": {
                    "entity_keys": [entity_key],
                    "field_names": [status_field],
                },
                "parameters": {
                    "runtime_rule": "field_transition_guard",
                    "entity_key": entity_key,
                    "field_name": status_field,
                    "allowed_transitions": allowed_transitions,
                },
                "source": {
                    "kind": "derived_from_entity_contracts",
                    "reason": "ordered_status_enum",
                },
                "explanation": {
                    "user_summary": f"{entity_key.replace('_', ' ')} status changes follow the declared status order.",
                    "why_it_exists": "Derived from ordered status options in the generated entity contract.",
                },
            }
        )
        sequence += 1
    return rows, sequence


def _contract_field(contract: dict[str, Any], field_name: str) -> dict[str, Any] | None:
    for field in contract.get("fields") if isinstance(contract.get("fields"), list) else []:
        if not isinstance(field, dict):
            continue
        if str(field.get("name") or "").strip() == field_name:
            return field
    return None


def _compile_related_count_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("validation", []) + sections.get("views", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "count" not in lowered and "counts" not in lowered:
            continue
        mentions = _policy_statement_entity_mentions(statement, entity_contracts=entity_contracts)
        for child_entity, child_contract in contracts.items():
            relationships = child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                parent_entity = str(relation.get("target_entity") or "").strip()
                relation_field = str(relation.get("field") or "").strip()
                if not parent_entity or not relation_field:
                    continue
                if child_entity not in mentions or parent_entity not in mentions:
                    continue
                key = (parent_entity, child_entity, relation_field)
                if key in seen:
                    continue
                seen.add(key)
                output_field = f"{child_entity}_count"
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{parent_entity} {child_entity} count",
                        "description": statement,
                        "family": "derived_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [parent_entity, child_entity],
                            "field_names": [relation_field, output_field],
                        },
                        "parameters": {
                            "runtime_rule": "related_count",
                            "entity_key": parent_entity,
                            "child_entity": child_entity,
                            "child_relation_field": relation_field,
                            "output_field": output_field,
                            "surfaces": ["list", "detail"],
                        },
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described aggregate/count requirement.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _compile_trigger_policies(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("behavior", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "automatic" not in lowered and "automatically" not in lowered:
            continue
        mentions = _policy_statement_entity_mentions(statement, entity_contracts=entity_contracts)
        if len(mentions) < 2:
            continue
        for source_entity, source_contract in contracts.items():
            if source_entity not in mentions:
                continue
            relationships = source_contract.get("relationships") if isinstance(source_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                target_entity = str(relation.get("target_entity") or "").strip()
                relation_field = str(relation.get("field") or "").strip()
                if not target_entity or target_entity not in mentions:
                    continue
                condition_field = ""
                condition_value: Any = None
                selected_field = _contract_field(source_contract, "selected")
                if isinstance(selected_field, dict):
                    condition_field = "selected"
                    condition_value = "yes" if "yes" in [str(option).strip().lower() for option in selected_field.get("options") or []] else True
                else:
                    status_field, status_options = _policy_status_field(source_contract)
                    if status_field and "selected" in {option.lower() for option in status_options}:
                        condition_field = status_field
                        condition_value = "selected"
                target_status_field, target_status_options = _policy_status_field(contracts.get(target_entity, {}))
                if not condition_field or not target_status_field or "selected" not in {option.lower() for option in target_status_options}:
                    continue
                trigger_key = (source_entity, condition_field, str(condition_value), target_entity, relation_field, target_status_field)
                if trigger_key in seen:
                    continue
                seen.add(trigger_key)
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{source_entity} selected updates {target_entity} status",
                        "description": statement,
                        "family": "trigger_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [source_entity, target_entity],
                            "field_names": [condition_field, relation_field, target_status_field],
                        },
                        "parameters": {
                            "runtime_rule": "post_write_related_update",
                            "source_entity": source_entity,
                            "on_operations": ["create", "update"],
                            "condition_field": condition_field,
                            "condition_equals": condition_value,
                            "target_entity": target_entity,
                            "target_relation_field": relation_field,
                            "target_lookup_field": str(relation.get("target_field") or "id").strip() or "id",
                            "target_update_field": target_status_field,
                            "target_update_value": "selected",
                        },
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described post-write state update behavior.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _compile_parent_scoped_uniqueness_invariants(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("behavior", []) + sections.get("validation", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "selected" not in lowered or not any(token in lowered for token in ("only one", "exactly one", "more than one", "at most one")):
            continue
        mentions = set(_policy_statement_entity_mentions(statement, entity_contracts=entity_contracts))
        for child_entity, child_contract in contracts.items():
            selected_field = _contract_field(child_contract, "selected")
            if not isinstance(selected_field, dict):
                continue
            relationships = child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                parent_entity = str(relation.get("target_entity") or "").strip()
                parent_relation_field = str(relation.get("field") or "").strip()
                if not parent_entity or not parent_relation_field:
                    continue
                if mentions and (child_entity not in mentions or parent_entity not in mentions):
                    continue
                invariant_key = (child_entity, parent_entity, parent_relation_field, "selected")
                if invariant_key in seen:
                    continue
                seen.add(invariant_key)
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{child_entity} selection unique within {parent_entity}",
                        "description": statement,
                        "family": "invariant_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [child_entity, parent_entity],
                            "field_names": [parent_relation_field, "selected"],
                        },
                        "parameters": {
                            "runtime_rule": "at_most_one_matching_child_per_parent",
                            "entity_key": child_entity,
                            "parent_entity": parent_entity,
                            "parent_relation_field": parent_relation_field,
                            "match_field": "selected",
                            "match_value": "yes",
                            "on_operations": ["create", "update"],
                        },
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described single-selection invariant.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


def _infer_parent_state_gate_from_statement(
    *,
    statement: str,
    parent_contract: dict[str, Any],
) -> tuple[str | None, str | None]:
    status_field, status_options = _policy_status_field(parent_contract)
    if not status_field or not status_options:
        return None, None
    lowered = str(statement or "").strip().lower()
    for option in status_options:
        lowered_option = str(option or "").strip().lower()
        if not lowered_option:
            continue
        if (
            f"in {lowered_option} status" in lowered
            or f"status {lowered_option}" in lowered
            or f"status is {lowered_option}" in lowered
            or f"status = {lowered_option}" in lowered
        ):
            return status_field, option
    return None, None


def _compile_parent_scoped_minimum_selection_invariants(
    *,
    app_slug: str,
    entity_contracts: list[dict[str, Any]],
    sections: dict[str, list[str]],
    start_sequence: int,
) -> tuple[list[dict[str, Any]], int]:
    contracts = {
        str(contract.get("key") or "").strip(): contract
        for contract in entity_contracts
        if isinstance(contract, dict) and str(contract.get("key") or "").strip()
    }
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    sequence = start_sequence
    statements = [str(item or "").strip() for item in sections.get("behavior", []) + sections.get("validation", []) if str(item or "").strip()]
    for statement in statements:
        lowered = statement.lower()
        if "selected" not in lowered:
            continue
        if not any(token in lowered for token in ("exactly one", "at least one", "must have one", "must have exactly one")):
            continue
        mentions = set(_policy_statement_entity_mentions(statement, entity_contracts=entity_contracts))
        for child_entity, child_contract in contracts.items():
            selected_field = _contract_field(child_contract, "selected")
            if not isinstance(selected_field, dict):
                continue
            relationships = child_contract.get("relationships") if isinstance(child_contract.get("relationships"), list) else []
            for relation in relationships:
                if not isinstance(relation, dict):
                    continue
                parent_entity = str(relation.get("target_entity") or "").strip()
                parent_relation_field = str(relation.get("field") or "").strip()
                if not parent_entity or not parent_relation_field:
                    continue
                if mentions and (child_entity not in mentions or parent_entity not in mentions):
                    continue
                parent_contract = contracts.get(parent_entity)
                if not parent_contract:
                    continue
                parent_state_field, parent_state_value = _infer_parent_state_gate_from_statement(
                    statement=statement,
                    parent_contract=parent_contract,
                )
                invariant_key = (
                    child_entity,
                    parent_entity,
                    parent_relation_field,
                    "selected",
                    parent_state_field or "",
                    parent_state_value or "",
                )
                if invariant_key in seen:
                    continue
                seen.add(invariant_key)
                parameters: dict[str, Any] = {
                    "runtime_rule": "at_least_one_matching_child_per_parent",
                    "entity_key": child_entity,
                    "parent_entity": parent_entity,
                    "parent_relation_field": parent_relation_field,
                    "match_field": "selected",
                    "match_value": "yes",
                    "on_parent_operations": ["create", "update"],
                    "on_child_operations": ["create", "update", "delete"],
                }
                if parent_state_field and parent_state_value:
                    parameters["parent_state_field"] = parent_state_field
                    parameters["parent_state_value"] = parent_state_value
                rows.append(
                    {
                        "id": f"{app_slug}-{sequence:03d}",
                        "name": f"{parent_entity} requires selected {child_entity}",
                        "description": statement,
                        "family": "invariant_policies",
                        "status": "compiled",
                        "enforcement_stage": "runtime_enforced",
                        "targets": {
                            "entity_keys": [parent_entity, child_entity],
                            "field_names": _normalize_unique_strings(
                                [parent_relation_field, "selected", parent_state_field or ""]
                            ),
                        },
                        "parameters": parameters,
                        "source": {
                            "kind": "prompt_section",
                            "text": statement,
                        },
                        "explanation": {
                            "user_summary": statement,
                            "why_it_exists": "Derived from prompt-described required-selection invariant.",
                        },
                    }
                )
                sequence += 1
    return rows, sequence


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


def _build_policy_bundle(
    *,
    workspace_id: uuid.UUID,
    app_spec: dict[str, Any],
    raw_prompt: str,
) -> dict[str, Any]:
    app_slug = str(app_spec.get("app_slug") or "generated-app").strip() or "generated-app"
    app_title = str(app_spec.get("title") or app_slug).strip() or app_slug
    entity_contracts = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    sections = _extract_objective_sections(raw_prompt)
    families = {
        "validation_policies": [],
        "relation_constraints": [],
        "transition_policies": [],
        "invariant_policies": [],
        "derived_policies": [],
        "trigger_policies": [],
    }
    sequence = 1
    for section_name in ("behavior", "validation"):
        for statement in sections.get(section_name, []):
            text = str(statement or "").strip()
            if not text:
                continue
            family = _policy_family_from_statement(text)
            entry = {
                "id": f"{app_slug}-{sequence:03d}",
                "name": text[:96],
                "description": text,
                "family": family,
                "status": "documented",
                "enforcement_stage": "not_compiled",
                "targets": _policy_targets_from_statement(text, entity_contracts=[row for row in entity_contracts if isinstance(row, dict)]),
                "parameters": {},
                "source": {
                    "kind": "prompt_section",
                    "section": section_name,
                    "text": text,
                },
                "explanation": {
                    "user_summary": text,
                    "why_it_exists": f"Derived from the generated app request {section_name} section.",
                },
            }
            families[family].append(entry)
            sequence += 1

    compiled_relation_constraints, sequence = _compile_relation_constraint_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        start_sequence=sequence,
    )
    families["relation_constraints"].extend(compiled_relation_constraints)
    compiled_parent_status_gates, sequence = _compile_parent_status_gate_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["validation_policies"].extend(compiled_parent_status_gates)
    compiled_transition_policies, sequence = _compile_transition_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["transition_policies"].extend(compiled_transition_policies)
    compiled_derived_policies, sequence = _compile_related_count_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["derived_policies"].extend(compiled_derived_policies)
    compiled_trigger_policies, sequence = _compile_trigger_policies(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["trigger_policies"].extend(compiled_trigger_policies)
    compiled_invariant_policies, sequence = _compile_parent_scoped_uniqueness_invariants(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["invariant_policies"].extend(compiled_invariant_policies)
    compiled_minimum_invariants, sequence = _compile_parent_scoped_minimum_selection_invariants(
        app_slug=app_slug,
        entity_contracts=[row for row in entity_contracts if isinstance(row, dict)],
        sections=sections,
        start_sequence=sequence,
    )
    families["invariant_policies"].extend(compiled_minimum_invariants)

    future_capabilities = [
        "render_policy_bundle",
        "validate_policy_bundle",
        "compile_policy_bundle",
        "simulate_policy_bundle",
        "explain_policy_bundle",
    ]
    signature_source: dict[str, Any] = {}
    for key in (
        "app_slug",
        "entities",
        "entity_contracts",
        "reports",
        "requested_visuals",
        "requires_primitives",
        "workflow_definitions",
        "platform_primitive_composition",
        "ui_surfaces",
        "domain_model",
        "structured_plan",
    ):
        if key in app_spec:
            signature_source[key] = copy.deepcopy(app_spec.get(key))
    app_spec_signature = hashlib.sha256(
        json.dumps(signature_source, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "xyn.policy_bundle.v0",
        "bundle_id": _policy_bundle_slug(app_slug),
        "app_slug": app_slug,
        "workspace_id": str(workspace_id),
        "title": f"{app_title} Policy Bundle",
        "description": "Prompt-derived business policy bundle for the generated application. This artifact is durable and inspectable. A narrow generic subset is compiled into runtime enforcement, while unsupported families remain documented-only.",
        "scope": {
            "artifact_slug": _generated_artifact_slug(app_slug),
            "applies_to": ["generated_runtime", "palette", "future_editor", "future_validator"],
        },
        "ownership": {
            "owner_kind": "generated_application",
            "editable": True,
            "source": "generated_from_prompt",
        },
        "derivation": {
            "source": "generated_from_app_spec",
            "app_slug": app_slug,
            "app_spec_signature": app_spec_signature,
        },
        "policy_families": [key for key, rows in families.items() if rows] or list(families.keys()),
        "policies": families,
        "configurable_parameters": [],
        "explanation": {
            "summary": "Policy bundle scaffolds business-rule intent separately from entity contracts so rendering, editing, validation, and runtime enforcement can target the same durable artifact. The current runtime slice compiles relation constraints, status-based write gates, transition guards, parent-scoped selection invariants (at-most-one plus optional-gated at-least-one), related-count projections, and simple post-write related updates.",
            "coverage": {
                "documented_policy_count": sum(len(rows) for rows in families.values()),
                "compiled_policy_count": sum(
                    1
                    for rows in families.values()
                    for item in rows
                    if isinstance(item, dict) and str(item.get("enforcement_stage") or "").strip() == "runtime_enforced"
                ),
                "entity_contract_count": len(entity_contracts),
            },
            "future_capabilities": future_capabilities,
        },
    }





def build_policy_bundle(*, workspace_id: uuid.UUID, app_spec: dict[str, Any], raw_prompt: str) -> dict[str, Any]:
    return _build_policy_bundle(workspace_id=workspace_id, app_spec=app_spec, raw_prompt=raw_prompt)
