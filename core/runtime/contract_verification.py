from __future__ import annotations

from collections import deque
from typing import Any, Callable


def _field_map_from_contract(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = contract.get("fields") if isinstance(contract.get("fields"), list) else []
    return {
        str(row.get("name") or "").strip(): row
        for row in rows
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    }


def _extract_items_from_response(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return [row for row in body.get("items") if isinstance(row, dict)]
    return []


def _sample_field_value(
    *,
    contract: dict[str, Any],
    field: dict[str, Any],
    workspace_id: str,
    created_records: dict[str, dict[str, Any]],
    normalize_unique_strings_fn: Callable[[list[Any] | tuple[Any, ...] | set[Any] | None], list[str]],
) -> Any:
    field_name = str(field.get("name") or "").strip()
    relation = field.get("relation") if isinstance(field.get("relation"), dict) else None
    if field_name == "workspace_id":
        return workspace_id
    if relation:
        target_key = str(relation.get("target_entity") or "").strip()
        target = created_records.get(target_key)
        if not isinstance(target, dict):
            return None
        return str(target.get(relation.get("target_field") or "id") or "").strip() or None
    options = normalize_unique_strings_fn(field.get("options") if isinstance(field.get("options"), list) else [])
    if options:
        return options[0]
    field_type = str(field.get("type") or "string").strip().lower()
    singular = str(contract.get("singular_label") or contract.get("key") or "record").strip().replace(" ", "-")
    if field_name in {"title", "name"}:
        return f"{singular}-1"
    if field_name == "voter_name":
        return "alex"
    if field_name.endswith("_date") or field_name == "date":
        return "2026-03-17"
    if field_name in {"created_at", "updated_at"}:
        return None
    if field_type.startswith("bool"):
        return True
    return f"{singular}-{field_name}-1"


def _build_contract_seed_payload(
    *,
    contract: dict[str, Any],
    workspace_id: str,
    created_records: dict[str, dict[str, Any]],
    normalize_unique_strings_fn: Callable[[list[Any] | tuple[Any, ...] | set[Any] | None], list[str]],
) -> dict[str, Any]:
    fields = _field_map_from_contract(contract)
    required = normalize_unique_strings_fn(
        (contract.get("validation") or {}).get("required_on_create")
        if isinstance(contract.get("validation"), dict)
        else []
    )
    payload: dict[str, Any] = {}
    for field_name in required:
        field = fields.get(field_name)
        if not isinstance(field, dict) or not bool(field.get("writable", False)):
            continue
        payload[field_name] = _sample_field_value(
            contract=contract,
            field=field,
            workspace_id=workspace_id,
            created_records=created_records,
            normalize_unique_strings_fn=normalize_unique_strings_fn,
        )
    for field_name, field in fields.items():
        if field_name in payload or not bool(field.get("writable", False)):
            continue
        if field_name in {"notes", "status", "active"}:
            payload[field_name] = _sample_field_value(
                contract=contract,
                field=field,
                workspace_id=workspace_id,
                created_records=created_records,
                normalize_unique_strings_fn=normalize_unique_strings_fn,
            )
    return {key: value for key, value in payload.items() if value is not None}


def _build_contract_update_payload(
    contract: dict[str, Any],
    normalize_unique_strings_fn: Callable[[list[Any] | tuple[Any, ...] | set[Any] | None], list[str]],
) -> dict[str, Any]:
    fields = _field_map_from_contract(contract)
    allowed = normalize_unique_strings_fn(
        (contract.get("validation") or {}).get("allowed_on_update")
        if isinstance(contract.get("validation"), dict)
        else []
    )
    for field_name in allowed:
        field = fields.get(field_name)
        if not isinstance(field, dict):
            continue
        options = normalize_unique_strings_fn(field.get("options") if isinstance(field.get("options"), list) else [])
        if len(options) > 1:
            return {field_name: options[1]}
        if field_name in {"name", "title", "notes"}:
            return {field_name: f"updated-{field_name}"}
    return {}


def _policy_bundle_entries(policy_bundle: dict[str, Any], family: str) -> list[dict[str, Any]]:
    policies = policy_bundle.get("policies") if isinstance(policy_bundle.get("policies"), dict) else {}
    rows = policies.get(family) if isinstance(policies.get(family), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _compiled_runtime_policies(
    *,
    policy_bundle: dict[str, Any],
    family: str,
    runtime_rule: str,
    entity_key: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for policy in _policy_bundle_entries(policy_bundle, family):
        params = policy.get("parameters") if isinstance(policy.get("parameters"), dict) else {}
        if str(params.get("runtime_rule") or "").strip() != runtime_rule:
            continue
        if str(params.get("entity_key") or "").strip() != entity_key:
            continue
        matches.append(policy)
    return matches


def _allowed_transition_path(
    *,
    current_status: str,
    allowed_statuses: list[str],
    allowed_transitions: dict[str, list[str]],
) -> list[str] | None:
    current = str(current_status or "").strip()
    targets = {str(value).strip() for value in allowed_statuses if str(value).strip()}
    if not current or not targets:
        return None
    if current in targets:
        return []
    queue: deque[tuple[str, list[str]]] = deque([(current, [])])
    seen = {current}
    while queue:
        state, path = queue.popleft()
        for candidate in allowed_transitions.get(state, []):
            next_state = str(candidate or "").strip()
            if not next_state or next_state in seen:
                continue
            next_path = path + [next_state]
            if next_state in targets:
                return next_path
            seen.add(next_state)
            queue.append((next_state, next_path))
    return None


def ensure_parent_status_gate_prerequisites(
    *,
    container_name: str,
    port: int,
    workspace_id: str,
    contract: dict[str, Any],
    entity_contracts: list[dict[str, Any]],
    created_records: dict[str, dict[str, Any]],
    policy_bundle: dict[str, Any],
    container_http_json_fn: Callable[..., tuple[int, dict[str, Any], str]],
) -> None:
    entity_key = str(contract.get("key") or "").strip()
    if not entity_key or not policy_bundle:
        return
    contracts = {
        str(item.get("key") or "").strip(): item
        for item in entity_contracts
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }
    gates = _compiled_runtime_policies(
        policy_bundle=policy_bundle,
        family="validation_policies",
        runtime_rule="parent_status_gate",
        entity_key=entity_key,
    )
    for gate in gates:
        params = gate.get("parameters") if isinstance(gate.get("parameters"), dict) else {}
        if "create" not in {str(value).strip() for value in params.get("on_operations") or [] if str(value).strip()}:
            continue
        parent_entity = str(params.get("parent_entity") or "").strip()
        parent_status_field = str(params.get("parent_status_field") or "").strip()
        allowed_statuses = [str(value).strip() for value in params.get("allowed_parent_statuses") or [] if str(value).strip()]
        if not parent_entity or not parent_status_field or not allowed_statuses:
            continue
        parent_contract = contracts.get(parent_entity)
        parent_record = created_records.get(parent_entity)
        if not isinstance(parent_contract, dict) or not isinstance(parent_record, dict):
            continue
        current_status = str(parent_record.get(parent_status_field) or "").strip()
        if current_status in set(allowed_statuses):
            continue
        transition_policy = next(
            (
                policy
                for policy in _compiled_runtime_policies(
                    policy_bundle=policy_bundle,
                    family="transition_policies",
                    runtime_rule="field_transition_guard",
                    entity_key=parent_entity,
                )
                if str(((policy.get("parameters") or {}).get("field_name")) or "").strip() == parent_status_field
            ),
            None,
        )
        transition_params = transition_policy.get("parameters") if isinstance((transition_policy or {}).get("parameters"), dict) else {}
        transition_path = _allowed_transition_path(
            current_status=current_status,
            allowed_statuses=allowed_statuses,
            allowed_transitions=transition_params.get("allowed_transitions") if isinstance(transition_params.get("allowed_transitions"), dict) else {},
        )
        if transition_path is None:
            transition_path = [allowed_statuses[0]]
        item_ref = str(parent_record.get("id") or "").strip()
        item_template = str(parent_contract.get("item_path_template") or f"/{parent_entity}" + "/{id}").strip()
        item_path = item_template.replace("{id}", item_ref)
        for next_status in transition_path:
            patch_code, patch_body, patch_text = container_http_json_fn(
                container_name,
                "PATCH",
                f"{item_path}?workspace_id={workspace_id}",
                port=port,
                payload={parent_status_field: next_status},
            )
            if patch_code != 200:
                raise RuntimeError(f"PATCH {item_path} failed ({patch_code}): {patch_text}")
            if isinstance(patch_body, dict):
                parent_record = patch_body
                created_records[parent_entity] = patch_body


def exercise_runtime_contracts(
    *,
    container_name: str,
    port: int,
    workspace_id: str,
    entity_contracts: list[dict[str, Any]],
    policy_bundle: dict[str, Any] | None = None,
    container_http_json_fn: Callable[..., tuple[int, dict[str, Any], str]],
    normalize_unique_strings_fn: Callable[[list[Any] | tuple[Any, ...] | set[Any] | None], list[str]],
) -> dict[str, Any]:
    results: dict[str, Any] = {}
    created_records: dict[str, dict[str, Any]] = {}
    pending = [row for row in entity_contracts if isinstance(row, dict)]
    while pending:
        progressed = False
        for contract in pending[:]:
            entity_key = str(contract.get("key") or "").strip()
            relationships = contract.get("relationships") if isinstance(contract.get("relationships"), list) else []
            deps = {
                str(rel.get("target_entity") or "").strip()
                for rel in relationships
                if isinstance(rel, dict)
                and str(rel.get("target_entity") or "").strip()
                and str(rel.get("target_entity") or "").strip() != entity_key
            }
            if any(dep not in created_records for dep in deps):
                continue
            collection_path = str(contract.get("collection_path") or f"/{entity_key}").strip()
            ensure_parent_status_gate_prerequisites(
                container_name=container_name,
                port=port,
                workspace_id=workspace_id,
                contract=contract,
                entity_contracts=entity_contracts,
                created_records=created_records,
                policy_bundle=policy_bundle or {},
                container_http_json_fn=container_http_json_fn,
            )
            seed_payload = _build_contract_seed_payload(
                contract=contract,
                workspace_id=workspace_id,
                created_records=created_records,
                normalize_unique_strings_fn=normalize_unique_strings_fn,
            )
            create_code, create_body, create_text = container_http_json_fn(
                container_name,
                "POST",
                collection_path,
                port=port,
                payload=seed_payload,
            )
            if create_code not in {200, 201}:
                raise RuntimeError(f"POST {collection_path} failed ({create_code}): {create_text}")
            created_record = create_body if isinstance(create_body, dict) else {}
            created_records[entity_key] = created_record
            list_code, list_body, list_text = container_http_json_fn(
                container_name,
                "GET",
                f"{collection_path}?workspace_id={workspace_id}",
                port=port,
            )
            if list_code != 200:
                raise RuntimeError(f"GET {collection_path} failed ({list_code}): {list_text}")
            items = _extract_items_from_response(list_body)
            if not items:
                raise RuntimeError(f"GET {collection_path} returned no items after seeding {entity_key}")
            item_ref = str(created_record.get("id") or "").strip()
            item_path_template = str(contract.get("item_path_template") or f"{collection_path}" + "/{id}")
            item_path = item_path_template.replace("{id}", item_ref)
            get_code, get_body, get_text = container_http_json_fn(
                container_name,
                "GET",
                f"{item_path}?workspace_id={workspace_id}",
                port=port,
            )
            if get_code != 200:
                raise RuntimeError(f"GET {item_path} failed ({get_code}): {get_text}")
            update_payload = _build_contract_update_payload(
                contract,
                normalize_unique_strings_fn=normalize_unique_strings_fn,
            )
            update_result: dict[str, Any] | None = None
            if update_payload:
                update_code, update_body, update_text = container_http_json_fn(
                    container_name,
                    "PATCH",
                    f"{item_path}?workspace_id={workspace_id}",
                    port=port,
                    payload=update_payload,
                )
                if update_code != 200:
                    raise RuntimeError(f"PATCH {item_path} failed ({update_code}): {update_text}")
                update_result = {"code": update_code, "body": update_body or update_text}
            results[entity_key] = {
                "seed_payload": seed_payload,
                "create": {"code": create_code, "body": create_body or create_text},
                "list": {"code": list_code, "body": list_body or list_text},
                "get": {"code": get_code, "body": get_body or get_text},
                "update": update_result,
            }
            pending.remove(contract)
            progressed = True
        if not progressed:
            unresolved = [str(row.get("key") or "").strip() for row in pending if isinstance(row, dict)]
            raise RuntimeError(f"Could not resolve seed order for generated entity contracts: {unresolved}")
    return results
