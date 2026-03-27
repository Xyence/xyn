from __future__ import annotations

import re
from typing import Any


def _safe_slug(value: str, *, default: str = "app") -> str:
    raw = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in str(value or "").lower())
    collapsed = "-".join(part for part in raw.split("-") if part)
    return collapsed or default


def _normalize_unique_strings(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _title_case_words(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[\s_]+", str(value or "").strip()) if part)


def _pluralize_label(value: str) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    if not lower:
        return "records"
    if lower.endswith("y") and lower[-2:] not in {"ay", "ey", "iy", "oy", "uy"}:
        return f"{text[:-1]}ies"
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return f"{text}es"
    return f"{text}s"
