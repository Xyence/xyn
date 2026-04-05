from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

from jsonschema import ValidationError, validate

from core.appspec.normalization import _normalize_unique_strings, _safe_slug

_SEMANTIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["entities", "entity_contracts", "requested_visuals"],
    "properties": {
        "entities": {"type": "array", "items": {"type": "string"}},
        "entity_contracts": {"type": "array", "items": {"type": "object"}},
        "requested_visuals": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

_ENTITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "notes": ("note", "notes", "knowledgebase", "knowledge base"),
    "tasks": ("task", "tasks", "todo", "to-do"),
    "projects": ("project", "projects"),
    "documents": ("document", "documents", "doc", "docs"),
    "campaigns": ("campaign", "campaigns"),
    "properties": ("property", "properties", "parcel", "parcels"),
    "signals": ("signal", "signals", "event", "events"),
    "sources": ("source", "sources", "connector", "connectors"),
    "watches": ("watch", "watches", "subscription", "subscriptions"),
    "customers": ("customer", "customers", "client", "clients"),
    "tickets": ("ticket", "tickets", "issue", "issues"),
}

_VISUAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "devices_by_status_chart": ("devices by status",),
    "interfaces_by_status_chart": ("interfaces by status",),
}


def _semantic_codex_binary() -> str:
    return str(os.getenv("XYN_APPSPEC_SEMANTIC_CODEX_BINARY") or "").strip() or shutil.which("codex") or "codex"


def _semantic_codex_available(codex_bin: str) -> bool:
    if "/" in codex_bin:
        return os.path.isfile(codex_bin) and os.access(codex_bin, os.X_OK)
    return bool(shutil.which(codex_bin))


def _semantic_capability_state(*, prefer_llm: bool, force_llm: bool, llm_enabled: bool, codex_available: bool) -> str:
    if force_llm:
        return "llm_forced"
    if prefer_llm and llm_enabled and codex_available:
        return "hybrid_llm_available"
    return "limited_no_llm"


def _limited_mode_reason(*, llm_enabled: bool, codex_available: bool) -> str:
    if not llm_enabled:
        return "llm_fallback_disabled"
    if not codex_available:
        return "codex_unavailable"
    return "heuristic_only"


