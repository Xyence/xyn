import json
import uuid
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from core.database import SessionLocal
from core import models
from core.runtime_contract import RunPayloadV1, WorkerInfoPayload
from core.runtime_execution import (
    capture_run_artifact,
    complete_run,
    create_runtime_run,
    dispatch_queued_run,
    fail_run,
    handle_stale_heartbeats,
    record_run_heartbeat,
    report_run_step,
    transition_run_status,
)
from core.runtime_workers import register_worker


class RuntimeExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "run_payload_v1.json"
        cls.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.db = SessionLocal()
        self.run_ids = []
        self.worker_ids = []
        self.step_ids = []
        self.artifact_ids = []
        self.preexisting_workers = {
            row.worker_id: {
                "status": row.status,
                "active_run_id": row.active_run_id,
                "last_heartbeat": row.last_heartbeat,
            }
            for row in self.db.query(models.RuntimeWorker).all()
        }
        if self.preexisting_workers:
            self.db.query(models.RuntimeWorker).update(
                {
                    models.RuntimeWorker.status: models.RuntimeWorkerStatus.OFFLINE,
                    models.RuntimeWorker.active_run_id: None,
                },
                synchronize_session=False,
            )
            self.db.commit()

    def tearDown(self):
        if self.step_ids:
            self.db.query(models.Event).filter(models.Event.step_id.in_(self.step_ids)).delete(synchronize_session=False)
        if self.run_ids:
            self.db.query(models.RuntimeWorker).filter(models.RuntimeWorker.active_run_id.in_(self.run_ids)).update(
                {models.RuntimeWorker.active_run_id: None},
                synchronize_session=False,
            )
        if self.run_ids:
            self.db.query(models.Event).filter(models.Event.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Artifact).filter(models.Artifact.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Step).filter(models.Step.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Run).filter(models.Run.id.in_(self.run_ids)).delete(synchronize_session=False)
        if self.worker_ids:
            self.db.query(models.RuntimeWorker).filter(models.RuntimeWorker.worker_id.in_(self.worker_ids)).delete(synchronize_session=False)
        for worker_id, state in self.preexisting_workers.items():
            self.db.query(models.RuntimeWorker).filter(models.RuntimeWorker.worker_id == worker_id).update(
                {
                    models.RuntimeWorker.status: state["status"],
                    models.RuntimeWorker.active_run_id: state["active_run_id"],
                    models.RuntimeWorker.last_heartbeat: state["last_heartbeat"],
                },
                synchronize_session=False,
            )
        self.db.commit()
        self.db.close()

    def _payload(self) -> RunPayloadV1:
        payload = dict(self.fixture)
        payload["run_id"] = str(uuid.uuid4())
        payload["work_item_id"] = f"wi-{uuid.uuid4()}"
        payload["target"] = dict(payload["target"])
        payload["context"] = dict(payload["context"])
        payload["policy"] = dict(payload["policy"])
        return RunPayloadV1.model_validate(payload)

    def _register_worker(self, worker_type: str = "codex_local", status: str = "idle") -> models.RuntimeWorker:
        worker_id = f"runtime-test-{uuid.uuid4()}"
        row = register_worker(
            self.db,
            WorkerInfoPayload(
                worker_id=worker_id,
                worker_type=worker_type,
                runtime_environment="local-dev",
                status=status,
                last_heartbeat=datetime.utcnow(),
                capabilities=["repo_modification", "patch_creation"],
            ),
        )
        self.worker_ids.append(worker_id)
        self.db.commit()
        return row

    def test_run_dispatch_assigns_registered_worker(self):
        payload = self._payload()
        run = create_runtime_run(self.db, payload)
        self.run_ids.append(run.id)
        run.priority = -1000
        self._register_worker()
        self.db.commit()

        dispatched = dispatch_queued_run(self.db)
        self.db.commit()
        self.assertIsNotNone(dispatched)
        self.db.refresh(run)
        self.assertEqual(run.status, models.RunStatus.RUNNING)
        self.assertEqual(run.id, dispatched.id)
        self.assertEqual(dispatched.status, models.RunStatus.RUNNING)
        self.assertTrue(run.worker_id)
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.heartbeat_at)
        self.assertEqual(run.attempt_count, 1)

    def test_step_reporting_creates_and_transitions_steps(self):
        payload = self._payload()
        run = create_runtime_run(self.db, payload)
        self.run_ids.append(run.id)
        run.status = models.RunStatus.RUNNING
        run.started_at = datetime.utcnow()
        run.heartbeat_at = datetime.utcnow()
        self.db.commit()

        step = report_run_step(
            self.db,
            run_id=run.id,
            step_key="checkout",
            label="Checkout repository",
            status="running",
            sequence_no=1,
            summary="started",
        )
        self.step_ids.append(step.id)
        step = report_run_step(
            self.db,
            run_id=run.id,
            step_key="checkout",
            label="Checkout repository",
            status="completed",
            sequence_no=1,
            summary="done",
        )
        self.db.commit()

        self.assertEqual(step.status, models.StepStatus.COMPLETED)
        self.assertEqual(step.step_key, "checkout")
        self.assertEqual(step.summary, "done")
        events = self.db.query(models.Event).filter(models.Event.run_id == run.id).order_by(models.Event.created_at.asc()).all()
        self.assertIn("run.step.started", [event.event_name for event in events])
        self.assertIn("run.step.completed", [event.event_name for event in events])
        self.assertEqual(events[0].data.get("workspace_id"), payload.target.workspace_id)

    def test_artifact_capture_stores_run_artifact(self):
        payload = self._payload()
        run = create_runtime_run(self.db, payload)
        self.run_ids.append(run.id)
        self.db.commit()

        artifact = capture_run_artifact(
            self.db,
            run_id=run.id,
            artifact_type="summary",
            label="Execution summary",
            content={"summary": "ok"},
        )
        self.artifact_ids.append(artifact.id)
        self.db.commit()

        self.assertEqual(artifact.kind, "summary")
        self.assertTrue(str(artifact.storage_path).startswith("artifact://runs/"))
        self.assertIn("storage_key", dict(artifact.extra_metadata or {}))
        self.assertEqual(dict(artifact.extra_metadata or {}).get("storage_provider"), "local")
        event = self.db.query(models.Event).filter(
            models.Event.run_id == run.id,
            models.Event.event_name == "run.artifact.created",
        ).first()
        self.assertIsNotNone(event)

    def test_heartbeat_timeout_marks_run_failed(self):
        payload = self._payload()
        run = create_runtime_run(self.db, payload)
        self.run_ids.append(run.id)
        worker = self._register_worker(status="busy")
        run.status = models.RunStatus.RUNNING
        run.worker_id = worker.worker_id
        run.started_at = datetime.utcnow() - timedelta(minutes=2)
        run.heartbeat_at = datetime.utcnow() - timedelta(minutes=2)
        run.execution_policy = {"max_retries": 0, "require_human_review_on_failure": False}
        self.db.commit()

        updated = handle_stale_heartbeats(self.db, stale_after_seconds=30)
        self.db.commit()

        self.assertIn(run.id, [item.id for item in updated])
        self.db.refresh(run)
        self.assertEqual(run.status, models.RunStatus.FAILED)
        self.assertEqual(run.failure_reason, "worker_unresponsive")

    def test_retry_policy_requeues_once_then_blocks(self):
        payload = self._payload()
        run = create_runtime_run(self.db, payload)
        self.run_ids.append(run.id)
        worker = self._register_worker(status="busy")
        run.status = models.RunStatus.RUNNING
        run.worker_id = worker.worker_id
        run.started_at = datetime.utcnow() - timedelta(minutes=2)
        run.heartbeat_at = datetime.utcnow() - timedelta(minutes=2)
        run.execution_policy = {"max_retries": 1, "require_human_review_on_failure": True}
        run.attempt_count = 1
        self.db.commit()

        first = handle_stale_heartbeats(self.db, stale_after_seconds=30)
        self.db.commit()
        self.assertIn(run.id, [item.id for item in first])
        self.db.refresh(run)
        self.assertEqual(run.status, models.RunStatus.QUEUED)

        run.status = models.RunStatus.RUNNING
        run.worker_id = worker.worker_id
        run.heartbeat_at = datetime.utcnow() - timedelta(minutes=2)
        run.attempt_count = 2
        self.db.commit()

        second = handle_stale_heartbeats(self.db, stale_after_seconds=30)
        self.db.commit()
        self.assertIn(run.id, [item.id for item in second])
        self.db.refresh(run)
        self.assertEqual(run.status, models.RunStatus.BLOCKED)
        self.assertEqual(run.escalation_reason, "human_review_required")

    def test_contract_validation_rejects_invalid_payload_and_transitions(self):
        invalid = dict(self.fixture)
        invalid["requested_outputs"] = ["patch", "unknown"]
        with self.assertRaises(ValidationError):
            RunPayloadV1.model_validate(invalid)

        payload = self._payload()
        run = create_runtime_run(self.db, payload)
        self.run_ids.append(run.id)
        self.db.commit()
        with self.assertRaises(ValueError):
            transition_run_status(self.db, run, models.RunStatus.COMPLETED)
        with self.assertRaises(ValueError):
            capture_run_artifact(
                self.db,
                run_id=run.id,
                artifact_type="binary",
                label="Invalid",
                content="bad",
            )


if __name__ == "__main__":
    unittest.main()
