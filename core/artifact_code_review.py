"""Artifact source inspection and lightweight code analysis helpers."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any, Optional


_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".md": "markdown",
    ".txt": "text",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".sql": "sql",
    ".sh": "shell",
    ".bash": "shell",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

_TEXT_LANGUAGE_SET = set(_LANGUAGE_BY_SUFFIX.values()) | {"xml"}

_MAX_TEXT_DECODE_BYTES = 4 * 1024 * 1024
_DEFAULT_READ_LINES = 400
_MAX_READ_LINES = 4000
_DEFAULT_SEARCH_LIMIT = 200
_MAX_SEARCH_LIMIT = 2000


class FilePathNotFoundError(KeyError):
    """Raised when a requested source path cannot be resolved."""

    def __init__(self, message: str = "file not found", *, candidate_paths: Optional[list[str]] = None):
        super().__init__(message)
        self.candidate_paths = list(candidate_paths or [])


def detect_language(path: str) -> str:
    suffix = Path(str(path or "")).suffix.lower()
    if suffix in _LANGUAGE_BY_SUFFIX:
        return _LANGUAGE_BY_SUFFIX[suffix]
    if suffix == ".xml":
        return "xml"
    return "binary"


def _is_text_language(language: str) -> bool:
    return str(language or "").strip().lower() in _TEXT_LANGUAGE_SET


def _line_count_bytes(payload: bytes) -> int:
    if not payload:
        return 0
    return payload.count(b"\n") + 1


def _safe_archive_path(name: str) -> Optional[str]:
    normalized = str(name or "").replace("\\", "/").strip().lstrip("/")
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def parse_artifact_source_files(*, artifact_name: str, artifact_bytes: bytes) -> dict[str, bytes]:
    """Return path->bytes from raw artifact payload.

    - Zip payloads expose all contained files.
    - Non-zip payloads are represented as a single file entry.
    """

    payload = bytes(artifact_bytes or b"")
    files: dict[str, bytes] = {}
    if payload:
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    safe_path = _safe_archive_path(member.filename)
                    if not safe_path:
                        continue
                    files[safe_path] = archive.read(member.filename)
        except zipfile.BadZipFile:
            files = {}
    if files:
        return files
    fallback_name = _safe_archive_path(artifact_name) or "artifact.bin"
    return {fallback_name: payload}


def build_source_index(
    files: dict[str, bytes],
    *,
    include_line_counts: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(files.keys()):
        blob = files[path]
        language = detect_language(path)
        line_count = None
        if include_line_counts and _is_text_language(language):
            line_count = _line_count_bytes(blob)
        rows.append(
            {
                "path": path,
                "kind": "file",
                "language": language,
                "size_bytes": len(blob),
                "line_count": line_count,
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
    return rows


def build_hierarchical_tree(index_rows: list[dict[str, Any]]) -> dict[str, Any]:
    root: dict[str, Any] = {"path": "/", "name": "/", "kind": "dir", "children": []}
    by_path: dict[str, dict[str, Any]] = {"/": root}
    for row in index_rows:
        rel_path = str(row.get("path") or "").strip().lstrip("/")
        if not rel_path:
            continue
        parts = rel_path.split("/")
        parent_path = "/"
        for idx, part in enumerate(parts):
            is_last = idx == len(parts) - 1
            node_path = "/" + "/".join(parts[: idx + 1])
            if node_path in by_path:
                parent_path = node_path
                continue
            if is_last:
                node = {
                    "path": node_path,
                    "name": part,
                    "kind": "file",
                    "language": row.get("language"),
                    "size_bytes": row.get("size_bytes"),
                    "line_count": row.get("line_count"),
                    "sha256": row.get("sha256"),
                }
            else:
                node = {"path": node_path, "name": part, "kind": "dir", "children": []}
            by_path[node_path] = node
            parent = by_path[parent_path]
            children = parent.get("children")
            if isinstance(children, list):
                children.append(node)
            parent_path = node_path
    _sort_tree(root)
    return root


def _sort_tree(node: dict[str, Any]) -> None:
    children = node.get("children")
    if not isinstance(children, list):
        return
    for child in children:
        if isinstance(child, dict):
            _sort_tree(child)
    children.sort(key=lambda row: (str(row.get("kind") or "") != "dir", str(row.get("name") or "").lower()))


def _decode_text(blob: bytes) -> Optional[str]:
    if len(blob) > _MAX_TEXT_DECODE_BYTES:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return blob.decode("utf-8", errors="replace")
        except Exception:
            return None


def read_file_chunk(
    *,
    files: dict[str, bytes],
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> dict[str, Any]:
    raw_path = str(path or "").replace("\\", "/").strip().lstrip("/")
    safe_path = _safe_archive_path(raw_path or "")
    if not safe_path:
        raise FilePathNotFoundError("file not found")
    resolved_path = _resolve_read_path(files=files, requested_path=safe_path)
    if resolved_path is None:
        candidates = _candidate_read_paths(files=files, requested_path=safe_path)
        raise FilePathNotFoundError("file not found", candidate_paths=candidates)
    blob = files[resolved_path]
    language = detect_language(resolved_path)
    text = _decode_text(blob)
    if text is None or not _is_text_language(language):
        raise ValueError("file is not readable text")
    lines = text.splitlines()
    total_lines = len(lines)
    resolved_start = max(1, int(start_line or 1))
    if end_line is None:
        resolved_end = min(total_lines, resolved_start + _DEFAULT_READ_LINES - 1)
    else:
        resolved_end = int(end_line)
    if resolved_end < resolved_start:
        raise ValueError("end_line must be >= start_line")
    if resolved_end - resolved_start + 1 > _MAX_READ_LINES:
        resolved_end = resolved_start + _MAX_READ_LINES - 1
    resolved_end = min(total_lines, max(resolved_start, resolved_end))
    selected = lines[resolved_start - 1 : resolved_end]
    return {
        "path": resolved_path,
        "language": language,
        "total_lines": total_lines,
        "returned_start_line": resolved_start,
        "returned_end_line": resolved_end,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "content": "\n".join(selected),
    }


def _resolve_read_path(*, files: dict[str, bytes], requested_path: str) -> Optional[str]:
    safe_path = _safe_archive_path(requested_path)
    if not safe_path:
        return None
    if safe_path in files:
        return safe_path
    candidates = _candidate_read_paths(files=files, requested_path=safe_path)
    if not candidates:
        return None
    # If multiple candidates have identical confidence score, require caller disambiguation.
    scored = _score_candidate_paths(candidates=candidates, requested_path=safe_path)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _candidate_read_paths(*, files: dict[str, bytes], requested_path: str) -> list[str]:
    safe_path = _safe_archive_path(requested_path)
    if not safe_path:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        token = str(path or "").strip()
        if not token or token in seen:
            return
        seen.add(token)
        out.append(token)

    # Backend/non-backend mirror compatibility.
    if safe_path.startswith("backend/"):
        mirrored = safe_path[len("backend/") :]
    else:
        mirrored = f"backend/{safe_path}"
    if mirrored in files:
        add(mirrored)

    # Suffix-aware matching for repo-root relative vs selected-root relative paths.
    for file_path in sorted(files.keys()):
        if file_path.endswith("/" + safe_path) or safe_path.endswith("/" + file_path):
            add(file_path)

    # Best-effort near match by basename when exact/suffix path differs.
    requested_name = Path(safe_path).name
    if requested_name:
        for file_path in sorted(files.keys()):
            if Path(file_path).name == requested_name:
                add(file_path)

    return out[:20]


def _score_candidate_paths(*, candidates: list[str], requested_path: str) -> list[tuple[tuple[int, int, int, int], str]]:
    requested = str(requested_path or "")
    requested_parts = [part for part in requested.split("/") if part]
    requested_backend = requested.startswith("backend/")
    scored: list[tuple[tuple[int, int, int, int], str]] = []
    for candidate in candidates:
        path = str(candidate)
        parts = [part for part in path.split("/") if part]
        suffix_match = int(not (path.endswith("/" + requested) or requested.endswith("/" + path)))
        backend_mismatch = int(path.startswith("backend/") != requested_backend)
        depth_delta = abs(len(parts) - len(requested_parts))
        tie_break = len(path)
        scored.append(((suffix_match, backend_mismatch, depth_delta, tie_break), path))
    scored.sort(key=lambda item: item[0])
    return scored


def search_files(
    *,
    files: dict[str, bytes],
    query: str,
    path_glob: Optional[str] = None,
    file_extensions: Optional[list[str]] = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = _DEFAULT_SEARCH_LIMIT,
) -> dict[str, Any]:
    token = str(query or "")
    if not token.strip():
        raise ValueError("query is required")
    resolved_limit = max(1, min(int(limit or _DEFAULT_SEARCH_LIMIT), _MAX_SEARCH_LIMIT))
    extension_set = {
        (ext if str(ext).startswith(".") else f".{ext}").lower()
        for ext in (file_extensions or [])
        if str(ext or "").strip()
    }
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(token, flags) if regex else None

    result_files: list[dict[str, Any]] = []
    total_hits = 0
    truncated = False
    for path in sorted(files.keys()):
        if path_glob and not fnmatch.fnmatch(path, path_glob):
            continue
        if extension_set and Path(path).suffix.lower() not in extension_set:
            continue
        language = detect_language(path)
        if not _is_text_language(language):
            continue
        text = _decode_text(files[path])
        if text is None:
            continue
        hits: list[dict[str, Any]] = []
        lines = text.splitlines()
        for idx, line in enumerate(lines, start=1):
            matched = bool(pattern.search(line)) if pattern else (token in line if case_sensitive else token.lower() in line.lower())
            if not matched:
                continue
            snippet = line.strip()
            if len(snippet) > 240:
                snippet = snippet[:240] + "..."
            hits.append({"line_number": idx, "snippet": snippet})
            total_hits += 1
            if total_hits >= resolved_limit:
                truncated = True
                break
        if hits:
            result_files.append(
                {
                    "path": path,
                    "language": language,
                    "hit_count": len(hits),
                    "hits": hits,
                }
            )
        if truncated:
            break
    return {
        "query": token,
        "regex": bool(regex),
        "case_sensitive": bool(case_sensitive),
        "limit": resolved_limit,
        "total_hits": total_hits,
        "truncated": truncated,
        "files": result_files,
    }


def compute_module_metrics(files: dict[str, bytes]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    module_paths: dict[str, str] = {}
    for path in files.keys():
        if path.endswith(".py"):
            module_paths[path.replace("/", ".")[:-3]] = path
    local_modules = set(module_paths.keys())
    fan_in_counts: dict[str, int] = {path: 0 for path in files.keys()}

    parsed_rows: list[dict[str, Any]] = []
    for path in sorted(files.keys()):
        blob = files[path]
        language = detect_language(path)
        row: dict[str, Any] = {
            "path": path,
            "language": language,
            "size_bytes": len(blob),
            "line_count": _line_count_bytes(blob) if _is_text_language(language) else None,
            "function_count": 0,
            "class_count": 0,
            "import_count": 0,
            "max_function_lines": None,
            "complexity_proxy": 0,
            "fan_out": 0,
            "fan_in": 0,
            "imports": [],
            "large_functions": [],
            "unused_import_candidates": [],
            "dead_code_candidates": [],
        }
        if language != "python":
            rows.append(row)
            continue
        text = _decode_text(blob) or ""
        try:
            tree = ast.parse(text)
        except Exception:
            row["parse_error"] = "python_parse_failed"
            rows.append(row)
            continue
        imports: list[str] = []
        imported_names: list[str] = []
        referenced_names: set[str] = set()
        function_defs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        class_defs: list[ast.ClassDef] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(str(alias.name))
                        imported_names.append(str(alias.asname or alias.name.split(".")[0]))
                else:
                    mod = str(node.module or "")
                    imports.append(mod)
                    for alias in node.names:
                        imported_names.append(str(alias.asname or alias.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_defs.append(node)
            elif isinstance(node, ast.ClassDef):
                class_defs.append(node)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                referenced_names.add(str(node.id))
        row["function_count"] = len(function_defs)
        row["class_count"] = len(class_defs)
        row["import_count"] = len(imports)
        unique_imports = sorted({item for item in imports if item})
        row["imports"] = unique_imports
        row["fan_out"] = len(unique_imports)

        complexity_nodes = (
            ast.If,
            ast.For,
            ast.AsyncFor,
            ast.While,
            ast.Try,
            ast.ExceptHandler,
            ast.With,
            ast.AsyncWith,
            ast.BoolOp,
            ast.IfExp,
            ast.Match,
        )
        row["complexity_proxy"] = sum(1 for node in ast.walk(tree) if isinstance(node, complexity_nodes))

        large_functions: list[dict[str, Any]] = []
        max_len = 0
        for fn in function_defs:
            start = getattr(fn, "lineno", 0) or 0
            end = getattr(fn, "end_lineno", start) or start
            length = max(0, int(end) - int(start) + 1)
            max_len = max(max_len, length)
            if length >= 80:
                large_functions.append({"name": str(fn.name), "start_line": int(start), "end_line": int(end), "line_count": int(length)})
            if str(fn.name).startswith("_"):
                ref_count = text.count(f"{fn.name}(")
                if ref_count <= 1:
                    row["dead_code_candidates"].append(
                        {"kind": "function", "name": str(fn.name), "line": int(start), "reason": "private function appears unreferenced in file"}
                    )
        row["max_function_lines"] = max_len or None
        row["large_functions"] = large_functions
        row["unused_import_candidates"] = sorted(
            {
                name
                for name in imported_names
                if name and name not in referenced_names and name != "*"
            }
        )
        for cls in class_defs:
            if str(cls.name).startswith("_"):
                ref_count = text.count(f"{cls.name}(") + text.count(f"class {cls.name}")
                if ref_count <= 1:
                    row["dead_code_candidates"].append(
                        {"kind": "class", "name": str(cls.name), "line": int(getattr(cls, "lineno", 0) or 0), "reason": "private class appears unreferenced in file"}
                    )
        parsed_rows.append({"path": path, "imports": unique_imports})
        rows.append(row)

    # Derive fan-in from local module imports.
    for parsed in parsed_rows:
        imports = parsed.get("imports") if isinstance(parsed.get("imports"), list) else []
        for imp in imports:
            token = str(imp or "").strip()
            if not token:
                continue
            target_path = module_paths.get(token)
            if not target_path:
                # best-effort prefix match for package imports
                for module_name, module_path in module_paths.items():
                    if token.startswith(module_name + "."):
                        target_path = module_path
                        break
            if target_path and target_path in fan_in_counts and target_path != parsed.get("path"):
                fan_in_counts[target_path] += 1
    for row in rows:
        row["fan_in"] = int(fan_in_counts.get(str(row.get("path") or ""), 0))
    rows.sort(key=lambda entry: (int(entry.get("line_count") or 0), int(entry.get("size_bytes") or 0)), reverse=True)
    return rows


def _severity_from_score(score: int) -> str:
    value = max(0, min(int(score), 100))
    if value >= 85:
        return "critical"
    if value >= 70:
        return "high"
    if value >= 40:
        return "medium"
    return "low"


def _extract_python_api_assessment(files: dict[str, bytes], metrics: list[dict[str, Any]]) -> dict[str, Any]:
    oversized_thresholds = [500, 1000, 3000, 10000]
    metrics_by_path = {str(row.get("path") or ""): row for row in metrics}
    oversized_by_threshold: dict[str, list[dict[str, Any]]] = {}
    for threshold in oversized_thresholds:
        rows = []
        for row in metrics:
            line_count = int(row.get("line_count") or 0)
            if line_count >= threshold:
                rows.append(
                    {
                        "path": str(row.get("path") or ""),
                        "line_count": line_count,
                        "language": str(row.get("language") or ""),
                    }
                )
        rows.sort(key=lambda item: int(item.get("line_count") or 0), reverse=True)
        oversized_by_threshold[str(threshold)] = rows[:500]

    fastapi_decorator_re = re.compile(
        r"@\s*(?P<owner>[A-Za-z_][\w\.]*)\.(?P<verb>get|post|put|delete|patch|options|head)\((?P<args>.*)\)"
    )
    django_path_re = re.compile(r"\b(re_path|path)\(\s*[\"']([^\"']+)[\"']")

    django_evidence: dict[str, list[dict[str, Any]]] = {
        "urls": [],
        "views": [],
        "models": [],
        "middleware": [],
        "settings": [],
        "drf": [],
    }
    fastapi_evidence: dict[str, list[dict[str, Any]]] = {
        "app_instances": [],
        "routers": [],
        "depends_usage": [],
        "pydantic_models": [],
    }
    route_inventory: list[dict[str, Any]] = []
    per_file_route_counts: dict[str, int] = {}
    files_with_both_frameworks: list[str] = []
    file_signals: dict[str, dict[str, bool]] = {}

    for path, blob in files.items():
        if detect_language(path) != "python":
            continue
        text = _decode_text(blob) or ""
        lines = text.splitlines()
        lower = text.lower()
        has_django = False
        has_fastapi = False
        has_drf = False

        if "django.urls" in lower or "urlpatterns" in lower or "re_path(" in lower or "path(" in lower:
            django_evidence["urls"].append({"path": path})
            has_django = True
        if "django.views" in lower or ".as_view(" in lower or "from django.shortcuts" in lower:
            django_evidence["views"].append({"path": path})
            has_django = True
        if "django.db.models" in lower or "models.model" in lower:
            django_evidence["models"].append({"path": path})
            has_django = True
        if "middleware" in lower and "django" in lower:
            django_evidence["middleware"].append({"path": path})
            has_django = True
        if "django.conf" in lower or "settings.py" in path.lower() or "settings." in lower:
            django_evidence["settings"].append({"path": path})
            has_django = True
        if "rest_framework" in lower or "apiview" in lower or "viewset" in lower or "serializer" in lower:
            django_evidence["drf"].append({"path": path})
            has_django = True
            has_drf = True

        if "fastapi(" in lower or "from fastapi import fastapi" in lower:
            fastapi_evidence["app_instances"].append({"path": path})
            has_fastapi = True
        if "apirouter(" in lower or "from fastapi import apirouter" in lower:
            fastapi_evidence["routers"].append({"path": path})
            has_fastapi = True
        if "depends(" in lower or "from fastapi import depends" in lower:
            fastapi_evidence["depends_usage"].append({"path": path})
            has_fastapi = True
        if "from pydantic import" in lower or "basemodel" in lower:
            fastapi_evidence["pydantic_models"].append({"path": path})

        route_count = 0
        for idx, line in enumerate(lines, start=1):
            dec_match = fastapi_decorator_re.search(line)
            if dec_match:
                owner = str(dec_match.group("owner") or "")
                verb = str(dec_match.group("verb") or "").upper()
                args = str(dec_match.group("args") or "")
                path_match = re.search(r"[\"']([^\"']+)[\"']", args)
                route_path = str(path_match.group(1)) if path_match else ""
                route_inventory.append(
                    {
                        "framework": "fastapi",
                        "method": verb,
                        "route": route_path,
                        "owner": owner,
                        "file": path,
                        "line": idx,
                    }
                )
                route_count += 1
                has_fastapi = True
                continue
            django_match = django_path_re.search(line)
            if django_match:
                route_inventory.append(
                    {
                        "framework": "django",
                        "method": "ANY",
                        "route": str(django_match.group(2) or ""),
                        "owner": str(django_match.group(1) or ""),
                        "file": path,
                        "line": idx,
                    }
                )
                route_count += 1
                has_django = True

        per_file_route_counts[path] = route_count
        if has_django and has_fastapi:
            files_with_both_frameworks.append(path)
        file_signals[path] = {"django": has_django, "fastapi": has_fastapi, "drf": has_drf}

    oversized_route_entries: list[dict[str, Any]] = []
    for route in route_inventory:
        file_path = str(route.get("file") or "")
        metric_row = metrics_by_path.get(file_path) or {}
        line_count = int(metric_row.get("line_count") or 0)
        if line_count >= 3000:
            oversized_route_entries.append({**route, "file_line_count": line_count})

    orchestration_candidates: list[dict[str, Any]] = []
    for row in metrics:
        path = str(row.get("path") or "")
        fan_out = int(row.get("fan_out") or 0)
        fan_in = int(row.get("fan_in") or 0)
        line_count = int(row.get("line_count") or 0)
        route_count = int(per_file_route_counts.get(path, 0))
        entrypoint_bonus = 25 if route_count > 0 else 0
        score = min(100, (fan_out * 2) + (fan_in * 3) + (entrypoint_bonus) + (line_count // 300))
        if score <= 0:
            continue
        orchestration_candidates.append(
            {
                "path": path,
                "score": score,
                "fan_in": fan_in,
                "fan_out": fan_out,
                "route_count": route_count,
                "line_count": line_count,
            }
        )
    orchestration_candidates.sort(key=lambda item: int(item.get("score") or 0), reverse=True)

    seam_keywords: dict[str, list[str]] = {
        "auth": ["auth", "login", "jwt", "token", "permission", "oauth", "session"],
        "content_generation": ["content", "prompt", "generation", "template", "render", "markdown", "llm"],
        "integrations": ["httpx", "requests", "boto3", "stripe", "twilio", "slack", "webhook", "client"],
        "admin": ["admin", "backoffice", "staff", "management", "moderation"],
        "background_jobs": ["celery", "rq", "apscheduler", "backgroundtask", "task(", "worker"],
        "schemas_models": ["schema", "serializer", "pydantic", "basemodel", "models.model", "sqlalchemy", "dataclass"],
        "utility_blobs": ["utils", "helpers", "common", "shared", "misc", "lib"],
        "legacy_migration_shims": ["legacy", "deprecated", "compat", "shim", "migration"],
    }
    seam_candidates: list[dict[str, Any]] = []
    for seam, keywords in seam_keywords.items():
        file_hits: list[dict[str, Any]] = []
        for path, blob in files.items():
            if detect_language(path) != "python":
                continue
            text = (_decode_text(blob) or "").lower()
            path_lower = path.lower()
            hit_count = 0
            matched: list[str] = []
            for keyword in keywords:
                keyword_lower = keyword.lower()
                count = text.count(keyword_lower) + path_lower.count(keyword_lower)
                if count > 0:
                    matched.append(keyword)
                    hit_count += count
            if hit_count <= 0:
                continue
            metric_row = metrics_by_path.get(path) or {}
            line_count = int(metric_row.get("line_count") or 0)
            file_hits.append(
                {
                    "path": path,
                    "signal_score": min(100, hit_count + (line_count // 600)),
                    "line_count": line_count,
                    "matched_keywords": sorted(set(matched)),
                }
            )
        if not file_hits:
            continue
        file_hits.sort(key=lambda item: int(item.get("signal_score") or 0), reverse=True)
        seam_candidates.append(
            {
                "seam": seam,
                "candidate_files": file_hits[:10],
                "confidence": "heuristic",
            }
        )

    python_rows = [row for row in metrics if str(row.get("language") or "") == "python"]
    largest_line_count = int(max((int(row.get("line_count") or 0) for row in python_rows), default=0))
    max_fan_out = int(max((int(row.get("fan_out") or 0) for row in python_rows), default=0))
    max_fan_in = int(max((int(row.get("fan_in") or 0) for row in python_rows), default=0))
    large_fn_count = sum(
        len(row.get("large_functions") or []) for row in python_rows if isinstance(row.get("large_functions"), list)
    )
    dead_code_count = sum(
        len(row.get("dead_code_candidates") or []) for row in python_rows if isinstance(row.get("dead_code_candidates"), list)
    )
    unused_import_count = sum(
        len(row.get("unused_import_candidates") or [])
        for row in python_rows
        if isinstance(row.get("unused_import_candidates"), list)
    )
    mixed_framework = bool(django_evidence["urls"] or django_evidence["views"] or django_evidence["models"]) and bool(
        fastapi_evidence["app_instances"] or fastapi_evidence["routers"] or fastapi_evidence["depends_usage"]
    )

    file_size_risk = min(100, int((largest_line_count / 10000.0) * 100))
    coupling_risk = min(100, (max_fan_out * 2) + (max_fan_in * 3))
    framework_mixing_risk = 100 if files_with_both_frameworks else (75 if mixed_framework else 0)
    dead_code_risk = min(100, int((dead_code_count * 8) + (unused_import_count * 2)))
    oversized_route_count = len(oversized_route_entries)
    change_safety_risk = min(100, int((file_size_risk * 0.35) + (coupling_risk * 0.35) + (oversized_route_count * 5)))
    ai_maintenance_risk = min(
        100,
        int((file_size_risk * 0.4) + (framework_mixing_risk * 0.25) + (coupling_risk * 0.2) + (large_fn_count * 0.8)),
    )

    risk_scores = {
        "file_size_risk": {"score": file_size_risk, "rating": _severity_from_score(file_size_risk)},
        "coupling_risk": {"score": coupling_risk, "rating": _severity_from_score(coupling_risk)},
        "framework_mixing_risk": {"score": framework_mixing_risk, "rating": _severity_from_score(framework_mixing_risk)},
        "dead_code_risk": {"score": dead_code_risk, "rating": _severity_from_score(dead_code_risk)},
        "change_safety_risk": {"score": change_safety_risk, "rating": _severity_from_score(change_safety_risk)},
        "ai_maintenance_risk": {"score": ai_maintenance_risk, "rating": _severity_from_score(ai_maintenance_risk)},
    }

    extraction_plan = [
        {
            "step": 1,
            "title": "Freeze API entrypoint behavior",
            "objective": "Reduce regression risk before structural changes.",
            "actions": [
                "Add smoke tests around high-traffic endpoints.",
                "Prevent new route additions to oversized entrypoint modules during extraction.",
            ],
        },
        {
            "step": 2,
            "title": "Extract routers/views by domain",
            "objective": "Move route declarations out of oversized modules first.",
            "actions": [
                "Create domain router/view modules (auth, content, admin, integrations).",
                "Keep compatibility imports in original entrypoint while migrating gradually.",
            ],
        },
        {
            "step": 3,
            "title": "Extract schemas/models",
            "objective": "Decouple request/response models from route handlers.",
            "actions": [
                "Move Pydantic/DRF serializers and ORM models into dedicated packages.",
                "Stabilize DTO naming and import contracts.",
            ],
        },
        {
            "step": 4,
            "title": "Extract service layer",
            "objective": "Reduce handler complexity and improve testability.",
            "actions": [
                "Move orchestration/integration logic into services.",
                "Keep handlers thin and declarative.",
            ],
        },
        {
            "step": 5,
            "title": "Remove dead imports and isolate framework glue",
            "objective": "Lower coupling and clarify runtime dependencies.",
            "actions": [
                "Clean unused imports and private dead-code candidates in small batches.",
                "Separate Django-specific and FastAPI-specific wiring into boundary modules.",
            ],
        },
        {
            "step": 6,
            "title": "Preserve compatibility shim and retire legacy paths",
            "objective": "Support safe incremental rollout.",
            "actions": [
                "Keep compatibility shims for moved symbols/routes until migration completes.",
                "Delete deprecated shim code only after call-site migration and test coverage updates.",
            ],
        },
    ]

    facts = {
        "python_file_count": len(python_rows),
        "largest_python_file_lines": largest_line_count,
        "route_count": len(route_inventory),
        "oversized_route_count": oversized_route_count,
        "mixed_framework_detected": mixed_framework,
        "files_with_both_frameworks": sorted(files_with_both_frameworks),
    }
    inferences = {
        "monolith_hotspots": [
            item for item in orchestration_candidates[:10] if int(item.get("line_count") or 0) >= 1000
        ],
        "top_refactor_seams": seam_candidates[:6],
        "risk_interpretation": {
            "highest_risk_dimensions": sorted(
                [
                    {"dimension": name, "score": payload["score"], "rating": payload["rating"]}
                    for name, payload in risk_scores.items()
                ],
                key=lambda item: int(item.get("score") or 0),
                reverse=True,
            )[:3]
        },
    }

    return {
        "oversized_file_report": {
            "thresholds": oversized_thresholds,
            "files_over_threshold": oversized_by_threshold,
            "api_entrypoint_candidates": orchestration_candidates[:20],
        },
        "framework_fingerprint": {
            "django": {
                "detected": bool(
                    django_evidence["urls"]
                    or django_evidence["views"]
                    or django_evidence["models"]
                    or django_evidence["middleware"]
                    or django_evidence["settings"]
                    or django_evidence["drf"]
                ),
                "evidence": django_evidence,
            },
            "fastapi": {
                "detected": bool(
                    fastapi_evidence["app_instances"] or fastapi_evidence["routers"] or fastapi_evidence["depends_usage"]
                ),
                "evidence": fastapi_evidence,
            },
            "mixed_framework_detected": mixed_framework,
            "files_with_both_frameworks": sorted(files_with_both_frameworks),
        },
        "route_inventory": {
            "count": len(route_inventory),
            "items": route_inventory[:2000],
            "routes_in_oversized_files": oversized_route_entries[:500],
        },
        "refactor_seam_candidates": seam_candidates,
        "monolith_risk_scores": risk_scores,
        "suggested_extraction_plan": extraction_plan,
        "facts": facts,
        "inferences": inferences,
        "confidence_notes": [
            "Framework and route detection are static heuristics and may miss dynamic registration.",
            "Dead code and unused imports are file-local candidates and require human validation.",
            "Risk scores are relative indicators for prioritization, not formal quality gates.",
        ],
        "limitations": [
            "No runtime trace data or traffic weighting was used.",
            "Cross-module call graph and true reachability are not computed.",
            "Regex-based route extraction can undercount unconventional decorator patterns.",
        ],
    }


def analyze_codebase(files: dict[str, bytes], *, mode: str = "general") -> dict[str, Any]:
    resolved_mode = str(mode or "general").strip().lower() or "general"
    metrics = compute_module_metrics(files)
    languages = sorted({str(row.get("language") or "") for row in metrics if str(row.get("language") or "")})
    framework_hits: dict[str, int] = {
        "django": 0,
        "fastapi": 0,
        "flask": 0,
        "sqlalchemy": 0,
        "pydantic": 0,
    }
    route_inventory: list[dict[str, Any]] = []
    duplicate_name_map: dict[str, list[str]] = {}
    for path, blob in files.items():
        name = Path(path).name.lower()
        duplicate_name_map.setdefault(name, []).append(path)
        text = _decode_text(blob) or ""
        lower = text.lower()
        if "from django" in lower or "django." in lower:
            framework_hits["django"] += 1
        if "fastapi" in lower or "apirouter" in lower:
            framework_hits["fastapi"] += 1
        if "from flask" in lower or "flask." in lower:
            framework_hits["flask"] += 1
        if "sqlalchemy" in lower:
            framework_hits["sqlalchemy"] += 1
        if "pydantic" in lower or "basemodel" in lower:
            framework_hits["pydantic"] += 1
        if detect_language(path) == "python":
            for match in re.finditer(r"@[\w\.]+?\.(get|post|put|delete|patch|options|head)\(([^)]*)\)", text):
                route_inventory.append(
                    {
                        "path": path,
                        "style": "decorator",
                        "verb": str(match.group(1)).upper(),
                        "signature": str(match.group(0)),
                    }
                )
            if "urlpatterns" in text and ("path(" in text or "re_path(" in text):
                route_inventory.append({"path": path, "style": "django_urlpatterns", "verb": "N/A", "signature": "urlpatterns"})

    framework_fingerprint = [
        {"framework": key, "evidence_file_count": int(count)}
        for key, count in framework_hits.items()
        if count > 0
    ]
    framework_fingerprint.sort(key=lambda row: row["evidence_file_count"], reverse=True)

    largest_files = [
        {
            "path": str(row.get("path") or ""),
            "line_count": int(row.get("line_count") or 0),
            "language": str(row.get("language") or ""),
        }
        for row in metrics[:20]
    ]
    high_fan_in = sorted(metrics, key=lambda row: int(row.get("fan_in") or 0), reverse=True)[:20]
    high_fan_out = sorted(metrics, key=lambda row: int(row.get("fan_out") or 0), reverse=True)[:20]
    large_function_candidates: list[dict[str, Any]] = []
    dead_code_candidates: list[dict[str, Any]] = []
    unused_import_candidates: list[dict[str, Any]] = []
    for row in metrics:
        path = str(row.get("path") or "")
        for fn in row.get("large_functions") if isinstance(row.get("large_functions"), list) else []:
            if isinstance(fn, dict):
                large_function_candidates.append({"path": path, **fn})
        for dead in row.get("dead_code_candidates") if isinstance(row.get("dead_code_candidates"), list) else []:
            if isinstance(dead, dict):
                dead_code_candidates.append({"path": path, **dead})
        unused = row.get("unused_import_candidates") if isinstance(row.get("unused_import_candidates"), list) else []
        if unused:
            unused_import_candidates.append({"path": path, "imports": unused})

    duplicate_names = [
        {"name": name, "paths": sorted(paths)}
        for name, paths in duplicate_name_map.items()
        if len(paths) > 1
    ]
    duplicate_names.sort(key=lambda row: (len(row.get("paths") or []), row.get("name") or ""), reverse=True)

    architectural_risks: list[dict[str, Any]] = []
    seams: list[dict[str, Any]] = []
    top = largest_files[0] if largest_files else None
    if top and int(top.get("line_count") or 0) >= 5000:
        architectural_risks.append(
            {
                "severity": "high",
                "category": "oversized_file",
                "path": top.get("path"),
                "detail": f"Largest file has {top.get('line_count')} lines; likely monolithic hotspot.",
            }
        )
        seams.append(
            {
                "priority": "high",
                "title": "Extract bounded modules from oversized file",
                "target_path": top.get("path"),
                "suggested_slices": ["routing", "schema/models", "service layer", "persistence", "cross-cutting utils"],
            }
        )
    if len(framework_fingerprint) >= 2:
        architectural_risks.append(
            {
                "severity": "medium",
                "category": "mixed_framework_usage",
                "detail": "Multiple web/data frameworks detected across the same artifact.",
            }
        )
        seams.append(
            {
                "priority": "medium",
                "title": "Isolate framework boundaries",
                "suggested_slices": [row["framework"] for row in framework_fingerprint],
            }
        )
    if any(int(row.get("fan_out") or 0) >= 30 for row in metrics):
        offender = sorted(metrics, key=lambda row: int(row.get("fan_out") or 0), reverse=True)[0]
        architectural_risks.append(
            {
                "severity": "medium",
                "category": "high_coupling",
                "path": offender.get("path"),
                "detail": f"High import fan-out ({offender.get('fan_out')}) indicates heavy coupling.",
            }
        )
    result: dict[str, Any] = {
        "analysis_mode": resolved_mode,
        "languages_detected": languages,
        "framework_fingerprint": framework_fingerprint,
        "largest_files_by_line_count": largest_files,
        "modules_high_fan_in": [
            {"path": row.get("path"), "fan_in": int(row.get("fan_in") or 0)}
            for row in high_fan_in
        ],
        "modules_high_fan_out": [
            {"path": row.get("path"), "fan_out": int(row.get("fan_out") or 0)}
            for row in high_fan_out
        ],
        "duplicate_or_similar_file_names": duplicate_names,
        "large_function_candidates": sorted(
            large_function_candidates,
            key=lambda row: int(row.get("line_count") or 0),
            reverse=True,
        )[:200],
        "unused_import_candidates": unused_import_candidates[:200],
        "dead_code_candidates": dead_code_candidates[:200],
        "route_inventory": route_inventory[:500],
        "architectural_risks": architectural_risks,
        "recommended_refactor_seams": seams,
        "confidence_notes": [
            "Analysis is heuristic and static-only.",
            "Unused import and dead code candidates are best-effort and may include false positives.",
            "Route extraction is pattern-based and may miss dynamic registration.",
        ],
        "limitations": [
            "No whole-program call graph.",
            "No runtime execution evidence used.",
            "Binary/non-text files are excluded from structural analysis.",
        ],
    }
    if resolved_mode == "python_api":
        result["python_api_assessment"] = _extract_python_api_assessment(files, metrics)
    return result
