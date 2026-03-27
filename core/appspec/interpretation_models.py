from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

InterpretationSource = Literal["deterministic", "semantic", "merged"]


@dataclass
class EntityIntent:
    key: str
    source: InterpretationSource
    confidence: float = 1.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EntityContractIntent:
    key: str
    contract: dict[str, Any]
    source: InterpretationSource
    confidence: float = 1.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VisualIntent:
    key: str
    source: InterpretationSource
    confidence: float = 1.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrimitiveIntent:
    key: str
    source: InterpretationSource
    confidence: float = 1.0
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InterpretationResult:
    route: str
    entities: list[EntityIntent] = field(default_factory=list)
    entity_contracts: list[EntityContractIntent] = field(default_factory=list)
    visuals: list[VisualIntent] = field(default_factory=list)
    primitives: list[PrimitiveIntent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "entities": [row.to_dict() for row in self.entities],
            "entity_contracts": [row.to_dict() for row in self.entity_contracts],
            "visuals": [row.to_dict() for row in self.visuals],
            "primitives": [row.to_dict() for row in self.primitives],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

