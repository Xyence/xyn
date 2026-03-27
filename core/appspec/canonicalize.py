from __future__ import annotations

import copy
from typing import Any

from core.appspec.interpretation_models import (
    EntityContractIntent,
    EntityIntent,
    InterpretationResult,
    PrimitiveIntent,
    VisualIntent,
)
from core.appspec.normalization import _normalize_unique_strings, _safe_slug


def _normalize_entity_key(value: str) -> str:
    return _safe_slug(str(value or "").strip(), default="records").replace("-", "_")


def _normalize_contracts(contracts: list[dict[str, Any]] | None, *, source: str) -> list[EntityContractIntent]:
    rows: list[EntityContractIntent] = []
    for row in contracts or []:
        if not isinstance(row, dict):
            continue
        key = _normalize_entity_key(str(row.get("key") or ""))
        if not key:
            continue
        normalized = copy.deepcopy(row)
        normalized["key"] = key
        rows.append(
            EntityContractIntent(
                key=key,
                contract=normalized,
                source=source,  # type: ignore[arg-type]
                confidence=1.0 if source == "deterministic" else 0.7,
                evidence="contract_from_prompt" if source == "deterministic" else "contract_from_semantic",
            )
        )
    return rows


def _normalize_entities(entities: list[str] | None, *, source: str) -> list[EntityIntent]:
    normalized = [_normalize_entity_key(item) for item in _normalize_unique_strings(entities or []) if str(item).strip()]
    return [
        EntityIntent(
            key=item,
            source=source,  # type: ignore[arg-type]
            confidence=1.0 if source == "deterministic" else 0.7,
            evidence="entity_from_deterministic" if source == "deterministic" else "entity_from_semantic",
        )
        for item in normalized
    ]


def _normalize_visuals(visuals: list[str] | None, *, source: str) -> list[VisualIntent]:
    rows = _normalize_unique_strings(visuals or [])
    return [
        VisualIntent(
            key=str(item),
            source=source,  # type: ignore[arg-type]
            confidence=1.0 if source == "deterministic" else 0.7,
            evidence="visual_from_deterministic" if source == "deterministic" else "visual_from_semantic",
        )
        for item in rows
    ]


def canonicalize_interpretation(
    *,
    route: str,
    existing_entities: list[str],
    summary_entities: list[str],
    requested_entities: list[str],
    deterministic_entities: list[str],
    semantic_entities: list[str],
    deterministic_contracts: list[dict[str, Any]],
    semantic_contracts: list[dict[str, Any]],
    requested_visuals: list[str],
    deterministic_visuals: list[str],
    semantic_visuals: list[str],
    primitive_keys: list[str],
) -> InterpretationResult:
    result = InterpretationResult(route=route)

    deterministic_contract_intents = _normalize_contracts(deterministic_contracts, source="deterministic")
    semantic_contract_intents = _normalize_contracts(semantic_contracts, source="semantic")
    deterministic_contract_map = {row.key: row for row in deterministic_contract_intents}

    merged_contracts: list[EntityContractIntent] = list(deterministic_contract_intents)
    if route == "B" and deterministic_contract_intents:
        if semantic_contract_intents:
            result.warnings.append(
                "Semantic contracts ignored because deterministic contracts are authoritative in Route B."
            )
    else:
        for row in semantic_contract_intents:
            if row.key in deterministic_contract_map:
                if deterministic_contract_map[row.key].contract != row.contract:
                    result.warnings.append(
                        f"Semantic contract for '{row.key}' differs from deterministic contract; keeping deterministic contract."
                    )
                continue
            merged_contracts.append(row)
    result.entity_contracts = merged_contracts

    deterministic_entity_intents = _normalize_entities(deterministic_entities, source="deterministic")
    semantic_entity_intents = _normalize_entities(semantic_entities, source="semantic")

    if route == "C":
        merged_entities = _normalize_unique_strings(
            [_normalize_entity_key(item) for item in semantic_entities]
            + [_normalize_entity_key(item) for item in existing_entities]
            + [_normalize_entity_key(item) for item in summary_entities]
            + [_normalize_entity_key(item) for item in requested_entities]
            + [_normalize_entity_key(item) for item in deterministic_entities]
        )
    elif route == "B":
        merged_entities = _normalize_unique_strings(
            [_normalize_entity_key(item) for item in existing_entities]
            + [_normalize_entity_key(item) for item in summary_entities]
            + [_normalize_entity_key(item) for item in requested_entities]
            + [_normalize_entity_key(item) for item in deterministic_entities]
            + [_normalize_entity_key(item) for item in semantic_entities]
        )
    else:
        merged_entities = _normalize_unique_strings(
            [_normalize_entity_key(item) for item in existing_entities]
            + [_normalize_entity_key(item) for item in summary_entities]
            + [_normalize_entity_key(item) for item in requested_entities]
            + [_normalize_entity_key(item) for item in deterministic_entities]
        )

    contract_keys = [row.key for row in merged_contracts]
    merged_entities = _normalize_unique_strings(contract_keys + merged_entities)
    source_map = {row.key: row for row in deterministic_entity_intents + semantic_entity_intents}
    result.entities = [
        source_map.get(
            key,
            EntityIntent(key=key, source="merged", confidence=0.8, evidence="entity_from_merged_inference"),
        )
        for key in merged_entities
    ]

    deterministic_visual_intents = _normalize_visuals(deterministic_visuals, source="deterministic")
    semantic_visual_intents = _normalize_visuals(semantic_visuals, source="semantic")
    requested_visual_intents = _normalize_visuals(requested_visuals, source="merged")
    visual_order = [row.key for row in requested_visual_intents + deterministic_visual_intents]
    if route in {"B", "C"}:
        visual_order.extend(row.key for row in semantic_visual_intents)
    visual_keys = _normalize_unique_strings(visual_order)
    visual_source = {row.key: row for row in requested_visual_intents + deterministic_visual_intents + semantic_visual_intents}
    result.visuals = [
        visual_source.get(
            key,
            VisualIntent(key=key, source="merged", confidence=0.8, evidence="visual_from_merged_inference"),
        )
        for key in visual_keys
    ]

    result.primitives = [
        PrimitiveIntent(key=str(item), source="deterministic", confidence=1.0, evidence="primitive_from_keyword_map")
        for item in _normalize_unique_strings(primitive_keys)
    ]
    return result
