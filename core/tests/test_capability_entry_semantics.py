from __future__ import annotations

import unittest

from core.runtime.provision_sibling import _build_capability_entry


class CapabilityEntrySemanticsTests(unittest.TestCase):
    def test_installed_artifact_is_primary_source_for_open_semantics(self):
        entry = _build_capability_entry(
            installed_artifact={
                "artifact_id": "art-1",
                "artifact_slug": "app.tracker",
                "workspace_id": "ws-1",
                "workspace_slug": "development",
                "artifact_revision_id": "rev-1",
            },
            generated_artifact={
                "artifact_slug": "app.tracker",
                "artifact_version": "0.0.1-dev",
                "artifact_revision_id": "rev-1",
            },
            sibling_output={"ui_url": "http://sib.localhost"},
            sibling_runtime={"runtime_base_url": "http://runtime:8080", "app_url": "http://sib.localhost"},
        )
        self.assertEqual(entry["source_of_truth"], "installed_artifact")
        self.assertEqual(entry["state"], "installed")
        self.assertEqual(entry["open_preference"]["mode"], "artifact_shell")
        self.assertEqual(entry["installed_artifact"]["artifact_id"], "art-1")

    def test_generated_not_installed_uses_runtime_fallback_semantics(self):
        entry = _build_capability_entry(
            installed_artifact={},
            generated_artifact={
                "artifact_slug": "app.tracker",
                "artifact_version": "0.0.1-dev",
                "artifact_revision_id": "rev-2",
            },
            sibling_output={"ui_url": "http://sib.localhost"},
            sibling_runtime={"runtime_base_url": "http://runtime:8080", "app_url": "http://sib.localhost"},
        )
        self.assertEqual(entry["source_of_truth"], "generated_artifact")
        self.assertEqual(entry["state"], "generated_not_installed")
        self.assertEqual(entry["open_preference"]["mode"], "runtime_url_fallback")
        self.assertEqual(entry["generated_artifact"]["artifact_revision_id"], "rev-2")


if __name__ == "__main__":
    unittest.main()
