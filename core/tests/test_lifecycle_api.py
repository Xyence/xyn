import os
import unittest
import uuid

from fastapi.testclient import TestClient

from core import models
from core.database import SessionLocal, engine
from core.kernel_app import create_app
from core.workspaces import ensure_default_workspace
from sqlalchemy import text


class LifecycleApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS lifecycle_transitions (
                      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                      workspace_id UUID NULL REFERENCES workspaces(id) ON DELETE SET NULL,
                      lifecycle_name VARCHAR(128) NOT NULL,
                      object_type VARCHAR(128) NOT NULL,
                      object_id VARCHAR(255) NOT NULL,
                      from_state VARCHAR(64),
                      to_state VARCHAR(64) NOT NULL,
                      actor VARCHAR(255),
                      reason TEXT,
                      metadata_json JSON NOT NULL DEFAULT '{}'::json,
                      correlation_id VARCHAR(255),
                      run_id UUID NULL REFERENCES runs(id) ON DELETE SET NULL,
                      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )

    def setUp(self):
        self._prev_runtime_worker = os.environ.get("XYN_RUNTIME_WORKER_ENABLED")
        os.environ["XYN_RUNTIME_WORKER_ENABLED"] = "false"
        self.db = SessionLocal()
        self.workspace = ensure_default_workspace(self.db)
        self.app = create_app()
        self.client = TestClient(self.app)
        self.created_draft_ids: list[uuid.UUID] = []
        self.created_job_ids: list[uuid.UUID] = []

    def tearDown(self):
        if self.created_job_ids:
            self.db.query(models.Job).filter(models.Job.id.in_(self.created_job_ids)).delete(synchronize_session=False)
        if self.created_draft_ids:
            self.db.query(models.Draft).filter(models.Draft.id.in_(self.created_draft_ids)).delete(synchronize_session=False)
        all_ids = [str(value) for value in self.created_draft_ids + self.created_job_ids]
        if all_ids:
            self.db.query(models.LifecycleTransition).filter(
                models.LifecycleTransition.object_id.in_(all_ids)
            ).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()
        self.client.close()
        if self._prev_runtime_worker is None:
            os.environ.pop("XYN_RUNTIME_WORKER_ENABLED", None)
        else:
            os.environ["XYN_RUNTIME_WORKER_ENABLED"] = self._prev_runtime_worker

    def _ctx(self) -> dict[str, str]:
        return {"workspace_slug": "default"}

    def test_draft_and_job_transitions_are_enforced_and_visible(self):
        create_response = self.client.post(
            "/api/v1/drafts",
            params=self._ctx(),
            json={"title": "Lifecycle Draft", "status": "draft", "created_by": "api-user"},
        )
        self.assertEqual(create_response.status_code, 201)
        draft_id = uuid.UUID(create_response.json()["id"])
        self.created_draft_ids.append(draft_id)

        invalid_response = self.client.patch(
            f"/api/v1/drafts/{draft_id}",
            params=self._ctx(),
            json={"status": "draft"},
        )
        self.assertEqual(invalid_response.status_code, 400)
        self.assertIn("Illegal transition", invalid_response.json().get("detail", ""))

        ready_response = self.client.patch(
            f"/api/v1/drafts/{draft_id}",
            params=self._ctx(),
            json={"status": "ready"},
        )
        self.assertEqual(ready_response.status_code, 200)

        submit_response = self.client.post(f"/api/v1/drafts/{draft_id}/submit", params=self._ctx())
        self.assertEqual(submit_response.status_code, 200)
        job_id = uuid.UUID(submit_response.json()["job_id"])
        self.created_job_ids.append(job_id)

        transitions_response = self.client.get(
            "/api/v1/lifecycle/transitions",
            params={**self._ctx(), "object_type": "draft", "object_id": str(draft_id)},
        )
        self.assertEqual(transitions_response.status_code, 200)
        transitions = transitions_response.json()
        states = [row["to_state"] for row in transitions]
        self.assertIn("draft", states)
        self.assertIn("ready", states)
        self.assertIn("submitted", states)

        bad_job_transition = self.client.patch(
            f"/api/v1/jobs/{job_id}",
            params=self._ctx(),
            json={"status": "queued"},
        )
        self.assertEqual(bad_job_transition.status_code, 400)

        good_job_transition = self.client.patch(
            f"/api/v1/jobs/{job_id}",
            params=self._ctx(),
            json={"status": "running"},
        )
        self.assertEqual(good_job_transition.status_code, 200)

        job_history = self.client.get(
            "/api/v1/lifecycle/transitions",
            params={**self._ctx(), "object_type": "job", "object_id": str(job_id)},
        )
        self.assertEqual(job_history.status_code, 200)
        job_states = [row["to_state"] for row in job_history.json()]
        self.assertIn("queued", job_states)
        self.assertIn("running", job_states)


if __name__ == "__main__":
    unittest.main()
