"""Deterministic runtime repo resolution for Epic C."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List


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
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RepoResolutionBlocked(f"Invalid XYN_RUNTIME_REPO_MAP JSON: {exc}", "repo_map_invalid") from exc
    else:
        payload = DEFAULT_RUNTIME_REPO_MAP
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


def validate_runtime_repo_map_targets() -> List[str]:
    warnings: List[str] = []
    repo_map = runtime_repo_map()
    for repo_key, candidates in repo_map.items():
        resolved_candidates = [Path(path).expanduser().resolve() for path in candidates]
        valid = False
        invalid_reasons: List[str] = []
        for candidate in resolved_candidates:
            if not candidate.exists():
                invalid_reasons.append(f"{candidate} (missing)")
                continue
            if not candidate.is_dir():
                invalid_reasons.append(f"{candidate} (not_a_directory)")
                continue
            if not (candidate / ".git").exists():
                invalid_reasons.append(f"{candidate} (not_a_git_repo)")
                continue
            if not os.access(candidate, os.R_OK | os.X_OK):
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
