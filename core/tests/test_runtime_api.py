import json
import os
import uuid
import unittest
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from core import models
from core.database import SessionLocal
from core.kernel_app import create_app
from core.runtime_contract import RunPayloadV1
from core.runtime_execution import capture_run_artifact, create_runtime_run, report_run_step


class RuntimeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "run_payload_v1.json"
        cls.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def setUp(self):
        self._prev_runtime_worker = os.environ.get("XYN_RUNTIME_WORKER_ENABLED")
        os.environ["XYN_RUNTIME_WORKER_ENABLED"] = "false"
        self.db = SessionLocal()
        self.run_ids = []
        self.step_ids = []
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        if self.step_ids:
            self.db.query(models.Event).filter(models.Event.step_id.in_(self.step_ids)).delete(synchronize_session=False)
        if self.run_ids:
            self.db.query(models.Event).filter(models.Event.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Artifact).filter(models.Artifact.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Step).filter(models.Step.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Run).filter(models.Run.id.in_(self.run_ids)).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()
        self.client.close()
        if self._prev_runtime_worker is None:
            os.environ.pop("XYN_RUNTIME_WORKER_ENABLED", None)
        else:
            os.environ["XYN_RUNTIME_WORKER_ENABLED"] = self._prev_runtime_worker

    def _run_payload(self, *, status: models.RunStatus = models.RunStatus.RUNNING, thread_id: str = "") -> models.Run:
        payload = dict(self.fixture)
        payload["run_id"] = str(uuid.uuid4())
        payload["work_item_id"] = f"wi-{uuid.uuid4()}"
        payload["target"] = dict(payload["target"])
        payload["target"]["workspace_id"] = str(uuid.uuid4())
        payload["context"] = dict(payload["context"])
        metadata = dict(payload["context"].get("metadata") or {})
        if thread_id:
            metadata["thread_id"] = thread_id
        payload["context"]["metadata"] = metadata
        model = RunPayloadV1.model_validate(payload)
        run = create_runtime_run(self.db, model)
        run.status = status
        run.created_at = datetime.utcnow()
        run.started_at = datetime.utcnow()
        run.heartbeat_at = datetime.utcnow()
        self.run_ids.append(run.id)
        self.db.commit()
        return run

    def test_active_runs_query_returns_expected_fields(self):
        run = self._run_payload(status=models.RunStatus.RUNNING)

        response = self.client.get("/api/v1/runs", params={"status": "running"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        match = next(item for item in payload["items"] if item["id"] == str(run.id))
        self.assertEqual(match["work_item_id"], run.work_item_id)
        self.assertEqual(match["worker_type"], run.worker_type)
        self.assertIn("prompt_payload", match)
        self.assertIn("execution_policy", match)

    def test_runtime_run_submission_validates_and_persists_payload(self):
        payload = dict(self.fixture)
        payload["run_id"] = str(uuid.uuid4())
        payload["work_item_id"] = f"wi-{uuid.uuid4()}"
        payload["target"] = dict(payload["target"])
        payload["target"]["workspace_id"] = str(uuid.uuid4())

        response = self.client.post("/api/v1/runtime/runs", json=payload)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.run_ids.append(uuid.UUID(body["id"]))
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["work_item_id"], payload["work_item_id"])
        self.assertEqual(body["prompt_payload"]["target"]["repo"], payload["target"]["repo"])

        invalid = dict(payload)
        invalid["run_id"] = str(uuid.uuid4())
        invalid["requested_outputs"] = ["patch", "nonsense"]
        invalid_response = self.client.post("/api/v1/runtime/runs", json=invalid)
        self.assertEqual(invalid_response.status_code, 422)

    def test_run_detail_returns_steps_and_artifacts_coherently(self):
        run = self._run_payload(status=models.RunStatus.COMPLETED)
        step = report_run_step(
            self.db,
            run_id=run.id,
            step_key="inspect_repository",
            label="Inspect repository",
            status="running",
            sequence_no=1,
            summary="Started",
        )
        self.step_ids.append(step.id)
        report_run_step(
            self.db,
            run_id=run.id,
            step_key="inspect_repository",
            label="Inspect repository",
            status="completed",
            sequence_no=1,
            summary="Completed",
        )
        capture_run_artifact(
            self.db,
            run_id=run.id,
            artifact_type="summary",
            label="Final summary",
            content="done",
            file_name="final_summary.md",
        )
        self.db.commit()

        detail = self.client.get(f"/api/v1/runs/{run.id}")
        steps = self.client.get(f"/api/v1/runs/{run.id}/steps")
        artifacts = self.client.get(f"/api/v1/runs/{run.id}/artifacts")

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(steps.status_code, 200)
        self.assertEqual(artifacts.status_code, 200)
        self.assertEqual(detail.json()["status"], "completed")
        self.assertEqual(steps.json()[0]["step_key"], "inspect_repository")
        self.assertEqual(artifacts.json()[0]["artifact_type"], "summary")
        self.assertTrue(artifacts.json()[0]["uri"].endswith("/final_summary.md"))

    def test_runtime_artifact_content_endpoint_returns_registered_artifact(self):
        run = self._run_payload(status=models.RunStatus.COMPLETED)
        artifact = capture_run_artifact(
            self.db,
            run_id=run.id,
            artifact_type="summary",
            label="Final summary",
            content="done",
            file_name="final_summary.md",
        )
        self.db.commit()

        response = self.client.get(f"/api/v1/runs/{run.id}/artifacts/{artifact.id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["artifact_id"], str(artifact.id))
        self.assertEqual(payload["run_id"], str(run.id))
        self.assertEqual(payload["content"], "done")

    def test_terminal_states_surface_failure_and_escalation_fields(self):
        failed = self._run_payload(status=models.RunStatus.FAILED)
        failed.failure_reason = "worker_unresponsive"
        failed.summary = "Worker stopped heartbeating."
        blocked = self._run_payload(status=models.RunStatus.BLOCKED)
        blocked.escalation_reason = "contract_ambiguity"
        blocked.summary = "Need human review."
        self.db.commit()

        failed_payload = self.client.get(f"/api/v1/runs/{failed.id}").json()
        blocked_payload = self.client.get(f"/api/v1/runs/{blocked.id}").json()

        self.assertEqual(failed_payload["failure_reason"], "worker_unresponsive")
        self.assertEqual(blocked_payload["escalation_reason"], "contract_ambiguity")

    def test_pause_continue_and_retry_runtime_endpoints(self):
        queued = self._run_payload(status=models.RunStatus.QUEUED)
        pause_response = self.client.post(f"/api/v1/runs/{queued.id}/pause")
        self.assertEqual(pause_response.status_code, 200)
        paused_body = pause_response.json()
        self.assertEqual(paused_body["status"], "blocked")
        self.assertEqual(paused_body["escalation_reason"], "paused_by_user")

        continue_response = self.client.post(f"/api/v1/runs/{queued.id}/continue")
        self.assertEqual(continue_response.status_code, 200)
        self.assertEqual(continue_response.json()["status"], "queued")

        failed = self._run_payload(status=models.RunStatus.FAILED)
        retry_response = self.client.post(f"/api/v1/runs/{failed.id}/retry")
        self.assertEqual(retry_response.status_code, 201)
        retry_body = retry_response.json()
        retry_id = uuid.UUID(retry_body["id"])
        self.run_ids.append(retry_id)
        self.assertEqual(retry_body["status"], "queued")
        self.assertNotEqual(retry_body["id"], str(failed.id))

    def test_event_stream_emits_runtime_events_with_workspace_filter_and_resume(self):
        run = self._run_payload(status=models.RunStatus.RUNNING, thread_id="thread-1")
        step = report_run_step(
            self.db,
            run_id=run.id,
            step_key="inspect_repository",
            label="Inspect repository",
            status="running",
            sequence_no=1,
            summary="Started",
        )
        step = report_run_step(
            self.db,
            run_id=run.id,
            step_key="inspect_repository",
            label="Inspect repository",
            status="completed",
            sequence_no=1,
            summary="Completed",
        )
        self.step_ids.append(step.id)
        self.db.commit()

        with self.client.stream(
            "GET",
            "/api/v1/events/stream",
            params={"workspace_id": run.prompt_payload["target"]["workspace_id"], "runtime_only": "true", "once": "true"},
        ) as response:
            self.assertEqual(response.status_code, 200)
            body = "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in response.iter_text())
        self.assertIn("event: run.step.completed", body)
        self.assertIn(f"\"run_id\": \"{run.id}\"", body)
        self.assertIn("\"thread_id\": \"thread-1\"", body)

        event = self.db.query(models.Event).filter(models.Event.step_id == step.id).order_by(models.Event.created_at.asc()).first()
        with self.client.stream(
            "GET",
            "/api/v1/events/stream",
            params={
                "workspace_id": run.prompt_payload["target"]["workspace_id"],
                "runtime_only": "true",
                "last_event_id": str(event.id),
                "once": "true",
            },
        ) as resumed:
            resumed_body = "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in resumed.iter_text())
        self.assertNotIn(str(event.id), resumed_body)


if __name__ == "__main__":
    unittest.main()
