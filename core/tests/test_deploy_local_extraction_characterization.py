from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.app_jobs import _handle_deploy_app_local


class DeployLocalExtractionCharacterizationTests(unittest.TestCase):
    def test_deploy_local_shim_preserves_patch_path_for_deploy_runtime(self):
        db = mock.Mock()
        job = SimpleNamespace(
            id=uuid.uuid4(),
            type="deploy_app_local",
            workspace_id=uuid.uuid4(),
            input_json={
                "execution_note_artifact_id": str(uuid.uuid4()),
                "generated_artifact": {"artifact_slug": "app.tracker"},
                "app_spec": {"app_slug": "tracker"},
                "policy_bundle": {"contracts": []},
            },
        )
        logs: list[str] = []

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
                with mock.patch("core.app_jobs._deploy_generated_runtime", return_value=deploy_output) as patched_deploy:
                    with mock.patch("core.app_jobs.update_execution_note", return_value=None):
                        output_json, follow_up = _handle_deploy_app_local(db, job, logs)

        patched_deploy.assert_called_once()
        self.assertEqual(output_json["app_url"], "http://localhost:12345")
        self.assertEqual(len(follow_up), 1)
        self.assertEqual(follow_up[0]["type"], "provision_sibling_xyn")
