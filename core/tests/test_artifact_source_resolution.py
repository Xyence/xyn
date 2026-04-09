from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase

from core.artifact_source_resolution import (
    parse_packaged_artifact_metadata,
    resolve_artifact_source,
)


class ArtifactSourceResolutionTests(TestCase):
    def setUp(self) -> None:
        self._prev_runtime_repo_map = os.environ.get("XYN_RUNTIME_REPO_MAP")

    def tearDown(self) -> None:
        if self._prev_runtime_repo_map is None:
            os.environ.pop("XYN_RUNTIME_REPO_MAP", None)
        else:
            os.environ["XYN_RUNTIME_REPO_MAP"] = self._prev_runtime_repo_map

    def test_resolve_prefers_filesystem_source_root_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "service.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "blob.bin").write_bytes(b"\x00\x01\x02")
            resolved = resolve_artifact_source(
                artifact_slug="app.demo",
                metadata={"source_root": str(root)},
                packaged_files={"manifest.json": b"{}"},
            )
        self.assertEqual(resolved.source_mode, "resolved_source")
        self.assertEqual(resolved.source_origin, "filesystem_hint")
        self.assertEqual(resolved.resolution_branch, "filesystem_hint")
        self.assertTrue(resolved.resolved_source_roots)
        self.assertIn("service.py", resolved.files)
        self.assertNotIn("blob.bin", resolved.files)

    def test_resolve_falls_back_to_packaged_when_no_source_root_found(self) -> None:
        resolved = resolve_artifact_source(
            artifact_slug="app.demo",
            metadata={"source_root": "/definitely/missing"},
            packaged_files={"manifest.json": b"{}"},
        )
        self.assertEqual(resolved.source_mode, "packaged_fallback")
        self.assertEqual(resolved.source_origin, "packaged_fallback")
        self.assertEqual(resolved.resolution_branch, "packaged_fallback")
        self.assertEqual(resolved.resolved_source_roots, [])
        self.assertIn("manifest.json", resolved.files)
        self.assertTrue(any("deterministic/provenance" in msg for msg in resolved.warnings))

    def test_resolve_uses_provenance_monorepo_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "xyn-platform"
            service_root = repo_root / "services" / "xyn-api"
            service_root.mkdir(parents=True, exist_ok=True)
            (service_root / "xyn_api.py").write_text("app = 'ok'\n", encoding="utf-8")
            os.environ["XYN_RUNTIME_REPO_MAP"] = json.dumps({"xyn-platform": [str(repo_root)]})
            resolved = resolve_artifact_source(
                artifact_slug="xyn-api",
                metadata={
                    "provenance": {
                        "source": {
                            "kind": "git",
                            "repo_key": "xyn-platform",
                            "repo_url": "https://github.com/xyn-platform",
                            "commit_sha": "abcdef0123456789",
                            "monorepo_subpath": "services/xyn-api",
                            "branch_hint": "develop",
                        }
                    }
                },
                packaged_files={"manifest.json": b"{}"},
            )
        self.assertEqual(resolved.source_mode, "resolved_source")
        self.assertEqual(resolved.source_origin, "github")
        self.assertEqual(resolved.resolution_branch, "provenance_backed")
        self.assertIn("xyn_api.py", resolved.files)
        source = resolved.provenance.get("source") if isinstance(resolved.provenance.get("source"), dict) else {}
        self.assertEqual(source.get("repo_key"), "xyn-platform")
        self.assertEqual(source.get("monorepo_subpath"), "services/xyn-api")

    def test_resolve_warns_when_provenance_repo_missing_and_falls_back(self) -> None:
        os.environ["XYN_RUNTIME_REPO_MAP"] = json.dumps({"xyn-platform": ["/definitely/missing"]})
        resolved = resolve_artifact_source(
            artifact_slug="app.demo",
            metadata={
                "provenance": {
                    "source": {
                        "kind": "git",
                        "repo_key": "xyn-platform",
                        "commit_sha": "abcdef0123456789",
                        "monorepo_subpath": "services/xyn-api",
                    }
                }
            },
            packaged_files={"manifest.json": b"{}"},
        )
        self.assertEqual(resolved.source_mode, "packaged_fallback")
        self.assertEqual(resolved.source_origin, "packaged_fallback")
        self.assertEqual(resolved.resolution_branch, "packaged_fallback")
        self.assertTrue(
            any(
                "No local mirror/checkout found" in msg
                or "monorepo_subpath/manifest_ref directory was not found" in msg
                for msg in resolved.warnings
            )
        )

    def test_resolve_uses_colon_pair_runtime_repo_map_syntax(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "xyn-platform"
            service_root = repo_root / "services" / "xyn-api"
            service_root.mkdir(parents=True, exist_ok=True)
            (service_root / "xyn_api.py").write_text("app = 'ok'\n", encoding="utf-8")
            os.environ["XYN_RUNTIME_REPO_MAP"] = f"xyn-platform:{repo_root}"
            resolved = resolve_artifact_source(
                artifact_slug="xyn-api",
                metadata={
                    "provenance": {
                        "source": {
                            "kind": "git",
                            "repo_key": "xyn-platform",
                            "repo_url": "https://github.com/xyn-platform",
                            "commit_sha": "abcdef0123456789",
                            "monorepo_subpath": "services/xyn-api",
                            "branch_hint": "develop",
                        }
                    }
                },
                packaged_files={"manifest.json": b"{}"},
            )
        self.assertEqual(resolved.source_mode, "resolved_source")
        self.assertEqual(resolved.resolution_branch, "provenance_backed")
        self.assertIn("xyn_api.py", resolved.files)

    def test_resolve_rejects_unsafe_monorepo_subpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "xyn-platform"
            repo_root.mkdir(parents=True, exist_ok=True)
            os.environ["XYN_RUNTIME_REPO_MAP"] = json.dumps({"xyn-platform": [str(repo_root)]})
            resolved = resolve_artifact_source(
                artifact_slug="app.demo",
                metadata={
                    "provenance": {
                        "source": {
                            "kind": "git",
                            "repo_key": "xyn-platform",
                            "commit_sha": "abcdef0123456789",
                            "monorepo_subpath": "../etc",
                        }
                    }
                },
                packaged_files={"manifest.json": b"{}"},
            )
        self.assertEqual(resolved.source_mode, "packaged_fallback")
        self.assertEqual(resolved.resolution_branch, "packaged_fallback")
        self.assertTrue(any("unsafe monorepo_subpath" in msg for msg in resolved.warnings))

    def test_xyn_api_without_provenance_does_not_silently_resolve_to_core_root(self) -> None:
        resolved = resolve_artifact_source(
            artifact_slug="xyn-api",
            metadata={},
            packaged_files={"manifest.json": b"{}"},
        )
        self.assertEqual(resolved.source_mode, "packaged_fallback")
        self.assertEqual(resolved.source_origin, "packaged_fallback")
        self.assertEqual(resolved.resolution_branch, "packaged_fallback")
        details = resolved.resolution_details if isinstance(resolved.resolution_details, dict) else {}
        candidates = details.get("candidate_roots") if isinstance(details.get("candidate_roots"), list) else []
        self.assertFalse(any(str(item.get("path") or "").rstrip("/") == "/app" and bool(item.get("selected")) for item in candidates if isinstance(item, dict)))

    def test_resolution_details_include_provenance_candidate_diagnostics(self) -> None:
        os.environ["XYN_RUNTIME_REPO_MAP"] = json.dumps({"xyn-platform": ["/definitely/missing"]})
        resolved = resolve_artifact_source(
            artifact_slug="app.demo",
            metadata={
                "provenance": {
                    "source": {
                        "kind": "git",
                        "repo_key": "xyn-platform",
                        "commit_sha": "abcdef0123456789",
                        "monorepo_subpath": "services/xyn-api",
                    }
                }
            },
            packaged_files={"manifest.json": b"{}"},
        )
        details = resolved.resolution_details if isinstance(resolved.resolution_details, dict) else {}
        provenance_details = details.get("provenance") if isinstance(details.get("provenance"), dict) else {}
        candidate_repo_roots = (
            provenance_details.get("candidate_repo_roots")
            if isinstance(provenance_details.get("candidate_repo_roots"), list)
            else []
        )
        self.assertTrue(candidate_repo_roots)
        self.assertTrue(any(isinstance(row, dict) and str(row.get("path") or "").endswith("/definitely/missing") for row in candidate_repo_roots))

    def test_parse_packaged_artifact_metadata_extracts_content_ref(self) -> None:
        artifact_json = {
            "artifact": {"slug": "xyn-api", "title": "xyn-api", "type": "module"},
            "metadata": {
                "manifest_ref": "xyn-api/artifact.manifest.json",
                "content_ref": {"path": "/workspace/xyn"},
                "source": {
                    "kind": "git",
                    "repo_url": "https://github.com/xyn-platform",
                    "repo_key": "xyn-platform",
                    "commit_sha": "abcdef0123456789",
                    "branch_hint": "develop",
                    "monorepo_subpath": "services/xyn-api",
                },
                "build": {
                    "pipeline_provider": "github_actions",
                    "run_id": "1234",
                    "image_ref": "public.ecr.aws/xyn/xyn-api:develop",
                    "image_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                    "built_from_commit_sha": "abcdef0123456789",
                },
            },
        }
        files = {
            "manifest.json": b"{}",
            "artifacts/module/xyn-api/0.1.0/artifact.json": json.dumps(artifact_json).encode("utf-8"),
        }
        metadata = parse_packaged_artifact_metadata(files)
        self.assertEqual(metadata.get("manifest_ref"), "xyn-api/artifact.manifest.json")
        content_ref = metadata.get("content_ref") if isinstance(metadata.get("content_ref"), dict) else {}
        self.assertEqual(content_ref.get("path"), "/workspace/xyn")
        provenance = metadata.get("provenance") if isinstance(metadata.get("provenance"), dict) else {}
        source = provenance.get("source") if isinstance(provenance.get("source"), dict) else {}
        build = provenance.get("build") if isinstance(provenance.get("build"), dict) else {}
        self.assertEqual(source.get("repo_key"), "xyn-platform")
        self.assertEqual(source.get("monorepo_subpath"), "services/xyn-api")
        self.assertEqual(build.get("pipeline_provider"), "github_actions")
