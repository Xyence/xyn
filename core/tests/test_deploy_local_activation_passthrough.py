import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from core.job_pipeline.stage_contracts import build_follow_up, build_stage_output, parse_stage_input
from core.runtime.deploy_local import handle_deploy_app_local


class DeployLocalActivationPassthroughTests(unittest.TestCase):
    def test_environment_and_activation_ids_are_forwarded_to_provision_stage(self):
        workspace_id = uuid.uuid4()
        job_id = uuid.uuid4()
        with TemporaryDirectory(prefix="xyn-deploy-local-test-") as tmp:
            deploy_root = Path(tmp)
            job = SimpleNamespace(
                id=job_id,
                workspace_id=workspace_id,
                input_json={
                    "app_spec": {"app_slug": "net-inventory"},
                    "policy_bundle": {"policy_families": []},
                    "generated_artifact": {"artifact_slug": "app.net-inventory"},
                    "environment_id": "env-123",
                    "activation_id": "act-123",
                    "sibling_id": "sib-123",
                },
            )

            output_json, follow_up = handle_deploy_app_local(
                db=None,
                job=job,
                logs=[],
                parse_stage_input_fn=parse_stage_input,
                safe_slug_fn=lambda value, default="": str(value or default),
                deployments_root_fn=lambda: deploy_root,
                utc_now_fn=lambda: datetime.now(timezone.utc),
                deploy_generated_runtime_fn=lambda **kwargs: {
                    "compose_project": "xyn-app-net-inventory",
                    "deployment_dir": str(deploy_root),
                    "compose_path": str(deploy_root / "docker-compose.yml"),
                    "app_container_name": "xyn-app-net-inventory-api",
                    "app_url": "http://localhost:18080",
                    "ports": {"app_tcp": 18080},
                },
                record_stage_metadata_fn=lambda *args, **kwargs: None,
                update_execution_note_fn=lambda *args, **kwargs: None,
                append_job_log_fn=lambda logs, line: None,
                build_stage_output_fn=build_stage_output,
                build_follow_up_fn=build_follow_up,
            )

        self.assertEqual(output_json.get("environment_id"), "env-123")
        self.assertEqual(output_json.get("activation_id"), "act-123")
        self.assertEqual(output_json.get("sibling_id"), "sib-123")
        self.assertEqual(len(follow_up), 1)
        self.assertEqual(follow_up[0]["type"], "provision_sibling_xyn")
        next_payload = follow_up[0]["input_json"]
        self.assertEqual(next_payload.get("environment_id"), "env-123")
        self.assertEqual(next_payload.get("activation_id"), "act-123")
        self.assertEqual(next_payload.get("sibling_id"), "sib-123")


if __name__ == "__main__":
    unittest.main()
