from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from core.artifact_code_review import (
    analyze_codebase,
    build_hierarchical_tree,
    build_source_index,
    compute_module_metrics,
    FilePathNotFoundError,
    parse_artifact_source_files,
    read_file_chunk,
    search_files,
)


def _zip_bytes(entries: dict[str, str]) -> bytes:
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return blob.getvalue()


class ArtifactCodeReviewTests(unittest.TestCase):
    def test_parse_zip_and_build_tree(self) -> None:
        payload = _zip_bytes(
            {
                "pkg/main.py": "import fastapi\n\napp = fastapi.FastAPI()\n",
                "pkg/routes.py": "from fastapi import APIRouter\nrouter = APIRouter()\n",
            }
        )
        files = parse_artifact_source_files(artifact_name="bundle.zip", artifact_bytes=payload)
        self.assertIn("pkg/main.py", files)
        index_rows = build_source_index(files, include_line_counts=True)
        self.assertEqual(len(index_rows), 2)
        tree = build_hierarchical_tree(index_rows)
        self.assertEqual(tree["kind"], "dir")
        self.assertTrue(any(child.get("name") == "pkg" for child in tree.get("children") or []))

    def test_read_file_chunk_line_window(self) -> None:
        files = {"app.py": b"line1\nline2\nline3\nline4\nline5\n"}
        chunk = read_file_chunk(files=files, path="app.py", start_line=2, end_line=4)
        self.assertEqual(chunk["returned_start_line"], 2)
        self.assertEqual(chunk["returned_end_line"], 4)
        self.assertEqual(chunk["total_lines"], 5)
        self.assertIn("line2", chunk["content"])
        self.assertNotIn("line1", chunk["content"])

    def test_search_files_supports_regex_and_filters(self) -> None:
        files = {
            "a.py": b"from fastapi import APIRouter\n@router.get('/x')\n",
            "b.txt": b"router.get not python",
        }
        result = search_files(
            files=files,
            query=r"router\.get",
            regex=True,
            file_extensions=[".py"],
            case_sensitive=False,
            limit=10,
        )
        self.assertGreaterEqual(result["total_hits"], 1)
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(result["files"][0]["path"], "a.py")

    def test_search_hit_path_can_be_read_directly(self) -> None:
        files = {
            "backend/xyn_orchestrator/xyn_api.py": b"def handler():\n    return 1\n",
            "xyn_orchestrator/xyn_api.py": b"def mirrored():\n    return 2\n",
        }
        result = search_files(files=files, query="handler", limit=10)
        self.assertGreaterEqual(result["total_hits"], 1)
        hit_path = str(result["files"][0]["path"])
        chunk = read_file_chunk(files=files, path=hit_path, start_line=1, end_line=2)
        self.assertEqual(chunk["path"], hit_path)
        self.assertIn("handler", chunk["content"])

    def test_read_file_chunk_resolves_backend_mirror_paths(self) -> None:
        files = {
            "backend/xyn_orchestrator/xyn_api.py": b"def backend_only():\n    return 1\n",
        }
        chunk = read_file_chunk(files=files, path="xyn_orchestrator/xyn_api.py", start_line=1, end_line=2)
        self.assertEqual(chunk["path"], "backend/xyn_orchestrator/xyn_api.py")
        self.assertIn("backend_only", chunk["content"])

        files_without_backend = {
            "xyn_orchestrator/xyn_api.py": b"def non_backend():\n    return 2\n",
        }
        chunk_without_backend = read_file_chunk(
            files=files_without_backend,
            path="backend/xyn_orchestrator/xyn_api.py",
            start_line=1,
            end_line=2,
        )
        self.assertEqual(chunk_without_backend["path"], "xyn_orchestrator/xyn_api.py")
        self.assertIn("non_backend", chunk_without_backend["content"])

    def test_read_file_chunk_accepts_repo_relative_path_when_canonical_key_is_root_relative(self) -> None:
        files = {
            "backend/xyn_orchestrator/architecture_placement.py": b"def placement():\n    return 'ok'\n",
        }
        chunk = read_file_chunk(
            files=files,
            path="services/xyn-api/backend/xyn_orchestrator/architecture_placement.py",
            start_line=1,
            end_line=2,
        )
        self.assertEqual(chunk["path"], "backend/xyn_orchestrator/architecture_placement.py")
        self.assertIn("placement", chunk["content"])

    def test_read_file_chunk_near_match_error_includes_candidate_paths(self) -> None:
        files = {
            "apps/a/xyn_api.py": b"def a():\n    return 1\n",
            "apps/b/xyn_api.py": b"def b():\n    return 2\n",
        }
        with self.assertRaises(FilePathNotFoundError) as ctx:
            read_file_chunk(files=files, path="xyn_api.py", start_line=1, end_line=1)
        self.assertEqual(str(ctx.exception), "'file not found'")
        self.assertEqual(
            sorted(ctx.exception.candidate_paths),
            sorted(["apps/a/xyn_api.py", "apps/b/xyn_api.py"]),
        )

    def test_tree_search_and_analysis_paths_are_directly_readable(self) -> None:
        files = {
            "backend/xyn_orchestrator/architecture_placement.py": b"def placement():\n    return 'ok'\n",
            "backend/xyn_orchestrator/guardrails/canonical_boundaries.py": b"RULE = 'xyn_api.py source of truth'\n",
            "xyn_orchestrator/tests/test_solution_change_session_repo_commits.py": b"def test_commit_chain():\n    assert True\n",
        }
        tree_rows = build_source_index(files, include_line_counts=True)
        self.assertTrue(tree_rows)
        tree_path = str(tree_rows[0]["path"])
        tree_chunk = read_file_chunk(files=files, path=tree_path, start_line=1, end_line=2)
        self.assertEqual(tree_chunk["path"], tree_path)

        search_result = search_files(files=files, query="xyn_api.py", limit=10)
        self.assertGreaterEqual(search_result["total_hits"], 1)
        search_path = str(search_result["files"][0]["path"])
        search_chunk = read_file_chunk(files=files, path=search_path, start_line=1, end_line=2)
        self.assertEqual(search_chunk["path"], search_path)

        analysis = analyze_codebase(files, mode="general")
        largest = analysis.get("largest_files_by_line_count") if isinstance(analysis.get("largest_files_by_line_count"), list) else []
        self.assertTrue(largest)
        analysis_path = str(largest[0]["path"])
        analysis_chunk = read_file_chunk(files=files, path=analysis_path, start_line=1, end_line=2)
        self.assertEqual(analysis_chunk["path"], analysis_path)

    def test_large_python_file_is_readable_when_within_configured_limit(self) -> None:
        from core import artifact_source_resolution as module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            module._DEFAULT_MAX_FILE_BYTES = 4 * 1024 * 1024
            module._DEFAULT_MAX_TOTAL_BYTES = 8 * 1024 * 1024
            module._DEFAULT_MAX_FILES = 100
            target = root / "backend" / "xyn_orchestrator" / "xyn_api.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"a" * (2 * 1024 * 1024))
            files, roots, warnings = module._read_source_roots([root])
            self.assertIn("backend/xyn_orchestrator/xyn_api.py", files)
            self.assertEqual(roots, [str(root.resolve())])
            self.assertEqual(warnings, [])

    def test_compute_metrics_and_analysis_include_expected_fields(self) -> None:
        files = {
            "monolith.py": (
                "import fastapi\n"
                "import sqlalchemy\n"
                "from pydantic import BaseModel\n\n"
                "app = fastapi.FastAPI()\n\n"
                "@app.get('/health')\n"
                "def health():\n"
                "    return {'ok': True}\n\n"
                "def _unused_helper():\n"
                "    return 1\n"
            ).encode("utf-8"),
            "small.py": b"import os\n",
        }
        metrics = compute_module_metrics(files)
        self.assertTrue(any(str(row.get("path")) == "monolith.py" for row in metrics))
        analysis = analyze_codebase(files)
        self.assertIn("languages_detected", analysis)
        self.assertTrue(any(item.get("framework") == "fastapi" for item in analysis["framework_fingerprint"]))
        self.assertTrue(any(str(item.get("path")) == "monolith.py" for item in analysis["largest_files_by_line_count"]))

    def test_python_api_mode_returns_specialized_assessment(self) -> None:
        giant_lines = "\n".join(["def noop():", "    return 1"] * 2600)
        files = {
            "xyn_api.py": (
                "from fastapi import FastAPI, APIRouter, Depends\n"
                "from django.urls import path\n"
                "from rest_framework.viewsets import ViewSet\n"
                "app = FastAPI()\n"
                "router = APIRouter()\n"
                "@router.get('/health')\n"
                "def health():\n"
                "    return {'ok': True}\n"
                "urlpatterns = [path('legacy/', lambda r: None)]\n"
                + giant_lines
            ).encode("utf-8"),
            "services/auth.py": b"def login_user(token: str):\n    return token\n",
        }
        analysis = analyze_codebase(files, mode="python_api")
        self.assertEqual(analysis.get("analysis_mode"), "python_api")
        python_api = analysis.get("python_api_assessment") if isinstance(analysis.get("python_api_assessment"), dict) else {}
        self.assertIn("oversized_file_report", python_api)
        self.assertIn("framework_fingerprint", python_api)
        self.assertTrue(bool(((python_api.get("framework_fingerprint") or {}).get("mixed_framework_detected"))))
        routes = ((python_api.get("route_inventory") or {}).get("items")) or []
        self.assertGreaterEqual(len(routes), 1)
        risks = python_api.get("monolith_risk_scores") if isinstance(python_api.get("monolith_risk_scores"), dict) else {}
        self.assertIn("file_size_risk", risks)
        plan = python_api.get("suggested_extraction_plan") if isinstance(python_api.get("suggested_extraction_plan"), list) else []
        self.assertGreaterEqual(len(plan), 3)


if __name__ == "__main__":
    unittest.main()
