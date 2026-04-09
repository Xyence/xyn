"""Deterministic runtime repo resolution for Epic C."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


DEFAULT_RUNTIME_REPO_MAP = {
    "xyn": ["/workspace/xyn"],
    "xyn-platform": ["/workspace/xyn-platform"],
}


@dataclass(frozen=True)
class ResolvedRuntimeRepo:
    repo_key: str
    path: Path


class RepoResolutionError(RuntimeError):
    failure_reason = "repo_unreachable"
    escalation_reason = None


class RepoResolutionBlocked(RepoResolutionError):
    failure_reason = None

    def __init__(self, message: str, escalation_reason: str):
        super().__init__(message)
        self.escalation_reason = escalation_reason


class RepoResolutionFailed(RepoResolutionError):
    def __init__(self, message: str, failure_reason: str = "repo_unreachable"):
        super().__init__(message)
        self.failure_reason = failure_reason


def runtime_repo_map() -> Dict[str, List[Path]]:
    raw = str(os.getenv("XYN_RUNTIME_REPO_MAP", "")).strip()
    payload = _parse_runtime_repo_map(raw)
    result: Dict[str, List[Path]] = {}
    for repo_key, value in dict(payload or {}).items():
        if isinstance(value, str):
            entries = [value]
        elif isinstance(value, list):
            entries = [str(item) for item in value if str(item).strip()]
        else:
            raise RepoResolutionBlocked(f"Repo mapping for '{repo_key}' must be a string or list.", "repo_map_invalid")
        result[str(repo_key)] = [Path(item).expanduser().resolve() for item in entries]
    return result


def inspect_runtime_repo_map_targets() -> List[dict]:
    repo_map = runtime_repo_map()
    rows: List[dict] = []
    for repo_key, candidates in repo_map.items():
        for candidate in [Path(path).expanduser().resolve() for path in candidates]:
            exists = candidate.exists()
            is_dir = candidate.is_dir() if exists else False
            has_git = bool((candidate / ".git").exists()) if is_dir else False
            readable = bool(os.access(candidate, os.R_OK | os.X_OK)) if is_dir else False
            is_empty = False
            if is_dir and readable:
                try:
                    is_empty = next(candidate.iterdir(), None) is None
                except Exception:
                    is_empty = False
            rows.append(
                {
                    "repo_key": str(repo_key),
                    "path": str(candidate),
                    "exists": bool(exists),
                    "is_dir": bool(is_dir),
                    "is_empty": bool(is_empty),
                    "has_git": bool(has_git),
                    "readable": bool(readable),
                    "valid": bool(exists and is_dir and has_git and readable),
                }
            )
    return rows


def validate_runtime_repo_map_targets() -> List[str]:
    warnings: List[str] = []
    target_rows = inspect_runtime_repo_map_targets()
    by_repo: Dict[str, List[dict]] = {}
    for row in target_rows:
        by_repo.setdefault(str(row.get("repo_key") or ""), []).append(row)
    for repo_key, rows in by_repo.items():
        resolved_candidates = [Path(str(row.get("path") or "")).expanduser().resolve() for row in rows]
        valid = False
        invalid_reasons: List[str] = []
        for row in rows:
            candidate = str(row.get("path") or "")
            if not bool(row.get("exists")):
                invalid_reasons.append(f"{candidate} (missing)")
                continue
            if not bool(row.get("is_dir")):
                invalid_reasons.append(f"{candidate} (not_a_directory)")
                continue
            if bool(row.get("is_empty")):
                invalid_reasons.append(f"{candidate} (empty_directory)")
            if not bool(row.get("has_git")):
                invalid_reasons.append(f"{candidate} (not_a_git_repo)")
                continue
            if not bool(row.get("readable")):
                invalid_reasons.append(f"{candidate} (not_readable)")
                continue
            valid = True
            break
        if not valid:
            candidate_text = ", ".join(str(path) for path in resolved_candidates) or "(none)"
            reason_text = ", ".join(invalid_reasons) if invalid_reasons else "no candidates configured"
            warnings.append(
                f"Runtime repo map target missing for repo '{repo_key}'. candidates=[{candidate_text}] details=[{reason_text}]"
            )
    return warnings


def _parse_runtime_repo_map(raw: str) -> Dict[str, List[str]]:
    token = str(raw or "").strip()
    if not token:
        return dict(DEFAULT_RUNTIME_REPO_MAP)
    if token.startswith("{"):
        try:
            payload = json.loads(token)
        except json.JSONDecodeError as exc:
            raise RepoResolutionBlocked(f"Invalid XYN_RUNTIME_REPO_MAP JSON: {exc}", "repo_map_invalid") from exc
        if not isinstance(payload, dict):
            raise RepoResolutionBlocked("XYN_RUNTIME_REPO_MAP JSON must be an object.", "repo_map_invalid")
        return dict(payload)

    parsed_pairs: List[Tuple[str, str]] = []
    for part in token.replace(";", ",").split(","):
        entry = str(part or "").strip()
        if not entry:
            continue
        repo_key, sep, repo_path = entry.partition(":")
        key = str(repo_key or "").strip()
        value = str(repo_path or "").strip()
        if not sep or not key or not value:
            raise RepoResolutionBlocked(
                "XYN_RUNTIME_REPO_MAP must be JSON or 'repo_key:/path' pairs separated by commas.",
                "repo_map_invalid",
            )
        parsed_pairs.append((key, value))
    if not parsed_pairs:
        raise RepoResolutionBlocked("XYN_RUNTIME_REPO_MAP is empty after parsing.", "repo_map_invalid")
    result: Dict[str, List[str]] = {}
    for key, value in parsed_pairs:
        result.setdefault(key, []).append(value)
    return result


def resolve_runtime_repo(repo_ref: str) -> ResolvedRuntimeRepo:
    token = str(repo_ref or "").strip()
    if not token:
        raise RepoResolutionBlocked("Missing target repository.", "target_repo_missing")
    path_candidate = Path(token).expanduser()
    if path_candidate.is_absolute():
        return _validate_repo_path("absolute", path_candidate.resolve())
    repo_map = runtime_repo_map()
    if token not in repo_map:
        raise RepoResolutionFailed(f"Target repo '{token}' is not configured in the runtime repo map.")
    candidates = [_existing_path(path) for path in repo_map[token]]
    candidates = [path for path in candidates if path is not None]
    if not candidates:
        raise RepoResolutionFailed(f"Target repo '{token}' is not mounted in the runtime environment.")
    unique = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    if len(unique) > 1:
        raise RepoResolutionBlocked(f"Target repo '{token}' resolved to multiple mounted paths.", "target_repo_ambiguous")
    return _validate_repo_path(token, unique[0])


def _existing_path(path: Path) -> Path | None:
    return path if path.exists() else None


def _validate_repo_path(repo_key: str, path: Path) -> ResolvedRuntimeRepo:
    if not path.exists():
        raise RepoResolutionFailed(f"Target repo path '{path}' does not exist.")
    if not path.is_dir():
        raise RepoResolutionFailed(f"Target repo path '{path}' is not a directory.")
    if not (path / ".git").exists():
        raise RepoResolutionFailed(f"Target repo path '{path}' is not a git repository.")
    if not os.access(path, os.R_OK | os.X_OK):
        raise RepoResolutionFailed(f"Target repo path '{path}' is not readable by runtime user.")
    return ResolvedRuntimeRepo(repo_key=repo_key, path=path)