def _simple_entity_contract(entity_key: str) -> dict[str, Any]:
    key = _safe_slug(str(entity_key or "").strip(), default="records").replace("-", "_")
    singular = key[:-1] if key.endswith("s") and len(key) > 1 else key
    return {
        "key": key,
        "singular_label": singular.replace("_", " "),
        "plural_label": key.replace("_", " "),
        "collection_path": f"/{key}",
        "item_path_template": f"/{key}" + "/{id}",
        "operations": {
            "list": {"declared": True, "method": "GET", "path": f"/{key}"},
            "get": {"declared": True, "method": "GET", "path": f"/{key}" + "/{id}"},
            "create": {"declared": True, "method": "POST", "path": f"/{key}"},
            "update": {"declared": True, "method": "PATCH", "path": f"/{key}" + "/{id}"},
            "delete": {"declared": True, "method": "DELETE", "path": f"/{key}" + "/{id}"},
        },
        "fields": [
            {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
            {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
            {"name": "name", "type": "string", "required": True, "readable": True, "writable": True, "identity": True},
            {"name": "status", "type": "string", "required": False, "readable": True, "writable": True, "identity": False},
            {"name": "created_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
            {"name": "updated_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
        ],
        "presentation": {
            "default_list_fields": ["name", "status"],
            "default_detail_fields": ["id", "name", "status", "workspace_id", "created_at", "updated_at"],
            "title_field": "name",
        },
        "validation": {
            "required_on_create": ["workspace_id", "name"],
            "allowed_on_update": ["name", "status"],
        },
        "relationships": [],
    }


def _heuristic_semantic_extract(raw_prompt: str) -> dict[str, Any]:
    prompt = str(raw_prompt or "").strip().lower()
    entities: list[str] = []
    for key, tokens in _ENTITY_KEYWORDS.items():
        if any(token in prompt for token in tokens):
            entities.append(key)
    entity_contracts = [_simple_entity_contract(entity) for entity in _normalize_unique_strings(entities)]
    visuals: list[str] = []
    for key, tokens in _VISUAL_KEYWORDS.items():
        if any(token in prompt for token in tokens):
            visuals.append(key)
    if "chart" in prompt and "status" in prompt:
        if "device" in prompt:
            visuals.append("devices_by_status_chart")
        if "interface" in prompt:
            visuals.append("interfaces_by_status_chart")
    return {
        "entities": _normalize_unique_strings(entities),
        "entity_contracts": entity_contracts,
        "requested_visuals": _normalize_unique_strings(visuals),
    }


def _normalize_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    entities = _normalize_unique_strings(payload.get("entities") if isinstance(payload.get("entities"), list) else [])
    entities = [_safe_slug(item, default="records").replace("-", "_") for item in entities if str(item).strip()]
    visuals = _normalize_unique_strings(
        payload.get("requested_visuals") if isinstance(payload.get("requested_visuals"), list) else []
    )
    contracts_raw = payload.get("entity_contracts") if isinstance(payload.get("entity_contracts"), list) else []
    contracts: list[dict[str, Any]] = []
    for row in contracts_raw:
        if not isinstance(row, dict):
            continue
        key = _safe_slug(str(row.get("key") or ""), default="").replace("-", "_")
        if not key:
            continue
        normalized_row = json.loads(json.dumps(row))
        normalized_row["key"] = key
        contracts.append(normalized_row)
    if not contracts and entities:
        contracts = [_simple_entity_contract(entity) for entity in entities]
    return {
        "entities": _normalize_unique_strings(entities),
        "entity_contracts": contracts,
        "requested_visuals": visuals,
    }


def _payload_types_valid(payload: dict[str, Any]) -> bool:
    if "entities" in payload and not isinstance(payload.get("entities"), list):
        return False
    if "entity_contracts" in payload and not isinstance(payload.get("entity_contracts"), list):
        return False
    if "requested_visuals" in payload and not isinstance(payload.get("requested_visuals"), list):
        return False
    return True


def _extract_via_codex(raw_prompt: str) -> dict[str, Any]:
    codex_bin = _semantic_codex_binary()
    if not _semantic_codex_available(codex_bin):
        raise RuntimeError("codex executable unavailable")
    instruction = (
        "Return JSON only (no markdown) matching exactly this schema: "
        '{"entities":[string], "entity_contracts":[object], "requested_visuals":[string]}. '
        "Entities should be snake_case plural keys. "
        "If unknown, return empty arrays.\n\nPrompt:\n"
        + str(raw_prompt or "")
    )
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as summary_file:
        summary_path = summary_file.name
    cmd = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--json",
        "--output-last-message",
        summary_path,
    ]
    proc = subprocess.run(
        cmd,
        input=instruction,
        text=True,
        capture_output=True,
        check=False,
        timeout=int(os.getenv("XYN_APPSPEC_SEMANTIC_TIMEOUT_SECONDS", "45")),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"codex exited {proc.returncode}")
    try:
        with open(summary_path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    finally:
        try:
            os.unlink(summary_path)
        except OSError:
            pass
    payload = json.loads(raw or "{}")
    return payload if isinstance(payload, dict) else {}


def extract_semantic_inference(
    raw_prompt: str,
    *,
    prefer_llm: bool = False,
    force_llm: bool = False,
) -> dict[str, Any]:
    payload, _ = extract_semantic_inference_with_diagnostics(
        raw_prompt,
        prefer_llm=prefer_llm,
        force_llm=force_llm,
    )
    return payload


def extract_semantic_inference_with_diagnostics(
    raw_prompt: str,
    *,
    prefer_llm: bool = False,
    force_llm: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    llm_enabled = str(os.getenv("XYN_APPSPEC_ENABLE_LLM_FALLBACK", "")).strip().lower() in {"1", "true", "yes", "on"}
    codex_bin = _semantic_codex_binary()
    codex_available = _semantic_codex_available(codex_bin)
    use_llm = force_llm or (prefer_llm and llm_enabled and codex_available)
    payload: dict[str, Any]
    payload_from_llm = False
    fallback_used = False
    repair_used = False
    capability_state = _semantic_capability_state(
        prefer_llm=prefer_llm,
        force_llm=force_llm,
        llm_enabled=llm_enabled,
        codex_available=codex_available,
    )
    limited_mode = capability_state == "limited_no_llm"
    limited_reason = _limited_mode_reason(llm_enabled=llm_enabled, codex_available=codex_available)
    if force_llm and not llm_enabled:
        raise RuntimeError("LLM semantic extraction forced but XYN_APPSPEC_ENABLE_LLM_FALLBACK is disabled")
    if force_llm and not codex_available:
        raise RuntimeError("LLM semantic extraction forced but codex executable unavailable")
    if use_llm:
        try:
            payload = _extract_via_codex(raw_prompt)
            payload_from_llm = True
        except Exception:
            if force_llm:
                raise
            payload = _heuristic_semantic_extract(raw_prompt)
            fallback_used = True
    else:
        payload = _heuristic_semantic_extract(raw_prompt)
    if payload_from_llm and not _payload_types_valid(payload):
        payload = _heuristic_semantic_extract(raw_prompt)
        fallback_used = True
        repair_used = True
    normalized = _normalize_semantic_payload(payload)
    try:
        validate(instance=normalized, schema=_SEMANTIC_SCHEMA)
    except ValidationError:
        normalized = _normalize_semantic_payload(_heuristic_semantic_extract(raw_prompt))
        fallback_used = True
        repair_used = True
    diagnostics = {
        "llm_used": bool(payload_from_llm),
        "fallback_used": bool(fallback_used),
        "repair_used": bool(repair_used),
        "capability_state": capability_state,
        "limited_mode": bool(limited_mode),
        "limited_mode_reason": limited_reason if limited_mode else "",
        "llm_enabled": bool(llm_enabled),
        "codex_available": bool(codex_available),
    }
    return normalized, diagnostics
