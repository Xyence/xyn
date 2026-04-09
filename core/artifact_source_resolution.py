"""Resolve artifact source roots and materialize inspectable source files."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from core.artifact_code_review import detect_language
from core.artifact_provenance import extract_provenance_metadata, merge_provenance_metadata


_DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".xyn",
    "dist",
    "build",
}
_DEFAULT_MAX_FILE_BYTES = int(os.getenv("XYN_SOURCE_REVIEW_MAX_FILE_BYTES", "1048576"))
_DEFAULT_MAX_TOTAL_BYTES = int(os.getenv("XYN_SOURCE_REVIEW_MAX_TOTAL_BYTES", "26214400"))
_DEFAULT_MAX_FILES = int(os.getenv("XYN_SOURCE_REVIEW_MAX_FILES", "5000"))


@dataclass(frozen=True)
class ResolvedArtifactSource:
    files: dict[str, bytes]
    source_mode: str
    source_origin: str
    resolution_branch: str
    resolution_details: dict[str, Any]
    provenance: dict[str, Any]
    resolved_source_roots: list[str]
    warnings: list[str]


def resolve_artifact_source(
    *,
    artifact_slug: str = "",
    artifact_id: str = "",
    source_ref_type: str = "",
    source_ref_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
    packaged_files: Optional[dict[str, bytes]] = None,
) -> ResolvedArtifactSource:
    warnings: list[str] = []
    normalized_metadata = merge_provenance_metadata(metadata if isinstance(metadata, dict) else {})
    provenance = extract_provenance_metadata(normalized_metadata)
    candidate_roots: list[Path] = []
    provenance_root_set: set[str] = set()
    source_origin = "filesystem_hint"
    resolution_branch = "filesystem_hint"
    resolution_details: dict[str, Any] = {
        "artifact_slug": str(artifact_slug or "").strip(),
        "artifact_id": str(artifact_id or "").strip(),
        "source_ref_type": str(source_ref_type or "").strip(),
        "source_ref_id": str(source_ref_id or "").strip(),
    }

    provenance_roots, provenance_warnings, provenance_details = _candidate_provenance_roots(provenance)
    resolution_details["provenance"] = provenance_details
    if provenance_roots:
        candidate_roots.extend(provenance_roots)
        provenance_root_set = {str(path.resolve()) for path in provenance_roots}
        source_origin = "mirror"
        resolution_branch = "provenance_backed"
    warnings.extend(provenance_warnings)

    hint_roots = _candidate_source_roots(
        artifact_slug=artifact_slug,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        metadata=normalized_metadata,
    )
    resolution_details["filesystem_hint_roots"] = [str(path.resolve()) for path in hint_roots]
    for root in hint_roots:
        if root not in candidate_roots:
            candidate_roots.append(root)

    files, selected_roots, scan_warnings = _read_source_roots(candidate_roots)
    selected_root_set = {str(Path(item).resolve()) for item in selected_roots}
    resolution_details["candidate_roots"] = [
        {
            "path": str(path.resolve()),
            "selected": str(path.resolve()) in selected_root_set,
            "origin": "provenance_backed" if str(path.resolve()) in provenance_root_set else "filesystem_hint",
        }
        for path in candidate_roots
    ]
    resolution_details["selected_source_roots"] = list(selected_roots)
    warnings.extend(scan_warnings)
    if files:
        if provenance_roots and selected_roots:
            if selected_root_set.intersection(provenance_root_set):
                repo_url = str((provenance.get("source") or {}).get("repo_url") or "").strip()
                source_origin = "github" if repo_url else "mirror"
                resolution_branch = "provenance_backed"
            else:
                source_origin = "filesystem_hint"
                resolution_branch = "filesystem_hint"
        elif selected_roots:
            resolution_branch = "filesystem_hint"
        resolution_details["selected_branch"] = resolution_branch
        return ResolvedArtifactSource(
            files=files,
            source_mode="resolved_source",
            source_origin=source_origin,
            resolution_branch=resolution_branch,
            resolution_details=resolution_details,
            provenance=provenance,
            resolved_source_roots=selected_roots,
            warnings=warnings,
        )
    if packaged_files:
        warnings.append(
            "Falling back to packaged artifact files because deterministic/provenance source resolution failed."
        )
        resolution_branch = "packaged_fallback"
        resolution_details["selected_branch"] = resolution_branch
        return ResolvedArtifactSource(
            files=dict(packaged_files),
            source_mode="packaged_fallback",
            source_origin="packaged_fallback",
            resolution_branch=resolution_branch,
            resolution_details=resolution_details,
            provenance=provenance,
            resolved_source_roots=[],
            warnings=warnings,
        )
    warnings.append("No source files could be resolved from provenance hints, filesystem hints, or packaged payload.")
    resolution_branch = "packaged_fallback"
    resolution_details["selected_branch"] = resolution_branch
    return ResolvedArtifactSource(
        files={},
        source_mode="packaged_fallback",
        source_origin="packaged_fallback",
        resolution_branch=resolution_branch,
        resolution_details=resolution_details,
        provenance=provenance,
        resolved_source_roots=[],
        warnings=warnings,
    )


def _candidate_provenance_roots(provenance: dict[str, Any]) -> tuple[list[Path], list[str], dict[str, Any]]:
    warnings: list[str] = []
    details: dict[str, Any] = {}
    source = provenance.get("source") if isinstance(provenance.get("source"), dict) else {}
    if not source:
        details["reason"] = "missing_source_block"
        return [], warnings, details
    if str(source.get("kind") or "").strip().lower() != "git":
        details["reason"] = "non_git_source"
        details["source_kind"] = str(source.get("kind") or "")
        return [], warnings, details

    repo_key = str(source.get("repo_key") or "").strip()
    repo_url = str(source.get("repo_url") or "").strip()
    commit_sha = str(source.get("commit_sha") or "").strip()
    monorepo_subpath = str(source.get("monorepo_subpath") or "").strip()
    branch_hint = str(source.get("branch_hint") or "").strip()
    manifest_ref = str(source.get("manifest_ref") or "").strip()
    details.update(
        {
            "repo_key": repo_key,
            "repo_url": repo_url,
            "commit_sha": commit_sha,
            "branch_hint": branch_hint,
            "monorepo_subpath": monorepo_subpath,
            "manifest_ref": manifest_ref,
        }
    )

    if not repo_key and repo_url:
        repo_key = _repo_key_from_url(repo_url)
        details["repo_key"] = repo_key
    if not repo_key:
        warnings.append("Provenance source missing repo_key/repo_url; cannot deterministically resolve source root.")
        details["reason"] = "missing_repo_key"
        return [], warnings, details

    details["candidate_repo_roots"] = []
    candidates: list[Path] = []
    seen: set[str] = set()
    for token, origin in _provenance_repo_root_candidates(repo_key):
        candidate = token.resolve()
        exists = candidate.exists()
        is_dir = candidate.is_dir() if exists else False
        (details["candidate_repo_roots"]).append(
            {"path": str(candidate), "origin": origin, "exists": bool(exists), "is_dir": bool(is_dir)}
        )
        if not exists or not is_dir:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    details["resolved_repo_roots"] = [str(item) for item in candidates]

    if not candidates:
        detail = f"repo_key={repo_key}"
        if commit_sha:
            detail += f" commit_sha={commit_sha}"
        if branch_hint:
            detail += f" branch_hint={branch_hint}"
        warnings.append(
            "No local mirror/checkout found for provenance-backed source resolution "
            f"({detail}). GitHub/mirror fetch integration is not implemented in this phase."
        )
        details["reason"] = "repo_roots_missing"
        return [], warnings, details

    roots: list[Path] = []
    path_candidates: list[str] = []
    if monorepo_subpath:
        safe = _safe_subpath(monorepo_subpath)
        if not safe:
            warnings.append(f"Ignoring unsafe monorepo_subpath from provenance: {monorepo_subpath}")
        else:
            path_candidates.append(safe)
    if manifest_ref:
        safe_manifest = _safe_subpath(manifest_ref)
        if safe_manifest:
            manifest_path = Path(safe_manifest)
            if manifest_path.suffix:
                parent = manifest_path.parent.as_posix()
                if parent and parent != ".":
                    path_candidates.append(parent)
            else:
                path_candidates.append(safe_manifest)
    if not path_candidates:
        path_candidates.append("")
    details["path_candidates"] = list(path_candidates)

    details["candidate_source_roots"] = []
    for repo_root in candidates:
        for rel in path_candidates:
            target = repo_root if not rel else (repo_root / rel).resolve()
            exists = target.exists()
            is_dir = target.is_dir() if exists else False
            details["candidate_source_roots"].append(
                {"path": str(target), "exists": bool(exists), "is_dir": bool(is_dir), "repo_root": str(repo_root)}
            )
            if exists and is_dir:
                roots.append(target)

    if not roots:
        warnings.append(
            "Provenance repo root resolved but monorepo_subpath/manifest_ref directory was not found; "
            "falling back to filesystem hints."
        )
        details["reason"] = "provenance_subpath_missing"
    else:
        details["reason"] = "resolved"
    details["resolved_source_roots"] = [str(item) for item in roots]
    return roots, warnings, details


def _candidate_source_roots(
    *,
    artifact_slug: str,
    source_ref_type: str,
    source_ref_id: str,
    metadata: Optional[dict[str, Any]],
) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()

    def add(path: str | Path) -> None:
        token = str(path or "").strip()
        if not token:
            return
        candidate = Path(token).expanduser()
        if not candidate.is_absolute():
            for base in _base_roots():
                maybe = (base / candidate).resolve()
                _add_if_exists(maybe, out, seen)
            return
        _add_if_exists(candidate.resolve(), out, seen)

    meta = metadata if isinstance(metadata, dict) else {}
    for key in ("source_root", "source_path", "repo_path", "workspace_path", "manifest_ref", "manifest_path"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            add(value)
    for key in ("source_roots", "pythonpath"):
        value = meta.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    add(item)

    manifest = meta.get("manifest")
    if isinstance(manifest, dict):
        _collect_manifest_paths(manifest, add=add)

    content_ref = meta.get("content_ref")
    if isinstance(content_ref, dict):
        for key in ("path", "root", "repo_path", "source_root", "manifest_ref"):
            value = content_ref.get(key)
            if isinstance(value, str) and value.strip():
                add(value)

    src_type = str(source_ref_type or "").strip().lower()
    src_id = str(source_ref_id or "").strip()
    if src_type in {"path", "dir", "directory"} and src_id:
        add(src_id)
    if src_type in {"repo", "repository"} and src_id:
        for base in _base_roots():
            add(base / src_id)
    if src_type in {"module", "service", "app", "application"} and src_id:
        for candidate in _slug_candidates(src_id):
            add(candidate)

    for candidate in _slug_candidates(artifact_slug):
        add(candidate)

    return out


def _collect_manifest_paths(manifest: dict[str, Any], *, add) -> None:
    artifact = manifest.get("artifact") if isinstance(manifest.get("artifact"), dict) else {}
    metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
    for key in ("manifest_ref", "manifest_path", "source_root", "source_path", "repo_path"):
        value = manifest.get(key)
        if isinstance(value, str) and value.strip():
            add(value)
    for key in ("manifest_ref", "manifest_path", "source_root", "source_path", "repo_path"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            add(value)
    roles = manifest.get("roles") if isinstance(manifest.get("roles"), list) else []
    for role in roles:
        if not isinstance(role, dict):
            continue
        pythonpath = role.get("pythonpath")
        if isinstance(pythonpath, list):
            for item in pythonpath:
                if isinstance(item, str) and item.strip():
                    add(item)
        static_dir = role.get("static_dir")
        if isinstance(static_dir, str) and static_dir.strip():
            add(static_dir)


def _slug_candidates(slug: str) -> list[Path]:
    token = str(slug or "").strip()
    if not token:
        return []
    normalized = token.replace(".", "/")
    out: list[Path] = []
    for base in _base_roots():
        out.extend(
            [
                base / token,
                base / normalized,
                base / "xyn-platform" / "services" / token,
                base / "xyn-platform" / "apps" / token,
                base / "services" / token,
                base / "apps" / token,
            ]
        )
    return out


def _safe_subpath(value: str) -> str:
    token = str(value or "").strip().replace("\\", "/")
    if not token:
        return ""
    path = Path(token)
    if path.is_absolute():
        return ""
    parts = [part for part in path.parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _repo_key_from_url(repo_url: str) -> str:
    token = str(repo_url or "").strip().rstrip("/")
    if not token:
        return ""
    # Supports https://.../org/repo(.git) and git@host:org/repo(.git)
    if ":" in token and token.startswith("git@"):
        token = token.split(":", 1)[-1]
    else:
        token = token.split("/")[-1]
        return token[:-4] if token.endswith(".git") else token
    tail = token.split("/")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _provenance_repo_root_candidates(repo_key: str) -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    runtime_map_raw = str(os.getenv("XYN_RUNTIME_REPO_MAP", "")).strip()
    if runtime_map_raw:
        try:
            parsed = json.loads(runtime_map_raw)
            if isinstance(parsed, dict):
                value = parsed.get(repo_key)
                if isinstance(value, str):
                    out.append((Path(value).expanduser(), "runtime_repo_map"))
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, str) and item.strip():
                            out.append((Path(item).expanduser(), "runtime_repo_map"))
        except Exception:
            pass
    for base in _base_roots():
        out.extend(
            [
                (base / repo_key, "heuristic_base_repo_key"),
                (base / "xyn-platform" if repo_key == "xyn-platform" else base / repo_key, "heuristic_base_repo_key"),
            ]
        )
    deduped: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, origin in out:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((path, origin))
    return deduped


def _base_roots() -> list[Path]:
    roots: list[Path] = []
    raw = str(os.getenv("XYN_SOURCE_REVIEW_ROOTS", "")).strip()
    if raw:
        for item in _split_roots(raw):
            roots.append(Path(item).expanduser().resolve())
    kernel_roots = str(os.getenv("XYN_KERNEL_MANIFEST_ROOTS", "")).strip()
    if kernel_roots:
        for item in _split_roots(kernel_roots):
            roots.append(Path(item).expanduser().resolve())
    defaults = [
        Path("/workspace"),
        Path("/home/ubuntu/src"),
        Path.cwd(),
        Path(__file__).resolve().parents[1],
    ]
    for default in defaults:
        roots.append(default.resolve())
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(root)
    return deduped


def _split_roots(raw: str) -> list[str]:
    separators = [os.pathsep, ",", ";"]
    values = [raw]
    for sep in separators:
        next_values: list[str] = []
        for value in values:
            if sep in value:
                next_values.extend(value.split(sep))
            else:
                next_values.append(value)
        values = next_values
    return [item.strip() for item in values if item.strip()]


def _add_if_exists(path: Path, out: list[Path], seen: set[str]) -> None:
    if not path.exists() or not path.is_dir():
        return
    key = str(path)
    if key in seen:
        return
    seen.add(key)
    out.append(path)


def _read_source_roots(roots: Iterable[Path]) -> tuple[dict[str, bytes], list[str], list[str]]:
    files: dict[str, bytes] = {}
    selected_roots: list[str] = []
    warnings: list[str] = []
    total_bytes = 0
    file_count = 0

    for root in roots:
        root = root.resolve()
        if not root.exists() or not root.is_dir():
            continue
        root_added = False
        for path in root.rglob("*"):
            if file_count >= _DEFAULT_MAX_FILES:
                warnings.append(f"Source file limit reached ({_DEFAULT_MAX_FILES}); truncating scan.")
                return files, selected_roots, warnings
            if total_bytes >= _DEFAULT_MAX_TOTAL_BYTES:
                warnings.append(f"Source byte limit reached ({_DEFAULT_MAX_TOTAL_BYTES}); truncating scan.")
                return files, selected_roots, warnings
            if path.is_dir():
                if path.name in _DEFAULT_EXCLUDE_DIRS:
                    continue
                continue
            try:
                resolved = path.resolve(strict=True)
            except Exception:
                continue
            if not _is_path_within_root(resolved, root):
                continue
            if resolved.is_symlink():
                continue
            rel = resolved.relative_to(root).as_posix()
            if not rel or rel.startswith(".git/"):
                continue
            if any(part in _DEFAULT_EXCLUDE_DIRS for part in rel.split("/")):
                continue
            size = resolved.stat().st_size
            if size <= 0:
                continue
            if size > _DEFAULT_MAX_FILE_BYTES:
                continue
            language = detect_language(rel)
            if language == "binary":
                continue
            blob = _read_file_bytes(resolved)
            if blob is None:
                continue
            key = rel if len(selected_roots) <= 1 else f"{root.name}/{rel}"
            files[key] = blob
            total_bytes += len(blob)
            file_count += 1
            root_added = True
        if root_added:
            selected_roots.append(str(root))
    return files, selected_roots, warnings


def _is_path_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except Exception:
        return False


def _read_file_bytes(path: Path) -> Optional[bytes]:
    try:
        payload = path.read_bytes()
    except Exception:
        return None
    # Lightweight binary guard for false-positive extensions.
    if b"\x00" in payload[:4096]:
        return None
    return payload


def parse_packaged_artifact_metadata(files: dict[str, bytes]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    manifest_payload = _load_json_from_files(files, "manifest.json")
    if isinstance(manifest_payload, dict):
        metadata["manifest"] = manifest_payload
    for path, payload in files.items():
        normalized = str(path).replace("\\", "/")
        if not normalized.endswith("/artifact.json"):
            continue
        artifact_payload = _safe_json(payload)
        if not isinstance(artifact_payload, dict):
            continue
        inner_metadata = artifact_payload.get("metadata") if isinstance(artifact_payload.get("metadata"), dict) else {}
        content_ref = inner_metadata.get("content_ref") if isinstance(inner_metadata.get("content_ref"), dict) else {}
        for key in ("manifest_ref", "manifest_path", "source_root", "source_path", "repo_path"):
            value = inner_metadata.get(key)
            if isinstance(value, str) and value.strip():
                metadata[key] = value
        if content_ref:
            metadata["content_ref"] = content_ref
        # Preserve canonical provenance envelopes when present.
        source_block = inner_metadata.get("source") if isinstance(inner_metadata.get("source"), dict) else {}
        if source_block:
            metadata["source"] = source_block
        build_block = inner_metadata.get("build") if isinstance(inner_metadata.get("build"), dict) else {}
        if build_block:
            metadata["build"] = build_block
        provenance_block = inner_metadata.get("provenance") if isinstance(inner_metadata.get("provenance"), dict) else {}
        if provenance_block:
            metadata["provenance"] = provenance_block
        artifact_block = artifact_payload.get("artifact") if isinstance(artifact_payload.get("artifact"), dict) else {}
        for key in ("slug", "title", "type"):
            value = artifact_block.get(key)
            if isinstance(value, str) and value.strip():
                metadata.setdefault(key, value)
        break
    return merge_provenance_metadata(metadata)


def _load_json_from_files(files: dict[str, bytes], path: str) -> Any:
    payload = files.get(path)
    if payload is None:
        return None
    return _safe_json(payload)


def _safe_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return None
