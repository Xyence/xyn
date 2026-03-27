from __future__ import annotations

from dataclasses import dataclass, field

from core.appspec.interpretation_models import EntityIntent, InterpretationResult

_BUILTIN_VISUAL_ENTITY_MAP = {
    "devices_by_status_chart": {"devices"},
    "interfaces_by_status_chart": {"interfaces"},
}


@dataclass
class InterpretationValidationResult:
    interpretation: InterpretationResult
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_interpretation_consistency(interpretation: InterpretationResult) -> InterpretationValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    result = interpretation

    entity_keys = {row.key for row in result.entities}

    dedup_contracts = []
    seen_contract_keys: set[str] = set()
    for row in result.entity_contracts:
        if row.key in seen_contract_keys:
            warnings.append(f"Duplicate contract for '{row.key}' detected; keeping first occurrence.")
            continue
        seen_contract_keys.add(row.key)
        dedup_contracts.append(row)
    result.entity_contracts = dedup_contracts

    for contract in result.entity_contracts:
        if contract.key not in entity_keys:
            warnings.append(f"Contract '{contract.key}' was not represented in entities; adding it to entity list.")
            result.entities.append(
                EntityIntent(
                    key=contract.key,
                    source="merged",
                    confidence=0.8,
                    evidence="entity_added_from_contract_consistency",
                )
            )
            entity_keys.add(contract.key)

    dedup_entities = []
    seen_entities: set[str] = set()
    for row in result.entities:
        if row.key in seen_entities:
            continue
        seen_entities.add(row.key)
        dedup_entities.append(row)
    result.entities = dedup_entities
    entity_keys = {row.key for row in result.entities}

    for visual in result.visuals:
        required_entities = _BUILTIN_VISUAL_ENTITY_MAP.get(visual.key)
        if not required_entities:
            continue
        if not required_entities.issubset(entity_keys):
            warnings.append(
                f"Visual '{visual.key}' references missing entities: {', '.join(sorted(required_entities - entity_keys))}."
            )

    deterministic_contract_keys = {
        row.key for row in result.entity_contracts if row.source == "deterministic"
    }
    for row in result.entity_contracts:
        if row.source == "semantic" and row.key in deterministic_contract_keys:
            errors.append(
                f"Semantic contract '{row.key}' conflicts with deterministic contract authority."
            )

    result.warnings.extend(warnings)
    result.errors.extend(errors)
    return InterpretationValidationResult(
        interpretation=result,
        errors=errors,
        warnings=warnings,
    )
