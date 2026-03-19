import unittest
import uuid

from core import models
from core.database import SessionLocal, engine
from core.lifecycle.service import InvalidTransitionError, apply_transition
from core.workspaces import ensure_default_workspace
from sqlalchemy import text


class LifecycleServiceTests(unittest.TestCase):
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
        self.db = SessionLocal()
        self.workspace = ensure_default_workspace(self.db)
        self.object_id = str(uuid.uuid4())

    def tearDown(self):
        self.db.query(models.LifecycleTransition).filter(
            models.LifecycleTransition.object_id == self.object_id
        ).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def test_legal_transition_is_recorded(self):
        apply_transition(
            self.db,
            lifecycle="draft",
            object_type="draft",
            object_id=self.object_id,
            from_state=None,
            to_state="draft",
            workspace_id=self.workspace.id,
            actor="tester",
            reason="Created draft",
            metadata={"path": "create"},
        )
        apply_transition(
            self.db,
            lifecycle="draft",
            object_type="draft",
            object_id=self.object_id,
            from_state="draft",
            to_state="ready",
            workspace_id=self.workspace.id,
            actor="tester",
            reason="Validated",
        )
        self.db.commit()

        rows = (
            self.db.query(models.LifecycleTransition)
            .filter(models.LifecycleTransition.object_id == self.object_id)
            .order_by(models.LifecycleTransition.created_at.asc())
            .all()
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].from_state, None)
        self.assertEqual(rows[0].to_state, "draft")
        self.assertEqual(rows[1].from_state, "draft")
        self.assertEqual(rows[1].to_state, "ready")
        self.assertEqual(rows[0].metadata_json.get("path"), "create")

    def test_illegal_transition_is_rejected(self):
        with self.assertRaises(InvalidTransitionError):
            apply_transition(
                self.db,
                lifecycle="job",
                object_type="job",
                object_id=self.object_id,
                from_state="queued",
                to_state="succeeded",
                workspace_id=self.workspace.id,
            )


if __name__ == "__main__":
    unittest.main()
