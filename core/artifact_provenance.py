"""Canonical artifact provenance helpers.

This module keeps provenance additive and metadata-backed for compatibility.
"""
from __future__ import annotations

import re
from typing import Any


_HEX_RE = re.compile(r"^[0-9a-f]+$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _non_empty(value: Any) -> str:
    return str(value or "").strip()


def _normalize_commit_sha(value: Any) -> str:
    raw = _non_empty(value).lower()
    if not raw:
        return ""
    if not (7 <= len(raw) <= 40):
        return ""
    if not _HEX_RE.match(raw):
        return ""
    return raw


def _normalize_image_digest(value: Any) -> str:
    raw = _non_empty(value).lower()
    if not raw:
        return ""
    if _SHA256_RE.match(raw):
        return raw
    return ""


def validate_provenance_payload(value: Any) -> list[str]:
    payload = _as_dict(value)
    source = _as_dict(payload.get("source"))
    build = _as_dict(payload.get("build"))
    errors: list[str] = []

    kind = _non_empty(source.get("kind")).lower()
    if kind and kind != "git":
        errors.append("source.kind must be 'git' when provided")

    for key in ("commit_sha",):
        raw = _non_empty(source.get(key))
        if raw and not _normalize_commit_sha(raw):
            errors.append(f"source.{key} must be a 7-40 character hex git SHA")

    built_from = _non_empty(build.get("built_from_commit_sha"))
    if built_from and not _normalize_commit_sha(built_from):
        errors.append("build.built_from_commit_sha must be a 7-40 character hex git SHA")

    digest = _non_empty(build.get("image_digest"))
    if digest and not _normalize_image_digest(digest):
        errors.append("build.image_digest must be in sha256:<64 hex> format")

    return errors


def normalize_provenance_payload(value: Any) -> dict[str, Any]:
    payload = _as_dict(value)
    source = _as_dict(payload.get("source"))
    build = _as_dict(payload.get("build"))

    out_source: dict[str, Any] = {}
    kind = _non_empty(source.get("kind")).lower()
    if kind == "git":
        out_source["kind"] = "git"

    repo_url = _non_empty(source.get("repo_url"))
    if repo_url:
        out_source["repo_url"] = repo_url
    repo_key = _non_empty(source.get("repo_key"))
    if repo_key:
        out_source["repo_key"] = repo_key
    commit_sha = _normalize_commit_sha(source.get("commit_sha"))
    if commit_sha:
        out_source["commit_sha"] = commit_sha
    branch_hint = _non_empty(source.get("branch_hint"))
    if branch_hint:
        out_source["branch_hint"] = branch_hint
    monorepo_subpath = _non_empty(source.get("monorepo_subpath"))
    if monorepo_subpath:
        out_source["monorepo_subpath"] = monorepo_subpath
    manifest_ref = _non_empty(source.get("manifest_ref"))
    if manifest_ref:
        out_source["manifest_ref"] = manifest_ref

    out_build: dict[str, Any] = {}
    pipeline_provider = _non_empty(build.get("pipeline_provider"))
    if pipeline_provider:
        out_build["pipeline_provider"] = pipeline_provider
    run_id = _non_empty(build.get("run_id"))
    if run_id:
        out_build["run_id"] = run_id
    image_ref = _non_empty(build.get("image_ref"))
    if image_ref:
        out_build["image_ref"] = image_ref
    image_digest = _normalize_image_digest(build.get("image_digest"))
    if image_digest:
        out_build["image_digest"] = image_digest
    built_from_commit_sha = _normalize_commit_sha(build.get("built_from_commit_sha"))
    if built_from_commit_sha:
        out_build["built_from_commit_sha"] = built_from_commit_sha

    out: dict[str, Any] = {}
    if out_source:
        # If repo-level fields exist and source.kind is omitted, assume git.
        if "kind" not in out_source and any(
            key in out_source for key in ("repo_url", "repo_key", "commit_sha", "branch_hint", "monorepo_subpath")
        ):
            out_source["kind"] = "git"
        out["source"] = out_source
    if out_build:
        out["build"] = out_build
    return out


def extract_provenance_metadata(metadata: Any) -> dict[str, Any]:
    meta = _as_dict(metadata)
    explicit = _as_dict(meta.get("provenance"))
    source = _as_dict(explicit.get("source"))
    build = _as_dict(explicit.get("build"))

    source = {**source, **_as_dict(meta.get("source"))}
    build = {**build, **_as_dict(meta.get("build"))}

    # Legacy top-level compatibility keys.
    legacy_source_map = {
        "repo_url": "repo_url",
        "repo_key": "repo_key",
        "commit_sha": "commit_sha",
        "branch_hint": "branch_hint",
        "manifest_ref": "manifest_ref",
        "source_ref_id": "repo_key",
        "repo_path": "monorepo_subpath",
    }
    for legacy_key, target_key in legacy_source_map.items():
        raw = _non_empty(meta.get(legacy_key))
        if raw and not _non_empty(source.get(target_key)):
            source[target_key] = raw

    legacy_build_map = {
        "pipeline_provider": "pipeline_provider",
        "pipeline_run_id": "run_id",
        "run_id": "run_id",
        "image_ref": "image_ref",
        "image_digest": "image_digest",
        "built_from_commit_sha": "built_from_commit_sha",
    }
    for legacy_key, target_key in legacy_build_map.items():
        raw = _non_empty(meta.get(legacy_key))
        if raw and not _non_empty(build.get(target_key)):
            build[target_key] = raw

    return normalize_provenance_payload({"source": source, "build": build})


def merge_provenance_metadata(metadata: Any, provenance: Any = None) -> dict[str, Any]:
    merged = _as_dict(metadata)
    extracted = extract_provenance_metadata(merged)
    incoming = normalize_provenance_payload(provenance)

    source = {**_as_dict(extracted.get("source")), **_as_dict(incoming.get("source"))}
    build = {**_as_dict(extracted.get("build")), **_as_dict(incoming.get("build"))}
    normalized = normalize_provenance_payload({"source": source, "build": build})
    if not normalized:
        return merged

    merged["provenance"] = normalized
    if isinstance(normalized.get("source"), dict):
        merged["source"] = dict(normalized["source"])
    if isinstance(normalized.get("build"), dict):
        merged["build"] = dict(normalized["build"])
    return merged

