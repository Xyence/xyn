from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


FALLBACK_CONTEXT_PACKS: list[dict[str, Any]] = [
    {
        "slug": "xyn-console-default",
        "title": "Xyn Console Default",
        "purpose": "any",
        "scope": "global",
        "capabilities": ["palette", "assistant", "artifact-navigation"],
        "bind_by_default": True,
        "description": "Default pack for palette and assistant command execution inside Xyn.",
        "content_format": "json",
        "content": {
            "intent": "default_assistant",
            "guidance": [
                "Use explicit artifact and workspace context.",
                "Prefer governed artifact references over hidden local state.",
                "Treat palette execution as deterministic command routing unless an agent is explicitly invoked.",
            ],
        },
    },
    {
        "slug": "xyn-planner-canon",
        "title": "Xyn Planner Canon",
        "purpose": "planner",
        "scope": "global",
        "capabilities": ["app-builder", "draft-generation", "app-spec"],
        "bind_by_default": True,
        "description": "Default pack for app-intent drafting and AppSpec generation.",
        "content_format": "json",
        "content": {
            "intent": "app_builder",
            "guidance": [
                "Create a durable draft first.",
                "Generate AppSpec artifacts before deployment.",
                "Treat cross-instance portability as requiring published artifacts or explicit import bundles.",
            ],
        },
    },
]


def default_context_pack_manifest_path() -> str:
    return str(os.getenv("XYN_CONTEXT_PACK_MANIFEST_PATH", ".xyn/sync/context-packs.manifest.json")).strip() or ".xyn/sync/context-packs.manifest.json"


def normalize_context_pack_definitions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        slug = str(row.get("slug") or "").strip().lower()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        normalized.append(
            {
                "slug": slug,
                "title": str(row.get("title") or slug).strip() or slug,
                "description": str(row.get("description") or "").strip(),
                "purpose": str(row.get("purpose") or "any").strip() or "any",
                "scope": str(row.get("scope") or "global").strip() or "global",
                "version": str(row.get("version") or "1.0.0").strip() or "1.0.0",
                "capabilities": [str(item) for item in (row.get("capabilities") or []) if str(item).strip()],
                "bind_by_default": bool(row.get("bind_by_default", False)),
                "content_format": str(row.get("content_format") or "markdown").strip() or "markdown",
                "content": row.get("content") if row.get("content") is not None else "",
                "applies_to_json": row.get("applies_to_json") if isinstance(row.get("applies_to_json"), dict) else {},
            }
        )
    return normalized


def load_authoritative_context_pack_definitions() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manifest_path = Path(default_context_pack_manifest_path()).expanduser()
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows = payload.get("context_packs") if isinstance(payload.get("context_packs"), list) else []
            normalized = normalize_context_pack_definitions([row for row in rows if isinstance(row, dict)])
            if normalized:
                return normalized, {
                    "source_system": str(payload.get("source_system") or "xyn-platform"),
                    "manifest_version": str(payload.get("manifest_version") or "xyn.context-pack-runtime-manifest.v1"),
                    "source_seed_pack_slug": str(payload.get("source_seed_pack_slug") or ""),
                    "source_seed_pack_version": str(payload.get("source_seed_pack_version") or ""),
                    "manifest_path": str(manifest_path),
                    "fallback_used": False,
                }
        except Exception:
            pass
    return normalize_context_pack_definitions(FALLBACK_CONTEXT_PACKS), {
        "source_system": "xyn-core-fallback",
        "manifest_version": "builtin-fallback",
        "source_seed_pack_slug": "",
        "source_seed_pack_version": "",
        "manifest_path": str(manifest_path),
        "fallback_used": True,
    }
