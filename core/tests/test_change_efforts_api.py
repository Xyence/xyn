import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.routing import APIRouter as _FastapiRouterAlias  # noqa: F401
from starlette.routing import Router as StarletteRouter
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from core import models
from core.database import SessionLocal


class ChangeEffortsApiTests(unittest.TestCase):
    @staticmethod
    def _patch_router_compat() -> None:
        import inspect
        import starlette.routing as starlette_routing

        if "on_startup" in inspect.signature(starlette_routing.Router.__init__).parameters:
            return
        if getattr(starlette_routing.Router, "_xyn_compat_patched", False):
            return
        original_init = starlette_routing.Router.__init__

        def _compat_init(self, *args, on_startup=None, on_shutdown=None, lifespan=None, **kwargs):
            return original_init(self, *args, **kwargs)

        starlette_routing.Router.__init__ = _compat_init  # type: ignore[assignment]
        setattr(starlette_routing.Router, "_xyn_compat_patched", True)

    def setUp(self):
        self._patch_router_compat()
        self.db = SessionLocal()
        try:
            self.db.execute(text("SELECT 1"))
        except OperationalError as exc:
            self.db.close()
            raise unittest.SkipTest(f"PostgreSQL unavailable for API tests: {exc}") from exc
        models.ChangeEffort.__table__.create(bind=self.db.get_bind(), checkfirst=True)
        self.app = FastAPI()
        from core.api.change_efforts import router as change_efforts_router
        self.app.include_router(change_efforts_router, prefix="/api/v1")
        self.client = TestClient(self.app)

        self.workspace = models.Workspace(
            id=uuid.uuid4(),
            slug=f"effort-api-{uuid.uuid4().hex[:8]}",
            title="Effort API Workspace",
        )
        self.db.add(self.workspace)
        self.db.commit()
        self.db.refresh(self.workspace)
        self.temp_dir = tempfile.TemporaryDirectory(prefix="xyn-effort-worktree-")

    def tearDown(self):
        self.db.query(models.ChangeEffort).filter(models.ChangeEffort.workspace_id == self.workspace.id).delete(synchronize_session=False)
        self.db.query(models.Artifact).filter(models.Artifact.workspace_id == self.workspace.id).delete(synchronize_session=False)
        self.db.query(models.Workspace).filter(models.Workspace.id == self.workspace.id).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()
        self.client.close()
        self.temp_dir.cleanup()

    def _create_effort(self, artifact_slug: str = "xyn-api") -> dict:
        response = self.client.post(
            "/api/v1/change-efforts",
            json={
                "workspace_id": str(self.workspace.id),
                "artifact_slug": artifact_slug,
            },
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["change_effort"]

    def test_create_effort_defaults(self):
        effort = self._create_effort()
        self.assertEqual(effort["base_branch"], "develop")
        self.assertEqual(effort["target_branch"], "develop")
        self.assertEqual(effort["status"], "created")

    def test_allocate_branch_is_idempotent(self):
        effort = self._create_effort()
        effort_id = effort["id"]

        first = self.client.post(f"/api/v1/change-efforts/{effort_id}/allocate-branch", json={})
        self.assertEqual(first.status_code, 200)
        first_branch = first.json()["change_effort"]["work_branch"]
        self.assertTrue(first_branch.startswith("xyn/xyn-api/"))

        second = self.client.post(f"/api/v1/change-efforts/{effort_id}/allocate-branch", json={})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["change_effort"]["work_branch"], first_branch)

    def test_allocate_worktree_is_idempotent(self):
        effort = self._create_effort()
        effort_id = effort["id"]
        self.client.post(f"/api/v1/change-efforts/{effort_id}/allocate-branch", json={})

        first = self.client.post(
            f"/api/v1/change-efforts/{effort_id}/allocate-worktree",
            json={"root_path": self.temp_dir.name},
        )
        self.assertEqual(first.status_code, 200)
        first_path = first.json()["change_effort"]["worktree_path"]
        self.assertTrue(Path(first_path).exists())

        second = self.client.post(
            f"/api/v1/change-efforts/{effort_id}/allocate-worktree",
            json={"root_path": self.temp_dir.name},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["change_effort"]["worktree_path"], first_path)

    def test_resolve_source_missing_provenance_returns_conflict(self):
        effort = self._create_effort(artifact_slug="xyn-api")
        artifact = models.Artifact(
            id=uuid.uuid4(),
            workspace_id=self.workspace.id,
            name="xyn-api",
            kind="bundle",
            storage_scope="instance-local",
            sync_state="local",
            content_type="application/zip",
            byte_length=10,
            created_by="test",
            storage_path="/tmp/nope",
            extra_metadata={"generated_artifact_slug": "xyn-api"},
        )
        self.db.add(artifact)
        self.db.commit()

        response = self.client.post(f"/api/v1/change-efforts/{effort['id']}/resolve-source")
        self.assertEqual(response.status_code, 409)
        self.assertIn("source.kind=git", response.json()["detail"])

    def test_invalid_target_branch_rejected(self):
        response = self.client.post(
            "/api/v1/change-efforts",
            json={
                "workspace_id": str(self.workspace.id),
                "artifact_slug": "xyn-api",
                "target_branch": "xyn/feature/123",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("target_branch", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
