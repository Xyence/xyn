import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.app_jobs import (
    _handle_deploy_app_local,
    _handle_generate_app_spec,
    _handle_provision_sibling_xyn,
    _handle_smoke_test,
)


class StageContractCharacterizationTests(unittest.TestCase):
    def _fake_db(self, workspace_slug: str = "development"):
        db = mock.Mock()
        workspace = SimpleNamespace(slug=workspace_slug)
        db.query.return_value.filter.return_value.first.return_value = workspace
        return db

    def test_generate_app_spec_output_and_follow_up_shape(self):
        db = self._fake_db("development")
        job = SimpleNamespace(
            id=uuid.uuid4(),
            type="generate_app_spec",
            workspace_id=uuid.uuid4(),
            input_json={"title": "Test Solution", "content_json": {"raw_prompt": "build a tracker"}},
        )
        logs = []

        app_spec = {"app_slug": "tracker", "title": "Tracker", "services": [{"name": "api", "image": "img", "ports": [8080]}]}
        policy_bundle = {"schema": "xyn.policy_bundle.v0", "contracts": []}
        diagnostics = {"structure_score": 0.9, "route": "A", "llm_used": False}
        note = SimpleNamespace(id=uuid.uuid4())
        packaged = {
            "artifact_slug": "app.tracker",
            "artifact_version": "0.0.1-dev",
            "artifact_package_path": "/tmp/app.tracker.zip",
        }

        with mock.patch("core.app_jobs.get_primitive_catalog", return_value=[]):
            with mock.patch("core.app_jobs.create_execution_note", return_value=note):
                with mock.patch("core.app_jobs._build_app_spec_with_diagnostics", return_value=(app_spec, diagnostics)):
                    with mock.patch("core.app_jobs.validate", return_value=None):
                        with mock.patch("core.app_jobs._build_policy_bundle", return_value=policy_bundle):
                            with mock.patch("core.app_jobs._persist_json_artifact", side_effect=["appspec-art", "policy-art"]):
                                with mock.patch("core.app_jobs._package_generated_app", return_value=packaged):
                                    with mock.patch("core.app_jobs._import_generated_artifact_package", return_value={"id": "reg-1"}):
                                        with mock.patch("core.app_jobs.update_execution_note", return_value=note):
                                            output_json, follow_up = _handle_generate_app_spec(db, job, logs)

        self.assertIn("app_spec", output_json)
        self.assertIn("policy_bundle", output_json)
        self.assertEqual(output_json["app_spec_artifact_id"], "appspec-art")
        self.assertEqual(output_json["policy_bundle_artifact_id"], "policy-art")
        self.assertEqual(output_json["execution_note_artifact_id"], str(note.id))
        self.assertEqual(len(follow_up), 1)
        self.assertEqual(follow_up[0]["type"], "deploy_app_local")
        self.assertIsInstance(follow_up[0].get("input_json"), dict)
        self.assertEqual(follow_up[0]["input_json"]["app_spec_artifact_id"], "appspec-art")
        self.assertEqual(follow_up[0]["input_json"]["policy_bundle_artifact_id"], "policy-art")

    def test_deploy_app_local_output_and_follow_up_shape(self):
        db = self._fake_db("development")
        note_id = str(uuid.uuid4())
        job = SimpleNamespace(
            id=uuid.uuid4(),
            type="deploy_app_local",
            workspace_id=uuid.uuid4(),
            input_json={
                "execution_note_artifact_id": note_id,
                "generated_artifact": {"artifact_slug": "app.tracker"},
                "app_spec": {"app_slug": "tracker"},
                "policy_bundle": {"contracts": []},
            },
        )
        logs = []

        with tempfile.TemporaryDirectory() as tmpdir:
            deploy_output = {
                "compose_project": "xyn-app-tracker",
                "deployment_dir": str(Path(tmpdir) / "tracker"),
                "compose_path": str(Path(tmpdir) / "tracker" / "compose.yml"),
                "app_container_name": "xyn-app-tracker-api",
                "app_url": "http://localhost:12345",
                "ports": {"app_tcp": 12345},
            }
            with mock.patch("core.app_jobs._deployments_root", return_value=Path(tmpdir)):
                with mock.patch("core.app_jobs._deploy_generated_runtime", return_value=deploy_output):
                    with mock.patch("core.app_jobs.update_execution_note", return_value=None):
                        output_json, follow_up = _handle_deploy_app_local(db, job, logs)

        self.assertEqual(output_json["app_slug"], "tracker")
        self.assertEqual(output_json["app_url"], "http://localhost:12345")
        self.assertEqual(len(follow_up), 1)
        self.assertEqual(follow_up[0]["type"], "provision_sibling_xyn")
        self.assertEqual(follow_up[0]["input_json"]["deployment"], output_json)

    def test_provision_sibling_output_and_follow_up_shape(self):
        db = self._fake_db("development")
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
        logs = []

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
            with mock.patch("core.app_jobs._find_revision_sibling_target", return_value=reused_sibling):
                with mock.patch("core.app_jobs._docker_container_running", return_value=True):
                    with mock.patch("core.app_jobs._import_generated_artifact_package_into_registry", return_value={"ok": True}):
                        with mock.patch("core.app_jobs._install_generated_artifact_in_sibling", return_value=installed):
                            with mock.patch("core.app_jobs._docker_network_exists", return_value=True):
                                with mock.patch("core.app_jobs._deployments_root", return_value=Path(tmpdir)):
                                    with mock.patch("core.app_jobs._deploy_generated_runtime", return_value=sibling_runtime):
                                        with mock.patch("core.app_jobs._register_sibling_runtime_target", return_value={"registered": True}):
                                            with mock.patch("core.app_jobs.update_execution_note", return_value=None):
                                                output_json, follow_up = _handle_provision_sibling_xyn(db, job, logs)

        self.assertEqual(output_json["ui_url"], "http://sib.localhost")
        self.assertEqual(output_json["installed_artifact"]["artifact_slug"], "app.tracker")
        self.assertIn("runtime_target", output_json)
        self.assertEqual(len(follow_up), 1)
        self.assertEqual(follow_up[0]["type"], "smoke_test")
        self.assertEqual(follow_up[0]["input_json"]["sibling"], output_json)

    def test_smoke_test_output_shape(self):
        db = self._fake_db("development")
        job = SimpleNamespace(
            id=uuid.uuid4(),
            type="smoke_test",
            workspace_id=uuid.uuid4(),
            input_json={
                "deployment": {
                    "app_container_name": "root-app-api",
                    "compose_project": "",
                    "compose_path": "",
                },
                "sibling": {
                    "compose_project": "sibproj",
                    "installed_artifact": {
                        "workspace_id": "ws-1",
                        "workspace_slug": "development",
                    },
                    "runtime_target": {
                        "app_container_name": "sib-runtime-api",
                        "runtime_base_url": "http://runtime.local",
                        "public_app_url": "http://runtime.local",
                    },
                },
                "app_spec": {"app_slug": "tracker"},
                "policy_bundle": {"contracts": []},
                "generated_artifact": {
                    "artifact_slug": "app.tracker",
                    "artifact_version": "0.0.1-dev",
                },
                "execution_note_artifact_id": str(uuid.uuid4()),
            },
        )
        logs = []

        manifest = {
            "entities": [{"key": "ticket", "fields": [{"name": "workspace_id"}]}],
            "commands": [{"operation_kind": "list", "prompt": "list tickets"}],
        }

        def _container_http_json_stub(_container, _method, path, port=0):
            if path == "/health":
                return 200, {"status": "ok", "port": port}, "ok"
            if path in {"/api/v1/health", "/xyn/api/health", "/xyn/api/v1/health", "/", "/xyn/api/auth/mode", "/xyn/api/me"}:
                return 200, {"status": "ok", "path": path}, "ok"
            return 200, {"status": "ok"}, "ok"

        with mock.patch("core.app_jobs._wait_for_container_http_ok", return_value=True):
            with mock.patch("core.app_jobs._container_http_json", side_effect=_container_http_json_stub):
                with mock.patch("core.app_jobs.build_resolved_capability_manifest", return_value=manifest):
                    with mock.patch("core.app_jobs._exercise_runtime_contracts", return_value=[{"entity": "ticket", "status": "ok"}]):
                        with mock.patch(
                            "core.app_jobs._container_http_session_json",
                            side_effect=[
                                (200, {"artifacts": [{"slug": "app.tracker", "package_version": "0.0.1-dev"}]}, "ok"),
                                (200, {"artifacts": [{"slug": "app.tracker", "package_version": "0.0.1-dev"}]}, "ok"),
                            ],
                        ):
                            with mock.patch("core.app_jobs._docker_container_running", return_value=True):
                                with mock.patch(
                                    "core.app_jobs._execute_sibling_palette_prompt",
                                    return_value=(200, {"kind": "table", "rows": [{"id": 1}], "meta": {"base_url": "http://runtime.local"}}, "ok"),
                                ):
                                    with mock.patch("core.app_jobs.update_execution_note", return_value=None):
                                        output_json, follow_up = _handle_smoke_test(db, job, logs)

        self.assertEqual(output_json.get("status"), "passed")
        self.assertIn("platform_plumbing", output_json)
        self.assertIn("generated_app_contract_smoke", output_json)
        self.assertEqual(follow_up, [])


if __name__ == "__main__":
    unittest.main()
