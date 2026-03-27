import uuid
import unittest
from types import SimpleNamespace
from unittest import mock

from core.job_pipeline.execution_note_coordinator import (
    begin_stage_note,
    record_stage_failure,
    record_stage_metadata,
    resolve_execution_note_artifact_id,
)
from core.app_jobs import _handle_generate_app_spec


class ExecutionNoteCoordinatorTests(unittest.TestCase):
    def test_begin_stage_note_forwards_payload(self):
        db = object()
        create = mock.Mock(return_value=SimpleNamespace(id=uuid.uuid4()))
        note = begin_stage_note(
            db,
            workspace_id=uuid.uuid4(),
            prompt_or_request="build app",
            findings=["f1"],
            root_cause="root",
            proposed_fix="fix",
            implementation_summary="impl",
            validation_summary=["v1"],
            debt_recorded=[],
            related_artifact_ids=[],
            status="in_progress",
            extra_metadata={"job_id": "j1"},
            create_note=create,
        )
        self.assertIsNotNone(note)
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["prompt_or_request"], "build app")
        self.assertEqual(kwargs["status"], "in_progress")
        self.assertEqual(kwargs["extra_metadata"], {"job_id": "j1"})

    def test_record_stage_metadata_success_and_absent_metadata_compatible(self):
        update = mock.Mock(return_value=SimpleNamespace(id=uuid.uuid4()))
        db = object()
        record_stage_metadata(
            db,
            artifact_id=uuid.uuid4(),
            implementation_summary="done",
            append_validation=["ok"],
            extra_metadata_updates={"inference_diagnostics": {"route": "B"}},
            update_note=update,
        )
        kwargs = update.call_args.kwargs
        self.assertEqual(kwargs["implementation_summary"], "done")
        self.assertEqual(kwargs["append_validation"], ["ok"])
        self.assertEqual(kwargs["extra_metadata_updates"], {"inference_diagnostics": {"route": "B"}})

        update.reset_mock()
        record_stage_metadata(db, artifact_id=uuid.uuid4(), update_note=update)
        minimal_kwargs = update.call_args.kwargs
        self.assertIn("artifact_id", minimal_kwargs)
        self.assertNotIn("extra_metadata_updates", minimal_kwargs)
        self.assertNotIn("append_validation", minimal_kwargs)

    def test_record_stage_failure_payload(self):
        update = mock.Mock(return_value=SimpleNamespace(id=uuid.uuid4()))
        db = object()
        record_stage_failure(db, artifact_id=uuid.uuid4(), job_type="smoke_test", error=RuntimeError("boom"), update_note=update)
        kwargs = update.call_args.kwargs
        self.assertEqual(kwargs["status"], "failed")
        self.assertIn("Execution stopped during job type=smoke_test", kwargs["implementation_summary"])
        self.assertEqual(kwargs["append_validation"], ["Failure during smoke_test: boom"])

    def test_resolve_execution_note_artifact_id(self):
        self.assertEqual(resolve_execution_note_artifact_id({}, {"x": 1}), "")
        self.assertEqual(
            resolve_execution_note_artifact_id({"execution_note_artifact_id": ""}, {"execution_note_artifact_id": "abc"}),
            "abc",
        )


class ExecutionNoteCoordinatorIntegrationTests(unittest.TestCase):
    def test_generate_app_spec_updates_note_with_diagnostics_metadata(self):
        workspace_id = uuid.uuid4()
        job = SimpleNamespace(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            type="generate_app_spec",
            input_json={"title": "Diagnostic App", "content_json": {"raw_prompt": "build tracker"}},
        )
        fake_note = SimpleNamespace(id=uuid.uuid4())
        fake_workspace = SimpleNamespace(slug="development")
        app_spec = {
            "schema_version": "xyn.appspec.v0",
            "app_slug": "diagnostic-app",
            "title": "Diagnostic App",
            "workspace_id": str(workspace_id),
            "services": [],
            "data": {"postgres": {}},
            "reports": [],
            "entities": ["campaigns"],
            "phase_1_scope": ["campaigns"],
            "requested_visuals": [],
        }
        policy_bundle = {"schema_version": "xyn.policy_bundle.v0", "bundle_id": "policy.diagnostic-app"}
        diagnostics = {
            "structure_score": 0.5,
            "route": "B",
            "llm_used": False,
            "consistency_warnings": [],
            "consistency_errors": [],
            "fallback_or_repair_used": False,
        }
        fake_db = mock.MagicMock()
        fake_db.query.return_value.filter.return_value.first.return_value = fake_workspace
        with mock.patch("core.app_jobs.create_execution_note", return_value=fake_note):
            with mock.patch("core.app_jobs._build_app_spec_with_diagnostics", return_value=(app_spec, diagnostics)):
                with mock.patch("core.app_jobs.validate", return_value=None):
                    with mock.patch("core.app_jobs._build_policy_bundle", return_value=policy_bundle):
                        with mock.patch("core.app_jobs._persist_json_artifact", side_effect=["appspec-art", "policy-art"]):
                            with mock.patch(
                                "core.app_jobs._package_generated_app",
                                return_value={"artifact_slug": "app.diagnostic-app", "artifact_version": "0.0.1-dev", "artifact_package_path": "/tmp/pkg.zip"},
                            ):
                                with mock.patch("core.app_jobs._import_generated_artifact_package", return_value={}):
                                    with mock.patch("core.app_jobs.update_execution_note", return_value=fake_note) as update:
                                        _handle_generate_app_spec(fake_db, job, [])

        kwargs = update.call_args.kwargs
        extra_metadata_updates = kwargs.get("extra_metadata_updates") if isinstance(kwargs.get("extra_metadata_updates"), dict) else {}
        self.assertEqual(extra_metadata_updates.get("inference_diagnostics"), diagnostics)


if __name__ == "__main__":
    unittest.main()
