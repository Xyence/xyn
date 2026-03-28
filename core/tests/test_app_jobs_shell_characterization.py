from __future__ import annotations

import unittest
import uuid
from types import SimpleNamespace
from unittest import mock

from core import app_jobs
from core.models import JobStatus


class AppJobsShellCharacterizationTests(unittest.TestCase):
    def _make_job(self, *, job_type: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=uuid.uuid4(),
            type=job_type,
            workspace_id=uuid.uuid4(),
            status=JobStatus.QUEUED.value,
            input_json={},
            output_json={},
            logs_text="",
            updated_at=None,
        )

    def _mock_db_for_job(self, job: SimpleNamespace) -> mock.Mock:
        db = mock.Mock()
        db.query.return_value.filter.return_value.first.return_value = job
        return db

    def test_execute_job_routes_by_type(self):
        route_map = {
            "generate_app_spec": "_handle_generate_app_spec",
            "deploy_app_local": "_handle_deploy_app_local",
            "provision_sibling_xyn": "_handle_provision_sibling_xyn",
            "smoke_test": "_handle_smoke_test",
        }
        for job_type, handler_name in route_map.items():
            with self.subTest(job_type=job_type):
                job = self._make_job(job_type=job_type)
                db = self._mock_db_for_job(job)
                with mock.patch("core.app_jobs.SessionLocal", return_value=db):
                    with mock.patch(f"core.app_jobs.{handler_name}", return_value=({"ok": True}, [])) as handler:
                        app_jobs._execute_job(job.id)
                handler.assert_called_once()
                self.assertEqual(job.status, JobStatus.SUCCEEDED.value)
                self.assertEqual(job.output_json.get("ok"), True)

    def test_execute_job_unknown_type_fails_with_expected_error(self):
        job = self._make_job(job_type="unknown_job_type")
        db = self._mock_db_for_job(job)
        with mock.patch("core.app_jobs.SessionLocal", return_value=db):
            app_jobs._execute_job(job.id)

        self.assertEqual(job.status, JobStatus.FAILED.value)
        self.assertIn("Unsupported job type: unknown_job_type", str(job.output_json.get("error") or ""))

    def test_execute_job_follow_up_enqueue_wiring(self):
        job = self._make_job(job_type="generate_app_spec")
        db = self._mock_db_for_job(job)
        with mock.patch("core.app_jobs.SessionLocal", return_value=db):
            with mock.patch(
                "core.app_jobs._handle_generate_app_spec",
                return_value=(
                    {"result": "ok"},
                    [{"type": "deploy_app_local", "input_json": {"artifact_id": "a-1"}}],
                ),
            ):
                with mock.patch("core.app_jobs._enqueue_job", return_value="queued-1") as enqueue:
                    app_jobs._execute_job(job.id)

        enqueue.assert_called_once_with(
            db,
            workspace_id=job.workspace_id,
            job_type="deploy_app_local",
            input_json={"artifact_id": "a-1"},
        )
        queued = job.output_json.get("queued_jobs") or []
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["job_id"], "queued-1")

    def test_recover_running_jobs_marks_failed_with_restart_message(self):
        row = SimpleNamespace(
            id=uuid.uuid4(),
            status=JobStatus.RUNNING.value,
            output_json={},
            logs_text="existing log",
            updated_at=None,
        )
        db = mock.Mock()
        db.query.return_value.filter.return_value.all.return_value = [row]

        app_jobs._recover_running_jobs(db)

        self.assertEqual(row.status, JobStatus.FAILED.value)
        self.assertIn("Job interrupted by process restart before completion.", str(row.output_json.get("error") or ""))
        self.assertIn("recovered stale RUNNING job as FAILED", row.logs_text)
        db.commit.assert_called_once()

    def test_compatibility_shims_present(self):
        expected_names = [
            "_build_policy_bundle",
            "_handle_deploy_app_local",
            "_handle_provision_sibling_xyn",
            "_handle_smoke_test",
            "_exercise_runtime_contracts",
            "_ensure_parent_status_gate_prerequisites",
            "_extract_objective_sections",
            "_infer_entities_from_prompt",
        ]
        for name in expected_names:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(app_jobs, name, None)))


if __name__ == "__main__":
    unittest.main()
