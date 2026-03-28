from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from core.app_jobs import _install_generated_artifact_in_sibling, _package_generated_app
from core.generated_artifacts.lifecycle import LEGACY_GENERATED_VERSION, generate_revision_id
from core.generated_artifacts.persistence import promote_artifact_revision


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.flush_called = 0

    def query(self, _model):
        return _FakeQuery(self._rows)

    def flush(self):
        self.flush_called += 1


class ArtifactVersioningTests(unittest.TestCase):
    def test_generate_revision_id_is_unique(self):
        a = generate_revision_id()
        b = generate_revision_id()
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("r-"))
        self.assertTrue(b.startswith("r-"))

    def test_package_generation_adds_revision_identity_with_legacy_version(self):
        app_spec = {
            "schema_version": "xyn.appspec.v0",
            "app_slug": "tracker",
            "title": "Tracker",
            "workspace_id": str(uuid.uuid4()),
            "entities": ["tickets"],
            "services": [],
            "reports": [],
        }
        policy_bundle = {"schema_version": "xyn.policy_bundle.v0", "policy_families": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("core.app_jobs._generated_artifacts_root", return_value=Path(tmpdir)):
                first = _package_generated_app(
                    workspace_id=uuid.uuid4(),
                    source_job_id="job-1",
                    app_spec=app_spec,
                    policy_bundle=policy_bundle,
                    runtime_config={},
                )
                second = _package_generated_app(
                    workspace_id=uuid.uuid4(),
                    source_job_id="job-2",
                    app_spec=app_spec,
                    policy_bundle=policy_bundle,
                    runtime_config={},
                )

        self.assertEqual(first["artifact_version"], LEGACY_GENERATED_VERSION)
        self.assertEqual(second["artifact_version"], LEGACY_GENERATED_VERSION)
        self.assertTrue(first.get("revision_id"))
        self.assertTrue(second.get("revision_id"))
        self.assertNotEqual(first.get("revision_id"), second.get("revision_id"))
        self.assertEqual(first.get("version_label"), "dev")

    def test_promote_artifact_revision_updates_matching_metadata(self):
        matching = mock.Mock(extra_metadata={"generated_artifact_slug": "app.tracker", "revision_id": "rev-1", "version_label": "dev", "lifecycle_stage": "GENERATED"})
        non_matching = mock.Mock(extra_metadata={"generated_artifact_slug": "app.tracker", "revision_id": "rev-2", "version_label": "dev", "lifecycle_stage": "GENERATED"})
        db = _FakeDB([matching, non_matching])

        updated = promote_artifact_revision(
            db,
            artifact_slug="app.tracker",
            revision_id="rev-1",
            target_label="stable",
        )

        self.assertEqual(updated, 1)
        self.assertEqual(db.flush_called, 1)
        self.assertEqual(matching.extra_metadata.get("version_label"), "stable")
        self.assertEqual(matching.extra_metadata.get("lifecycle_stage"), "PROMOTED")
        self.assertEqual(non_matching.extra_metadata.get("version_label"), "dev")

    def test_install_references_revision_when_supported(self):
        captured_install_bodies: list[dict] = []

        def _session_stub(_container, *, port, steps):
            path = steps[-1]["path"]
            if path == "/xyn/api/workspaces":
                return 200, {"workspaces": [{"id": "ws-1", "slug": "development"}]}, "ok"
            body = steps[-1].get("body") or {}
            captured_install_bodies.append(body)
            return 201, {"artifact": {"slug": "app.tracker", "artifact_id": "a-1", "binding_id": "b-1", "artifact_revision_id": "rev-1", "package_version": LEGACY_GENERATED_VERSION}}, "ok"

        with mock.patch("core.app_jobs._container_http_session_json", side_effect=_session_stub):
            installed = _install_generated_artifact_in_sibling(
                sibling_api_container="sib-api",
                workspace_slug="development",
                artifact_slug="app.tracker",
                artifact_version=LEGACY_GENERATED_VERSION,
                artifact_revision_id="rev-1",
            )

        self.assertEqual(len(captured_install_bodies), 1)
        self.assertEqual(captured_install_bodies[0].get("artifact_revision_id"), "rev-1")
        self.assertEqual(installed.get("artifact_revision_id"), "rev-1")
        self.assertFalse(installed.get("revision_fallback_used"))

    def test_install_revision_fallback_preserves_backward_compatibility(self):
        captured_install_bodies: list[dict] = []
        install_attempts = {"count": 0}

        def _session_stub(_container, *, port, steps):
            path = steps[-1]["path"]
            if path == "/xyn/api/workspaces":
                return 200, {"workspaces": [{"id": "ws-1", "slug": "development"}]}, "ok"
            body = steps[-1].get("body") or {}
            captured_install_bodies.append(body)
            install_attempts["count"] += 1
            if install_attempts["count"] == 1:
                return 400, {"error": "unknown field artifact_revision_id"}, "bad request"
            return 201, {"artifact": {"slug": "app.tracker", "artifact_id": "a-1", "binding_id": "b-1", "package_version": LEGACY_GENERATED_VERSION}}, "ok"

        with mock.patch("core.app_jobs._container_http_session_json", side_effect=_session_stub):
            installed = _install_generated_artifact_in_sibling(
                sibling_api_container="sib-api",
                workspace_slug="development",
                artifact_slug="app.tracker",
                artifact_version=LEGACY_GENERATED_VERSION,
                artifact_revision_id="rev-1",
            )

        self.assertEqual(len(captured_install_bodies), 2)
        self.assertIn("artifact_revision_id", captured_install_bodies[0])
        self.assertNotIn("artifact_revision_id", captured_install_bodies[1])
        self.assertTrue(installed.get("revision_fallback_used"))


if __name__ == "__main__":
    unittest.main()
