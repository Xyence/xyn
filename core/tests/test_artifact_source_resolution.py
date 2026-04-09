from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from core.artifact_source_resolution import (
    parse_packaged_artifact_metadata,
    resolve_artifact_source,
)


class ArtifactSourceResolutionTests(TestCase):
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
        self.assertEqual(resolved.resolved_source_roots, [])
        self.assertIn("manifest.json", resolved.files)

    def test_parse_packaged_artifact_metadata_extracts_content_ref(self) -> None:
        artifact_json = {
            "artifact": {"slug": "xyn-api", "title": "xyn-api", "type": "module"},
            "metadata": {
                "manifest_ref": "xyn-api/artifact.manifest.json",
                "content_ref": {"path": "/workspace/xyn"},
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

