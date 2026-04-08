import os
import tempfile
import unittest
import uuid
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from core import models
from core.database import SessionLocal
from core.api.environments import router as environments_router
from core.db_tenancy import DatabaseAllocation


class EnvironmentsApiTests(unittest.TestCase):
    def setUp(self):
        self._prev_runtime_worker = os.environ.get("XYN_RUNTIME_WORKER_ENABLED")
        self._prev_app_job_worker = os.environ.get("XYN_APP_JOB_WORKER_ENABLED")
        os.environ["XYN_RUNTIME_WORKER_ENABLED"] = "false"
        os.environ["XYN_APP_JOB_WORKER_ENABLED"] = "false"

        self.db = SessionLocal()
        try:
            self.db.execute(text("SELECT 1"))
        except OperationalError as exc:
            self.db.close()
            raise unittest.SkipTest(f"PostgreSQL unavailable for API tests: {exc}") from exc
        self.app = FastAPI()
        self.app.include_router(environments_router, prefix="/api/v1")
        self.client = TestClient(self.app)
        self.temp_paths: list[str] = []

        self.workspace = models.Workspace(
            id=uuid.uuid4(),
            slug=f"env-api-{uuid.uuid4().hex[:8]}",
            title="Env API Test Workspace",
        )
        self.db.add(self.workspace)
        self.db.commit()
        self.db.refresh(self.workspace)

    def tearDown(self):
        for path in self.temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass
        self.db.query(models.Workspace).filter(models.Workspace.id == self.workspace.id).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()
        self.client.close()
        if self._prev_runtime_worker is None:
            os.environ.pop("XYN_RUNTIME_WORKER_ENABLED", None)
        else:
            os.environ["XYN_RUNTIME_WORKER_ENABLED"] = self._prev_runtime_worker
        if self._prev_app_job_worker is None:
            os.environ.pop("XYN_APP_JOB_WORKER_ENABLED", None)
        else:
            os.environ["XYN_APP_JOB_WORKER_ENABLED"] = self._prev_app_job_worker

    def _create_environment(self, *, slug: str = "development", kind: str = "dev") -> models.Environment:
        row = models.Environment(
            id=uuid.uuid4(),
            workspace_id=self.workspace.id,
            slug=slug,
            title=slug.replace("-", " ").title(),
            kind=kind,
            status="active",
            is_ephemeral=False,
            metadata_json={},
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def _create_generate_job(
        self,
        *,
        artifact_slug: str = "app.net-inventory",
        revision_id: str = "rev_123",
        package_exists: bool = True,
    ) -> models.Job:
        package_path = ""
        if package_exists:
            tmp = tempfile.NamedTemporaryFile(prefix="xyn-generated-package-", suffix=".zip", delete=False)
            tmp.write(b"zip-content")
            tmp.flush()
            tmp.close()
            package_path = tmp.name
            self.temp_paths.append(package_path)
        job = models.Job(
            id=uuid.uuid4(),
            workspace_id=self.workspace.id,
            type="generate_app_spec",
            status=models.JobStatus.SUCCEEDED.value,
            input_json={},
            output_json={
                "app_spec": {
                    "app_slug": "net-inventory",
                    "title": "Net Inventory",
                    "workspace_id": str(self.workspace.id),
                    "services": [{"name": "net-inventory-api"}],
                },
                "policy_bundle": {"policy_families": []},
                "generated_artifact": {
                    "artifact_slug": artifact_slug,
                    "artifact_version": "1.0.0",
                    "revision_id": revision_id,
                    "artifact_package_path": package_path,
                },
                "policy_source": "reconstructed",
            },
            logs_text="generated ok",
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def test_list_environments(self):
        env = self._create_environment(slug="development", kind="dev")

        response = self.client.get(
            "/api/v1/environments",
            params={"workspace_id": str(self.workspace.id), "kind": "dev", "status": "active"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("environments", payload)
        self.assertEqual(len(payload["environments"]), 1)
        self.assertEqual(payload["environments"][0]["id"], str(env.id))

    def test_create_environment(self):
        response = self.client.post(
            "/api/v1/environments",
            json={
                "workspace_id": str(self.workspace.id),
                "slug": "preview-123",
                "title": "Preview 123",
                "kind": "preview",
                "is_ephemeral": True,
                "ttl_hours": 24,
            },
        )
        self.assertEqual(response.status_code, 201)
        payload = response.json()
        self.assertEqual(payload["slug"], "preview-123")
        self.assertEqual(payload["kind"], "preview")
        self.assertTrue(payload["is_ephemeral"])
        self.assertIsNotNone(payload["ttl_expires_at"])

        duplicate = self.client.post(
            "/api/v1/environments",
            json={
                "workspace_id": str(self.workspace.id),
                "slug": "preview-123",
                "title": "Preview 123b",
                "kind": "preview",
                "is_ephemeral": True,
            },
        )
        self.assertEqual(duplicate.status_code, 409)

    def test_list_siblings_for_environment(self):
        env = self._create_environment()
        sibling = models.Sibling(
            id=uuid.uuid4(),
            environment_id=env.id,
            workspace_id=self.workspace.id,
            name="dev-shell",
            status="active",
            compose_project="xyn-dev-shell",
            ui_url="http://example.localhost",
            api_url="http://api.example.localhost",
            runtime_base_url="http://runtime.localhost",
            workspace_app_instance_id="instance-1",
            runtime_target_json={},
            runtime_registration_json={},
            metadata_json={},
        )
        self.db.add(sibling)
        self.db.commit()

        response = self.client.get(f"/api/v1/environments/{env.id}/siblings")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["siblings"]), 1)
        self.assertEqual(payload["siblings"][0]["id"], str(sibling.id))
        self.assertEqual(payload["siblings"][0]["status"], "active")

    @mock.patch("core.api.environments._provision_local_instance")
    @mock.patch("core.api.environments.allocate_database")
    def test_spawn_sibling_creates_state(self, allocate_database_mock: mock.Mock, provision_local_instance_mock: mock.Mock):
        env = self._create_environment()
        allocate_database_mock.return_value = mock.Mock(
            database_url="postgresql://tenant:pw@db.example.internal:5432/xyn_sibling_1",
            to_public_dict=lambda: {"mode": "external", "tenancy_mode": "shared_rds_db_per_sibling"},
        )
        provision_local_instance_mock.return_value = {
            "deployment_id": "dep-123",
            "status": "succeeded",
            "compose_project": "xyn-dev-shell",
            "ui_url": "http://dev-shell.localhost",
            "api_url": "http://api.dev-shell.localhost",
            "runtime_target": {"runtime_base_url": "http://runtime.dev-shell.localhost", "public_app_url": "http://dev-shell.localhost"},
            "runtime_registration": {"instance": {"id": "inst-123"}},
        }

        response = self.client.post(
            f"/api/v1/environments/{env.id}/siblings/spawn",
            json={"name": "dev-shell", "workspace_slug": self.workspace.slug, "force": False},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["operation"], "spawn_sibling")
        self.assertEqual(payload["sibling"]["compose_project"], "xyn-dev-shell")
        self.assertEqual(payload["sibling"]["status"], "ready")

        persisted = (
            self.db.query(models.Sibling)
            .filter(models.Sibling.environment_id == env.id, models.Sibling.name == "dev-shell")
            .order_by(models.Sibling.updated_at.desc())
            .first()
        )
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "ready")
        self.assertEqual(persisted.compose_project, "xyn-dev-shell")
        self.assertIn("database_allocation", payload["provision_output"])

    @mock.patch("core.api.environments._provision_local_instance")
    @mock.patch("core.api.environments.allocate_database")
    def test_spawn_local_db_mode_keeps_empty_database_url(
        self,
        allocate_database_mock: mock.Mock,
        provision_local_instance_mock: mock.Mock,
    ):
        env = self._create_environment()
        allocate_database_mock.return_value = DatabaseAllocation(mode="local", tenancy_mode="local_compose")
        provision_local_instance_mock.return_value = {
            "deployment_id": "dep-123",
            "status": "succeeded",
            "compose_project": "xyn-dev-shell",
            "ui_url": "http://dev-shell.localhost",
            "api_url": "http://api.dev-shell.localhost",
        }
        response = self.client.post(
            f"/api/v1/environments/{env.id}/siblings/spawn",
            json={"name": "dev-shell", "workspace_slug": self.workspace.slug, "force": False},
        )
        self.assertEqual(response.status_code, 200)
        kwargs = provision_local_instance_mock.call_args.kwargs
        self.assertEqual(str(kwargs.get("database_url") or ""), "")

    @mock.patch("core.api.environments.allocate_database")
    def test_spawn_db_allocation_failure_returns_clear_error(self, allocate_database_mock: mock.Mock):
        env = self._create_environment()
        allocate_database_mock.side_effect = RuntimeError("Failed to allocate external tenant database (target=postgresql://***@db:5432/postgres).")
        response = self.client.post(
            f"/api/v1/environments/{env.id}/siblings/spawn",
            json={"name": "dev-shell", "workspace_slug": self.workspace.slug, "force": True},
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("Failed to allocate external tenant database", str(response.json().get("detail")))

    @mock.patch("core.api.environments._provision_local_instance")
    @mock.patch("core.api.environments.allocate_database")
    def test_spawn_same_name_conflict_returns_existing_without_new_provision(
        self,
        allocate_database_mock: mock.Mock,
        provision_local_instance_mock: mock.Mock,
    ):
        env = self._create_environment()
        existing = models.Sibling(
            id=uuid.uuid4(),
            environment_id=env.id,
            workspace_id=self.workspace.id,
            name="dev-shell",
            status="active",
            compose_project="xyn-dev-shell",
            runtime_target_json={},
            runtime_registration_json={},
            metadata_json={},
        )
        self.db.add(existing)
        self.db.commit()

        response = self.client.post(
            f"/api/v1/environments/{env.id}/siblings/spawn",
            json={"name": "dev-shell", "workspace_slug": self.workspace.slug, "force": False},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["provision_output"]["status"], "existing")
        provision_local_instance_mock.assert_not_called()
        allocate_database_mock.assert_not_called()

    @mock.patch("core.api.environments.restart_project")
    def test_restart_sibling_updates_status(self, restart_project_mock: mock.Mock):
        env = self._create_environment()
        sibling = models.Sibling(
            id=uuid.uuid4(),
            environment_id=env.id,
            workspace_id=self.workspace.id,
            name="dev-shell",
            status="stopped",
            compose_project="xyn-dev-shell",
            runtime_target_json={},
            runtime_registration_json={},
            metadata_json={},
        )
        self.db.add(sibling)
        self.db.commit()

        restart_project_mock.return_value = (True, "restarted")
        response = self.client.post(f"/api/v1/siblings/{sibling.id}/restart", json={"remove_volumes": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "active")

        self.db.refresh(sibling)
        self.assertEqual(sibling.status, "active")

    @mock.patch("core.api.environments.stop_project")
    def test_stop_sibling_updates_status(self, stop_project_mock: mock.Mock):
        env = self._create_environment()
        sibling = models.Sibling(
            id=uuid.uuid4(),
            environment_id=env.id,
            workspace_id=self.workspace.id,
            name="dev-shell",
            status="active",
            compose_project="xyn-dev-shell",
            runtime_target_json={},
            runtime_registration_json={},
            metadata_json={},
        )
        self.db.add(sibling)
        self.db.commit()

        stop_project_mock.return_value = (True, "stopped")
        response = self.client.post(f"/api/v1/siblings/{sibling.id}/stop", json={"remove_volumes": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "stopped")

        self.db.refresh(sibling)
        self.assertEqual(sibling.status, "stopped")

    def test_activate_artifact_creates_activation_and_enqueues_job(self):
        env = self._create_environment()
        self._create_generate_job(artifact_slug="app.net-inventory", revision_id="rev_123")

        response = self.client.post(
            f"/api/v1/environments/{env.id}/activate-artifact",
            json={"artifact_slug": "app.net-inventory", "revision_id": "rev_123"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["environment_id"], str(env.id))
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["job_type"], "deploy_app_local")
        self.assertTrue(payload["job_id"])

        activation = self.db.query(models.Activation).filter(models.Activation.id == uuid.UUID(payload["activation_id"])).first()
        self.assertIsNotNone(activation)
        self.assertEqual(activation.artifact_slug, "app.net-inventory")
        self.assertEqual(activation.status, "pending")
        self.assertIsNotNone(activation.source_job_id)

        job = self.db.query(models.Job).filter(models.Job.id == activation.source_job_id).first()
        self.assertIsNotNone(job)
        self.assertEqual(job.type, "deploy_app_local")
        self.assertEqual(str((job.input_json or {}).get("environment_id") or ""), str(env.id))
        self.assertEqual(str((job.input_json or {}).get("activation_id") or ""), str(activation.id))

    def test_activate_artifact_idempotency_returns_existing_without_second_job(self):
        env = self._create_environment()
        self._create_generate_job(artifact_slug="app.net-inventory", revision_id="rev_123")
        key = "idem-1"

        first = self.client.post(
            f"/api/v1/environments/{env.id}/activate-artifact",
            json={"artifact_slug": "app.net-inventory", "revision_id": "rev_123", "idempotency_key": key},
        )
        self.assertEqual(first.status_code, 200)
        first_payload = first.json()

        second = self.client.post(
            f"/api/v1/environments/{env.id}/activate-artifact",
            json={"artifact_slug": "app.net-inventory", "revision_id": "rev_123", "idempotency_key": key},
        )
        self.assertEqual(second.status_code, 200)
        second_payload = second.json()
        self.assertEqual(first_payload["activation_id"], second_payload["activation_id"])
        self.assertEqual(first_payload["job_id"], second_payload["job_id"])

        deploy_jobs = (
            self.db.query(models.Job)
            .filter(models.Job.workspace_id == self.workspace.id, models.Job.type == "deploy_app_local")
            .all()
        )
        self.assertEqual(len(deploy_jobs), 1)

    def test_activate_artifact_missing_revision_returns_error(self):
        env = self._create_environment()
        self._create_generate_job(artifact_slug="app.net-inventory", revision_id="rev_aaa")
        before_count = self.db.query(models.Activation).filter(models.Activation.environment_id == env.id).count()
        response = self.client.post(
            f"/api/v1/environments/{env.id}/activate-artifact",
            json={"artifact_slug": "app.net-inventory", "revision_id": "rev_zzz"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("No successful generate_app_spec output found", str(response.json().get("detail")))
        after_rows = (
            self.db.query(models.Activation)
            .filter(models.Activation.environment_id == env.id)
            .order_by(models.Activation.created_at.desc())
            .all()
        )
        self.assertEqual(len(after_rows), before_count + 1)
        self.assertEqual(after_rows[0].status, "failed")

    def test_activate_artifact_workspace_instance_propagates_revision_anchor(self):
        env = self._create_environment()
        self._create_generate_job(artifact_slug="app.net-inventory", revision_id="rev_123")
        response = self.client.post(
            f"/api/v1/environments/{env.id}/activate-artifact",
            json={
                "artifact_slug": "app.net-inventory",
                "revision_id": "rev_123",
                "workspace_app_instance_id": "inst-001",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        activation = self.db.query(models.Activation).filter(models.Activation.id == uuid.UUID(payload["activation_id"])).first()
        self.assertIsNotNone(activation)
        self.assertEqual(activation.workspace_app_instance_id, "inst-001")

        job = self.db.query(models.Job).filter(models.Job.id == activation.source_job_id).first()
        self.assertIsNotNone(job)
        app_spec = (job.input_json or {}).get("app_spec") if isinstance((job.input_json or {}).get("app_spec"), dict) else {}
        revision_anchor = app_spec.get("revision_anchor") if isinstance(app_spec.get("revision_anchor"), dict) else {}
        self.assertEqual(str(revision_anchor.get("workspace_id") or ""), str(self.workspace.id))
        self.assertEqual(str(revision_anchor.get("artifact_slug") or ""), "app.net-inventory")
        self.assertEqual(str(revision_anchor.get("workspace_app_instance_id") or ""), "inst-001")


if __name__ == "__main__":
    unittest.main()
