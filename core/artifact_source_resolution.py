"""Resolve artifact source roots and materialize inspectable source files."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from core.artifact_code_review import detect_language


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
    candidate_roots = _candidate_source_roots(
        artifact_slug=artifact_slug,
        source_ref_type=source_ref_type,
        source_ref_id=source_ref_id,
        metadata=metadata,
    )
    files, selected_roots, scan_warnings = _read_source_roots(candidate_roots)
    warnings.extend(scan_warnings)
    if files:
        return ResolvedArtifactSource(
            files=files,
            source_mode="resolved_source",
            resolved_source_roots=selected_roots,
            warnings=warnings,
        )
    if packaged_files:
        warnings.append(
            "Falling back to packaged artifact files because no filesystem source roots were resolved."
        )
        return ResolvedArtifactSource(
            files=dict(packaged_files),
            source_mode="packaged_fallback",
            resolved_source_roots=[],
            warnings=warnings,
        )
    warnings.append("No source files could be resolved from filesystem roots or packaged payload.")
    return ResolvedArtifactSource(
        files={},
        source_mode="packaged_fallback",
        resolved_source_roots=[],
        warnings=warnings,
    )


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
    # Common mapping: xyn-api artifact often maps to the running backend codebase.
    if token in {"xyn-api", "xyn.api"}:
        out.append(Path(__file__).resolve().parents[1])
    return out


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
        artifact_block = artifact_payload.get("artifact") if isinstance(artifact_payload.get("artifact"), dict) else {}
        for key in ("slug", "title", "type"):
            value = artifact_block.get(key)
            if isinstance(value, str) and value.strip():
                metadata.setdefault(key, value)
        break
    return metadata


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
