from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.app_jobs import _handle_provision_sibling_xyn


class ProvisionSiblingExtractionCharacterizationTests(unittest.TestCase):
    def test_provision_sibling_shim_preserves_patch_path_and_follow_up_shape(self):
        db = mock.Mock()
        workspace = SimpleNamespace(slug="development")
        db.query.return_value.filter.return_value.first.return_value = workspace
        note_id = str(uuid.uuid4())
        job = SimpleNamespace(
            id=uuid.uuid4(),
            type="provision_sibling_xyn",
            workspace_id=uuid.uuid4(),
            input_json={
                "execution_note_artifact_id": note_id,
                "deployment": {"app_slug": "tracker", "compose_project": "rootproj", "compose_path": "/tmp/root-compose.yml"},
                "app_spec": {"app_slug": "tracker", "title": "Tracker"},
                "policy_bundle": {"contracts": []},
                "generated_artifact": {
                    "artifact_slug": "app.tracker",
                    "artifact_version": "0.0.1-dev",
                },
            },
        )
        logs: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "app.tracker.zip"
            package_path.write_text("zip")
            job.input_json["generated_artifact"]["artifact_package_path"] = str(package_path)

            reused_sibling = {
                "deployment_id": "dep-1",
                "compose_project": "sibproj",
                "ui_url": "http://sib.localhost",
                "api_url": "http://api.sib.localhost",
                "runtime_target": {
                    "compose_project": "sib-runtime",
                    "external_network": "sibproj_default",
                    "network_alias": "sib-runtime-api",
                },
            }
            installed = {
                "workspace_id": str(job.workspace_id),
                "workspace_slug": "development",
                "artifact_slug": "app.tracker",
                "artifact_id": "art-1",
            }
            sibling_runtime = {
                "app_container_name": "sib-runtime-api",
                "runtime_base_url": "http://sib-runtime-api:8080",
                "app_url": "http://localhost:9090",
            }
            with mock.patch("core.app_jobs._find_revision_sibling_target", return_value=reused_sibling) as find_mock:
                with mock.patch("core.app_jobs._docker_container_running", return_value=True):
                    with mock.patch("core.app_jobs._import_generated_artifact_package_into_registry", return_value={"ok": True}):
                        with mock.patch("core.app_jobs._install_generated_artifact_in_sibling", return_value=installed):
                            with mock.patch("core.app_jobs._docker_network_exists", return_value=True):
                                with mock.patch("core.app_jobs._deployments_root", return_value=Path(tmpdir)):
                                    with mock.patch("core.app_jobs._deploy_generated_runtime", return_value=sibling_runtime):
                                        with mock.patch("core.app_jobs._register_sibling_runtime_target", return_value={"registered": True}):
                                            with mock.patch("core.app_jobs.update_execution_note", return_value=None):
                                                output_json, follow_up = _handle_provision_sibling_xyn(db, job, logs)

        find_mock.assert_called_once()
        self.assertEqual(output_json["ui_url"], "http://sib.localhost")
        self.assertEqual(output_json["installed_artifact"]["artifact_slug"], "app.tracker")
        self.assertEqual((output_json.get("capability_entry") or {}).get("source_of_truth"), "installed_artifact")
        self.assertEqual(((output_json.get("capability_entry") or {}).get("open_preference") or {}).get("mode"), "artifact_shell")
        self.assertIn("runtime_target", output_json)
        self.assertEqual(len(follow_up), 1)
        self.assertEqual(follow_up[0]["type"], "smoke_test")

    def test_provision_sibling_passes_allocated_database_url_to_local_provisioner(self):
        db = mock.Mock()
        workspace = SimpleNamespace(slug="development")
        environment_row = SimpleNamespace(id=uuid.uuid4(), workspace_id=uuid.uuid4())

        def _query_for(model):
            model_name = getattr(model, "__name__", "")
            if model_name == "Workspace":
                row = workspace
            elif model_name == "Environment":
                row = environment_row
            else:
                row = None
            query = mock.Mock()
            query.filter.return_value.first.return_value = row
            query.filter.return_value.order_by.return_value.first.return_value = row
            return query

        db.query.side_effect = _query_for
        note_id = str(uuid.uuid4())
        job = SimpleNamespace(
            id=uuid.uuid4(),
            type="provision_sibling_xyn",
            workspace_id=environment_row.workspace_id,
            input_json={
                "execution_note_artifact_id": note_id,
                "deployment": {"app_slug": "tracker", "compose_project": "rootproj", "compose_path": "/tmp/root-compose.yml"},
                "app_spec": {"app_slug": "tracker", "title": "Tracker"},
                "policy_bundle": {"contracts": []},
                "generated_artifact": {
                    "artifact_slug": "app.tracker",
                    "artifact_version": "0.0.1-dev",
                },
                "environment_id": str(environment_row.id),
            },
        )
        logs: list[str] = []
        allocation = SimpleNamespace(
            database_url="postgresql://tenant:pw@db.example.internal:5432/xyn_sibling_1",
            to_public_dict=lambda: {"mode": "external", "tenancy_mode": "shared_rds_db_per_sibling"},
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            package_path = Path(tmpdir) / "app.tracker.zip"
            package_path.write_text("zip")
            job.input_json["generated_artifact"]["artifact_package_path"] = str(package_path)

            installed = {
                "workspace_id": str(job.workspace_id),
                "workspace_slug": "development",
                "artifact_slug": "app.tracker",
                "artifact_id": "art-1",
            }
            sibling_runtime = {
                "app_container_name": "sib-runtime-api",
                "runtime_base_url": "http://sib-runtime-api:8080",
                "app_url": "http://localhost:9090",
            }
            with mock.patch("core.app_jobs._find_revision_sibling_target", return_value=None):
                with mock.patch("core.app_jobs.allocate_database", return_value=allocation):
                    with mock.patch(
                        "core.app_jobs.provision_local_instance",
                        return_value={
                            "deployment_id": "dep-1",
                            "compose_project": "sibproj",
                            "ui_url": "http://sib.localhost",
                            "api_url": "http://api.sib.localhost",
                        },
                    ) as provision_mock:
                        with mock.patch("core.app_jobs._docker_container_running", return_value=True):
                            with mock.patch("core.app_jobs._import_generated_artifact_package_into_registry", return_value={"ok": True}):
                                with mock.patch("core.app_jobs._install_generated_artifact_in_sibling", return_value=installed):
                                    with mock.patch("core.app_jobs._docker_network_exists", return_value=True):
                                        with mock.patch("core.app_jobs._deployments_root", return_value=Path(tmpdir)):
                                            with mock.patch("core.app_jobs._deploy_generated_runtime", return_value=sibling_runtime):
                                                with mock.patch("core.app_jobs._register_sibling_runtime_target", return_value={"registered": True}):
                                                    with mock.patch("core.app_jobs.update_execution_note", return_value=None):
                                                        _output_json, _follow_up = _handle_provision_sibling_xyn(db, job, logs)

        provision_request = provision_mock.call_args.args[0]
        self.assertEqual(
            str(getattr(provision_request, "database_url", "") or ""),
            "postgresql://tenant:pw@db.example.internal:5432/xyn_sibling_1",
        )
