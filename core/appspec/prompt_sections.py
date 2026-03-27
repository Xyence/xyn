from __future__ import annotations

import re
from typing import Any

from core.appspec.normalization import _normalize_unique_strings, _safe_slug, _title_case_words
from core.appspec.primitive_inference import _infer_primitives_from_text


def _extract_objective_sections(objective: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {
        "core_entities": [],
        "behavior": [],
        "views": [],
        "validation": [],
    }
    text = re.sub(r"\s+", " ", str(objective or "")).strip()
    if not text:
        return sections
    section_patterns = {
        "core_entities": re.compile(
            r"core entities\s*:\s*(.*?)(?=\bbehavior\s*:|\bviews\s*/\s*usability\s*:|\bviews\s*:|\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "behavior": re.compile(
            r"behavior\s*:\s*(.*?)(?=\bviews\s*/\s*usability\s*:|\bviews\s*:|\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "views": re.compile(
            r"(?:views\s*/\s*usability|views)\s*:\s*(.*?)(?=\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "validation": re.compile(r"(?:validation\s*/\s*rules|validation)\s*:\s*(.*)$", re.IGNORECASE),
    }
    for section_name, pattern in section_patterns.items():
        match = pattern.search(text)
        if not match:
            continue
        body = str(match.group(1) or "").strip()
        if not body:
            continue
        if section_name == "core_entities":
            sections[section_name].extend(part.strip() for part in re.split(r"\s+(?=\d+\.)", body) if part.strip())
            continue
        sections[section_name].extend(
            re.sub(r"^\s*[-*]\s*", "", part).strip()
            for part in re.split(r"\s+-\s+", body)
            if re.sub(r"^\s*[-*]\s*", "", part).strip()
        )
    return sections


def _extract_prompt_sections(raw_prompt: str) -> dict[str, str]:
    text = str(raw_prompt or "").replace("\r\n", "\n")
    if not text.strip():
        return {}
    lines = text.split("\n")
    sections: dict[str, list[str]] = {}
    current_heading = "__preamble__"
    sections[current_heading] = []
    heading_re = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
    label_re = re.compile(r"^\s*([A-Z][A-Za-z0-9 /()'\"_-]{2,})\s*:\s*$")
    for line in lines:
        heading_match = heading_re.match(line)
        if heading_match:
            current_heading = str(heading_match.group(1) or "").strip().lower()
            sections.setdefault(current_heading, [])
            continue
        label_match = label_re.match(line)
        if label_match and not line.strip().startswith("-"):
            current_heading = str(label_match.group(1) or "").strip().lower()
            sections.setdefault(current_heading, [])
            continue
        sections.setdefault(current_heading, []).append(line)
    cleaned: dict[str, str] = {}
    for heading, body_lines in sections.items():
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        cleaned[str(heading or "").strip().lower()] = body
    return cleaned


def _pick_prompt_section(sections: dict[str, str], *candidates: str) -> str:
    for candidate in candidates:
        token = str(candidate or "").strip().lower()
        if not token:
            continue
        for key, value in sections.items():
            if token in key and str(value or "").strip():
                return str(value).strip()
    return ""


def _extract_workflow_blocks_from_prompt(raw_prompt: str) -> list[dict[str, Any]]:
    sections = _extract_prompt_sections(raw_prompt)
    blocks: list[dict[str, Any]] = []
    for heading, body in sections.items():
        if "workflow" not in heading:
            continue
        workflow_key = _safe_slug(heading.replace("workflow", "").strip() or heading, default="workflow")
        blocks.append(
            {
                "workflow_key": workflow_key,
                "workflow_label": _title_case_words(heading.replace("workflow", "").strip() or heading),
                "description": str(body).strip(),
                "requires_primitives": _infer_primitives_from_text(body),
            }
        )
    return blocks


def _build_structured_plan_snapshot(raw_prompt: str) -> dict[str, Any]:
    sections = _extract_prompt_sections(raw_prompt)
    workflow_blocks = _extract_workflow_blocks_from_prompt(raw_prompt)
    snapshot = {
        "application_overview": _pick_prompt_section(sections, "application overview", "purpose"),
        "domain_model": _pick_prompt_section(sections, "domain model", "property model", "signal model"),
        "workflow_definitions": workflow_blocks,
        "platform_primitive_composition": [
            {
                "workflow_key": str(item.get("workflow_key") or ""),
                "workflow_label": str(item.get("workflow_label") or ""),
                "requires_primitives": _normalize_unique_strings(item.get("requires_primitives") if isinstance(item.get("requires_primitives"), list) else []),
            }
            for item in workflow_blocks
            if isinstance(item, dict)
        ],
        "evaluation_semantics": _pick_prompt_section(sections, "evaluation semantics", "changed-data and evaluation semantics"),
        "admin_user_separation": _pick_prompt_section(sections, "admin vs user separation", "role separation", "admin/operator workflow", "end-user workflow"),
        "ui_surfaces": _pick_prompt_section(sections, "ui surface", "ui expectations", "mvp ui", "ui with at least"),
        "configurability": _pick_prompt_section(sections, "configurability", "campaign constraints"),
        "explicit_exclusions": _pick_prompt_section(sections, "explicit exclusions"),
    }
    if not any(str(value or "").strip() for key, value in snapshot.items() if key != "workflow_definitions" and key != "platform_primitive_composition") and not workflow_blocks:
        return {}
    return snapshot
