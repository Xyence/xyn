from __future__ import annotations

from core.appspec.normalization import _normalize_unique_strings


def _contains_phrase(text: str, *phrases: str) -> bool:
    corpus = str(text or "").lower()
    return any(str(phrase or "").lower() in corpus for phrase in phrases if str(phrase or "").strip())


def _infer_primitives_from_text(text: str) -> list[str]:
    prompt = str(text or "").lower()
    primitives: list[str] = []
    primitive_keyword_map: list[tuple[str, tuple[str, ...]]] = [
        ("access_control", ("authorization", "access control", "capability enforcement", "role-based access")),
        ("lifecycle", ("lifecycle", "transition", "stateful control")),
        ("orchestration", ("orchestration", "scheduling", "job execution", "pipeline")),
        ("run_history", ("run history", "operational records", "job history")),
        ("source_connector", ("source connector", "source connector / import", "source import", "ingestion")),
        ("source_governance", ("source governance", "readiness", "activation")),
        ("artifact_storage", ("artifact persistence", "raw artifact", "snapshot artifact")),
        ("changed_data_publication", ("changed-data publication", "reconciled-state", "reconciled property-level state")),
        ("provenance_audit", ("provenance", "audit", "lineage")),
        ("geospatial", ("postgis", "geospatial", "map", "spatial")),
        ("parcel_identity", ("parcel identity", "crosswalk", "canonical parcel", "handle")),
        ("matching", ("matching", "match evaluation")),
        ("watch_subscription", ("watch", "subscription", "campaign")),
        ("notifications", ("notification", "signal feed", "signal list")),
    ]
    for primitive, keywords in primitive_keyword_map:
        if any(keyword in prompt for keyword in keywords):
            primitives.append(primitive)
    if _contains_phrase(prompt, "location", "locations", "address", "site", "building", "room"):
        primitives.append("location")
    return _normalize_unique_strings(primitives)
