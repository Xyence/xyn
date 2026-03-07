import json
import os
import tempfile
import unittest
from unittest.mock import patch

from core.context_pack_manifest import load_authoritative_context_pack_definitions


class ContextPackBridgeTests(unittest.TestCase):
    def test_loads_authoritative_manifest_with_stable_slugs(self):
        manifest = {
            "manifest_version": "xyn.context-pack-runtime-manifest.v1",
            "source_system": "xyn-platform",
            "source_seed_pack_slug": "xyn-core-context-packs",
            "source_seed_pack_version": "v1.2.0",
            "context_packs": [
                {
                    "slug": "xyn-console-default",
                    "title": "Xyn Console Default",
                    "description": "Console",
                    "purpose": "any",
                    "scope": "global",
                    "version": "1.0.0",
                    "capabilities": ["palette"],
                    "bind_by_default": True,
                    "content_format": "markdown",
                    "content": "# console",
                },
                {
                    "slug": "xyn-planner-canon",
                    "title": "Xyn Planner Canon",
                    "description": "Planner",
                    "purpose": "planner",
                    "scope": "global",
                    "version": "1.0.0",
                    "capabilities": ["app-builder"],
                    "bind_by_default": True,
                    "content_format": "markdown",
                    "content": "# planner",
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "context-packs.manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with patch.dict(os.environ, {"XYN_CONTEXT_PACK_MANIFEST_PATH": manifest_path}, clear=False):
                rows, source = load_authoritative_context_pack_definitions()
        self.assertEqual(source["source_system"], "xyn-platform")
        self.assertFalse(source["fallback_used"])
        self.assertEqual([row["slug"] for row in rows], ["xyn-console-default", "xyn-planner-canon"])
        self.assertTrue(all(bool(row["bind_by_default"]) for row in rows))

    def test_dedupes_duplicate_slugs_from_manifest(self):
        manifest = {
            "manifest_version": "xyn.context-pack-runtime-manifest.v1",
            "source_system": "xyn-platform",
            "context_packs": [
                {"slug": "xyn-console-default", "title": "A", "content": "", "capabilities": []},
                {"slug": "xyn-console-default", "title": "B", "content": "", "capabilities": []},
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "context-packs.manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle)
            with patch.dict(os.environ, {"XYN_CONTEXT_PACK_MANIFEST_PATH": manifest_path}, clear=False):
                rows, _ = load_authoritative_context_pack_definitions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slug"], "xyn-console-default")


if __name__ == "__main__":
    unittest.main()
