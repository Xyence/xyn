from __future__ import annotations

import json
import os
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

class SemanticPlanningError(RuntimeError):
    """Base class for semantic planning failures."""


class SemanticPlanningAgentUnavailableError(SemanticPlanningError):
    """Raised when semantic planning cannot invoke the planning agent."""


class SemanticPlanningResponseValidationError(SemanticPlanningError):
    """Raised when planning-agent output does not satisfy the semantic schema."""


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


def _normalize_planning_agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
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
    return {
        "entities": _normalize_unique_strings(entities),
        "entity_contracts": contracts,
        "requested_visuals": visuals,
    }


def _planning_agent_payload_types_valid(payload: dict[str, Any]) -> bool:
    if "entities" in payload and not isinstance(payload.get("entities"), list):
        return False
    if "entity_contracts" in payload and not isinstance(payload.get("entity_contracts"), list):
        return False
    if "requested_visuals" in payload and not isinstance(payload.get("requested_visuals"), list):
        return False
    return True


def _invoke_semantic_planning_agent(raw_prompt: str) -> dict[str, Any]:
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
    capability_state = _semantic_capability_state(
        prefer_llm=prefer_llm,
        force_llm=force_llm,
        llm_enabled=llm_enabled,
        codex_available=codex_available,
    )
    limited_mode = capability_state == "limited_no_llm"
    limited_reason = _limited_mode_reason(llm_enabled=llm_enabled, codex_available=codex_available)
    if not use_llm:
        raise SemanticPlanningAgentUnavailableError(
            f"Semantic planning agent unavailable ({limited_reason}). "
            "No deterministic fallback planning is permitted."
        )
    try:
        payload = _invoke_semantic_planning_agent(raw_prompt)
    except Exception as exc:
        raise SemanticPlanningError(f"Semantic planning agent invocation failed: {exc}") from exc
    if not _planning_agent_payload_types_valid(payload):
        raise SemanticPlanningResponseValidationError(
            "Semantic planning output has invalid top-level field types."
        )
    normalized = _normalize_planning_agent_payload(payload)
    try:
        validate(instance=normalized, schema=_SEMANTIC_SCHEMA)
    except ValidationError as exc:
        raise SemanticPlanningResponseValidationError(
            f"Semantic planning output failed schema validation: {exc.message}"
        ) from exc
    diagnostics = {
        "llm_used": True,
        "fallback_used": False,
        "repair_used": False,
        "capability_state": capability_state,
        "limited_mode": bool(limited_mode),
        "limited_mode_reason": limited_reason if limited_mode else "",
        "llm_enabled": bool(llm_enabled),
        "codex_available": bool(codex_available),
    }
    return normalized, diagnostics
