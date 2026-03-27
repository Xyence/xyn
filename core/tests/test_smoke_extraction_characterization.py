from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.app_jobs import _handle_smoke_test


class SmokeExtractionCharacterizationTests(unittest.TestCase):
    def _fake_db(self, workspace_slug: str = "development"):
        db = mock.Mock()
        workspace = SimpleNamespace(slug=workspace_slug)
        db.query.return_value.filter.return_value.first.return_value = workspace
        return db

    def test_smoke_shim_preserves_output_shape_and_registry_fallback(self):
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
                        "artifact_slug": "app.tracker",
                        "artifact_id": "artifact-1",
                    },
                    "runtime_target": {
                        "app_container_name": "sib-runtime-api",
                        "runtime_base_url": "http://sib-runtime-api:8080",
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
        logs: list[str] = []

        manifest = {
            "entities": [{"key": "ticket", "fields": [{"name": "workspace_id"}]}],
            "commands": [{"operation_kind": "list", "prompt": "list tickets"}],
        }

        def _container_http_json_stub(_container, _method, path, port=0, payload=None):
            if path == "/health":
                return 200, {"status": "ok", "port": port}, "ok"
            if path in {"/api/v1/health", "/xyn/api/health", "/xyn/api/v1/health", "/", "/xyn/api/auth/mode", "/xyn/api/me"}:
                return 200, {"status": "ok", "path": path}, "ok"
            return 200, {"status": "ok"}, "ok"

        with mock.patch("core.app_jobs._wait_for_container_http_ok", return_value=True):
            with mock.patch("core.app_jobs._container_http_json", side_effect=_container_http_json_stub):
                with mock.patch("core.app_jobs.build_resolved_capability_manifest", return_value=manifest):
                    with mock.patch("core.app_jobs._exercise_runtime_contracts", return_value=[{"entity": "ticket", "status": "ok"}]) as exercise_mock:
                        with mock.patch(
                            "core.app_jobs._container_http_session_json",
                            side_effect=[
                                (200, {"artifacts": []}, "ok"),
                                (200, {"artifacts": [{"slug": "app.tracker", "package_version": "0.0.1-dev"}]}, "ok"),
                            ],
                        ):
                            with mock.patch("core.app_jobs._docker_container_running", return_value=True):
                                with mock.patch(
                                    "core.app_jobs._execute_sibling_palette_prompt",
                                    return_value=(
                                        200,
                                        {
                                            "kind": "table",
                                            "rows": [{"id": "1"}],
                                            "meta": {"base_url": "http://sib-runtime-api:8080"},
                                        },
                                        "ok",
                                    ),
                                ):
                                    with mock.patch("core.app_jobs.finalize_stage_note", return_value=None):
                                        output_json, follow_up = _handle_smoke_test(db, job, logs)

        self.assertIn("platform_plumbing", output_json)
        self.assertIn("generated_app_contract_smoke", output_json)
        self.assertIn("palette_after_root_runtime_stop", output_json)
        self.assertIn("root_runtime_stop", output_json)
        self.assertIn("root_runtime_restart", output_json)
        self.assertEqual(output_json.get("status"), "passed")
        self.assertEqual(follow_up, [])
        self.assertEqual(
            ((output_json.get("platform_plumbing") or {}).get("generated_artifact") or {}).get("registry_catalog", {}).get("source"),
            "installed_artifact_fallback",
        )
        self.assertGreaterEqual(exercise_mock.call_count, 2)

    def test_smoke_preserves_root_runtime_stop_restart_probe(self):
        db = self._fake_db("development")
        with tempfile.TemporaryDirectory() as tmpdir:
            compose_path = Path(tmpdir) / "docker-compose.yml"
            compose_path.write_text("services:\n  api:\n    image: example\n", encoding="utf-8")
            job = SimpleNamespace(
                id=uuid.uuid4(),
                type="smoke_test",
                workspace_id=uuid.uuid4(),
                input_json={
                    "deployment": {
                        "app_container_name": "root-app-api",
                        "compose_project": "rootproj",
                        "compose_path": str(compose_path),
                    },
                    "sibling": {
                        "compose_project": "sibproj",
                        "installed_artifact": {
                            "workspace_id": "ws-1",
                            "workspace_slug": "development",
                            "artifact_slug": "app.tracker",
                            "artifact_id": "artifact-1",
                        },
                        "runtime_target": {
                            "app_container_name": "sib-runtime-api",
                            "runtime_base_url": "http://sib-runtime-api:8080",
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
            logs: list[str] = []

            manifest = {
                "entities": [{"key": "ticket", "fields": [{"name": "workspace_id"}]}],
                "commands": [{"operation_kind": "list", "prompt": "list tickets"}],
            }

            with mock.patch("core.app_jobs._wait_for_container_http_ok", return_value=True):
                with mock.patch("core.app_jobs._container_http_json", return_value=(200, {"status": "ok"}, "ok")):
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
                                        side_effect=[
                                            (
                                                200,
                                                {"kind": "table", "rows": [{"id": "1"}], "meta": {"base_url": "http://sib-runtime-api:8080"}},
                                                "ok",
                                            ),
                                            (
                                                200,
                                                {"kind": "table", "rows": [{"id": "1"}], "meta": {"base_url": "http://sib-runtime-api:8080"}},
                                                "ok",
                                            ),
                                        ],
                                    ):
                                        with mock.patch(
                                            "core.app_jobs._run",
                                            side_effect=[
                                                (0, "stopped", ""),
                                                (0, "started", ""),
                                            ],
                                        ) as run_mock:
                                            with mock.patch("core.app_jobs.finalize_stage_note", return_value=None):
                                                output_json, _ = _handle_smoke_test(db, job, logs)

            self.assertEqual((output_json.get("root_runtime_stop") or {}).get("status"), "stopped")
            self.assertEqual((output_json.get("root_runtime_restart") or {}).get("status"), "restarted")
            self.assertTrue((output_json.get("palette_after_root_runtime_stop") or {}).get("rows"))
            self.assertEqual(run_mock.call_count, 2)
            stop_cmd = run_mock.call_args_list[0].args[0]
            up_cmd = run_mock.call_args_list[1].args[0]
            self.assertEqual(stop_cmd, ["docker", "compose", "-p", "rootproj", "-f", str(compose_path), "stop"])
            self.assertEqual(up_cmd, ["docker", "compose", "-p", "rootproj", "-f", str(compose_path), "up", "-d"])
