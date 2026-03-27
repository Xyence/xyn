import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from core.generated_artifacts.persistence import (
    link_generated_artifact_memberships,
    persist_appspec_artifact,
    persist_generated_json_artifact,
    persist_policy_artifact,
)


class _FakeDB:
    def __init__(self) -> None:
        self.rows = []
        self.flushed = False

    def add(self, row):
        self.rows.append(row)

    def flush(self):
        self.flushed = True


class GeneratedArtifactPersistenceTests(unittest.TestCase):
    def test_persist_generated_json_artifact_round_trip_and_metadata(self):
        workspace_id = uuid.uuid4()
        payload = {"a": 1, "z": ["x"]}
        fixed_now = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmpdir:
            db = _FakeDB()
            artifact_id = persist_generated_json_artifact(
                db,
                workspace_id=workspace_id,
                name="appspec.demo",
                kind="app_spec",
                payload=payload,
                metadata={"job_id": "j1"},
                workspace_root_factory=lambda: Path(tmpdir),
                now_fn=lambda: fixed_now,
            )

            self.assertTrue(db.flushed)
            self.assertEqual(len(db.rows), 1)
            row = db.rows[0]
            self.assertEqual(str(row.id), artifact_id)
            self.assertEqual(row.name, "appspec.demo")
            self.assertEqual(row.kind, "app_spec")
            self.assertEqual(row.extra_metadata.get("workspace_id"), str(workspace_id))
            self.assertEqual(row.extra_metadata.get("job_id"), "j1")
            self.assertEqual(row.created_at, fixed_now)

            path = Path(str(row.storage_path))
            self.assertTrue(path.exists())
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored, payload)

    def test_persist_generated_json_artifact_backward_compatible_without_optional_metadata(self):
        workspace_id = uuid.uuid4()

        with tempfile.TemporaryDirectory() as tmpdir:
            db = _FakeDB()
            persist_generated_json_artifact(
                db,
                workspace_id=workspace_id,
                name="policy.demo",
                kind="policy_bundle",
                payload={"schema": "xyn.policy_bundle.v0"},
                metadata=None,
                workspace_root_factory=lambda: Path(tmpdir),
                now_fn=lambda: datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc),
            )
            row = db.rows[0]
            self.assertEqual(row.extra_metadata, {"workspace_id": str(workspace_id)})

    def test_persist_appspec_artifact_metadata_parity(self):
        persist = mock.Mock(return_value="artifact-1")
        result = persist_appspec_artifact(
            mock.Mock(),
            workspace_id=uuid.uuid4(),
            app_spec={"app_slug": "demo"},
            job_id="job-1",
            inference_diagnostics={"route": "B"},
            persist_fn=persist,
        )
        self.assertEqual(result, "artifact-1")
        kwargs = persist.call_args.kwargs
        self.assertEqual(kwargs["name"], "appspec.demo")
        self.assertEqual(kwargs["kind"], "app_spec")
        self.assertEqual(kwargs["metadata"], {"job_id": "job-1", "inference_diagnostics": {"route": "B"}})

    def test_persist_policy_artifact_metadata_parity(self):
        persist = mock.Mock(return_value="policy-1")
        policy_slug_fn = mock.Mock(side_effect=lambda app_slug: f"policy.{app_slug}")
        result = persist_policy_artifact(
            mock.Mock(),
            workspace_id=uuid.uuid4(),
            app_slug="demo",
            policy_bundle={"schema": "xyn.policy_bundle.v0"},
            job_id="job-1",
            app_spec_artifact_id="artifact-1",
            policy_slug_fn=policy_slug_fn,
            persist_fn=persist,
        )
        self.assertEqual(result, "policy-1")
        kwargs = persist.call_args.kwargs
        self.assertEqual(kwargs["name"], "policy.demo")
        self.assertEqual(kwargs["kind"], "policy_bundle")
        self.assertEqual(kwargs["metadata"], {"job_id": "job-1", "app_spec_artifact_id": "artifact-1"})

    def test_link_generated_artifact_memberships_is_idempotent_noop_here(self):
        db = mock.Mock()
        self.assertEqual(link_generated_artifact_memberships(_db=db), [])
        self.assertEqual(link_generated_artifact_memberships(_db=db), [])


if __name__ == "__main__":
    unittest.main()
