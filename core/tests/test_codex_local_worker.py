import json
import os
import shutil
import subprocess
import tempfile
import uuid
import unittest
from pathlib import Path
from unittest import mock

from core import models
from core.codex_executor import (
    CliCodexExecutor,
    CodexExecutionError,
    CodexExecutionResult,
    CodexTimeoutError,
    ValidationResult,
    check_codex_availability,
)
from core.database import SessionLocal
from core.runtime_contract import RunPayloadV1
from core.runtime_execution import create_runtime_run, dispatch_queued_run, execute_assigned_run
from core.runtime_workers import register_codex_local_worker


class _FakeCodexExecutor:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.invocations = 0

    def execute(self, *, repo_path: Path, prompt_title: str, prompt_body: str, timeout_seconds):
        self.invocations += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        file_path = repo_path / "worker_output.txt"
        file_path.write_text(outcome.get("file_content", "updated\n"), encoding="utf-8")
        return CodexExecutionResult(
            summary_text=outcome.get("summary", "done"),
            log_text=outcome.get("log", "codex log"),
            exit_code=0,
        )


class _FakeValidationRunner:
    def __init__(self, results):
        self._results = list(results)
        self.invocations = 0

    def run(self, *, repo_path: Path, timeout_seconds):
        self.invocations += 1
        if self._results:
            return self._results.pop(0)
        return ValidationResult(success=True, summary="Validation skipped.", log_text="", skipped=True)


class CodexLocalWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "run_payload_v1.json"
        cls.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.db = SessionLocal()
        self.tempdirs = []
        self.run_ids = []
        self.worker_ids = []
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
        os.environ.pop("XYN_RUNTIME_REPO_MAP", None)
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
        runtime_root = Path(".xyn/workspace/runtime_runs").resolve()
        for run_id in self.run_ids:
            shutil.rmtree(runtime_root / str(run_id), ignore_errors=True)
        for tmpdir in self.tempdirs:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _temp_repo(self) -> Path:
        tmpdir = tempfile.mkdtemp(prefix="codex-worker-")
        self.tempdirs.append(tmpdir)
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init", "-b", "main"], cwd=tmpdir, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmpdir, check=True)
        (repo_path / "README.md").write_text("initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmpdir, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmpdir, check=True, capture_output=True, text=True)
        return repo_path

    def _payload(self, repo_path: str, *, branch: str | None = "main", requested_outputs=None, prompt_body: str | None = None) -> RunPayloadV1:
        payload = dict(self.fixture)
        payload["run_id"] = str(uuid.uuid4())
        payload["work_item_id"] = f"wi-{uuid.uuid4()}"
        payload["target"] = dict(payload["target"])
        payload["target"]["repo"] = repo_path
        payload["target"]["branch"] = branch
        payload["prompt"] = dict(payload["prompt"])
        if prompt_body is not None:
            payload["prompt"]["body"] = prompt_body
        if requested_outputs is not None:
            payload["requested_outputs"] = list(requested_outputs)
        return RunPayloadV1.model_validate(payload)

    def _register_and_dispatch(self, payload: RunPayloadV1):
        run = create_runtime_run(self.db, payload)
        run.priority = -1000
        self.run_ids.append(run.id)
        with mock.patch("core.codex_local_worker.check_codex_availability") as availability:
            availability.return_value.available = True
            availability.return_value.reason = None
            worker = register_codex_local_worker(self.db, worker_id=f"codex-test-{uuid.uuid4()}")
        self.worker_ids.append(worker.worker_id)
        self.db.commit()
        dispatched = dispatch_queued_run(self.db)
        self.db.commit()
        self.assertEqual(dispatched.id, run.id)
        self.db.refresh(run)
        return run, worker

    def test_successful_codex_local_run_emits_steps_and_artifacts(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"test-repo":["{repo}"]}}'
        payload = self._payload("test-repo", requested_outputs=["patch", "log", "summary", "report"], prompt_body="Implement change and run tests.")
        run, _worker = self._register_and_dispatch(payload)
        executor = _FakeCodexExecutor([{"summary": "Implemented worker change.", "log": "codex ok", "file_content": "changed\n"}])
        validation = _FakeValidationRunner([
            ValidationResult(success=True, summary="Tests passed.", log_text="ok", report_payload={"tests": 1, "failures": 0}),
        ])

        execute_assigned_run(self.db, run.id, executor=executor, validation_runner=validation)
        self.db.commit()
        self.db.refresh(run)

        self.assertEqual(run.status, models.RunStatus.COMPLETED)
        steps = self.db.query(models.Step).filter(models.Step.run_id == run.id).order_by(models.Step.idx.asc()).all()
        self.assertEqual([step.step_key for step in steps], [
            "inspect_repository",
            "prepare_context",
            "execute_codex",
            "validate_results",
            "finalize_outputs",
        ])
        self.assertTrue(all(step.status == models.StepStatus.COMPLETED for step in steps))
        artifacts = self.db.query(models.Artifact).filter(models.Artifact.run_id == run.id).order_by(models.Artifact.created_at.asc()).all()
        artifact_uris = {artifact.kind: artifact.storage_path for artifact in artifacts}
        self.assertEqual(artifact_uris["patch"], f"artifact://runs/{run.id}/patch.diff")
        self.assertEqual(artifact_uris["log"], f"artifact://runs/{run.id}/build_logs.txt")
        self.assertEqual(artifact_uris["summary"], f"artifact://runs/{run.id}/final_summary.md")
        self.assertEqual(artifact_uris["report"], f"artifact://runs/{run.id}/test_report.json")
        events = self.db.query(models.Event).filter(models.Event.run_id == run.id).all()
        event_names = [event.event_name for event in events]
        self.assertIn("run.started", event_names)
        self.assertIn("run.step.started", event_names)
        self.assertIn("run.step.completed", event_names)
        self.assertIn("run.artifact.created", event_names)
        self.assertIn("run.completed", event_names)

    def test_blocked_run_on_missing_target_repo(self):
        payload = self._payload("/tmp/definitely-missing-codex-worker-repo", requested_outputs=["patch", "summary"])
        payload.policy.max_retries = 0
        run, _worker = self._register_and_dispatch(payload)

        execute_assigned_run(self.db, run.id, executor=_FakeCodexExecutor([]), validation_runner=_FakeValidationRunner([]))
        self.db.commit()
        self.db.refresh(run)

        self.assertEqual(run.status, models.RunStatus.FAILED)
        self.assertEqual(run.failure_reason, "repo_unreachable")
        self.assertTrue(run.summary)
        event_names = [event.event_name for event in self.db.query(models.Event).filter(models.Event.run_id == run.id).all()]
        self.assertIn("run.failed", event_names)

    def test_failed_run_retries_once_then_succeeds(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"test-repo":["{repo}"]}}'
        payload = self._payload("test-repo", requested_outputs=["patch", "log", "summary"], prompt_body="Make a change.")
        run, _worker = self._register_and_dispatch(payload)
        executor = _FakeCodexExecutor([
            CodexExecutionError("first attempt failed"),
            {"summary": "second attempt succeeded", "log": "retry ok", "file_content": "after retry\n"},
        ])
        validation = _FakeValidationRunner([ValidationResult(success=True, summary="Validation skipped.", log_text="", skipped=True)])

        execute_assigned_run(self.db, run.id, executor=executor, validation_runner=validation)
        self.db.commit()
        self.db.refresh(run)
        self.assertEqual(run.status, models.RunStatus.QUEUED)

        dispatched = dispatch_queued_run(self.db)
        self.db.commit()
        self.assertEqual(dispatched.id, run.id)
        execute_assigned_run(self.db, run.id, executor=executor, validation_runner=validation)
        self.db.commit()
        self.db.refresh(run)

        self.assertEqual(run.status, models.RunStatus.COMPLETED)
        self.assertEqual(executor.invocations, 2)

    def test_timeout_handling_marks_run_failed(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"test-repo":["{repo}"]}}'
        payload = self._payload("test-repo", requested_outputs=["log", "summary"], prompt_body="Make a change quickly.")
        payload.policy.max_retries = 0
        run, _worker = self._register_and_dispatch(payload)
        executor = _FakeCodexExecutor([CodexTimeoutError("timed out")])

        execute_assigned_run(self.db, run.id, executor=executor, validation_runner=_FakeValidationRunner([]))
        self.db.commit()
        self.db.refresh(run)

        self.assertEqual(run.status, models.RunStatus.FAILED)
        self.assertEqual(run.failure_reason, "timeout_exceeded")
        event_names = [event.event_name for event in self.db.query(models.Event).filter(models.Event.run_id == run.id).all()]
        self.assertIn("run.failed", event_names)
        summary_artifact = self.db.query(models.Artifact).filter(
            models.Artifact.run_id == run.id,
            models.Artifact.kind == "summary",
        ).first()
        self.assertIsNotNone(summary_artifact)

    def test_artifact_integrity_uses_deterministic_paths(self):
        repo = self._temp_repo()
        os.environ["XYN_RUNTIME_REPO_MAP"] = f'{{"test-repo":["{repo}"]}}'
        payload = self._payload("test-repo", requested_outputs=["patch", "log", "summary", "report"], prompt_body="Implement and validate.")
        run, _worker = self._register_and_dispatch(payload)
        executor = _FakeCodexExecutor([{"summary": "ok", "log": "codex ok", "file_content": "artifact check\n"}])
        validation = _FakeValidationRunner([
            ValidationResult(success=True, summary="ok", log_text="ok", report_payload={"tests": 1}),
        ])

        execute_assigned_run(self.db, run.id, executor=executor, validation_runner=validation)
        self.db.commit()

        runtime_root = Path(".xyn/workspace/runtime_runs").resolve() / str(run.id) / "artifacts"
        self.assertTrue((runtime_root / "patch.diff").exists())
        self.assertTrue((runtime_root / "build_logs.txt").exists())
        self.assertTrue((runtime_root / "final_summary.md").exists())
        self.assertTrue((runtime_root / "test_report.json").exists())

    def test_executor_availability_check(self):
        availability = check_codex_availability("/definitely/missing/codex")
        self.assertFalse(availability.available)

    def test_cli_executor_uses_bypass_mode_when_enabled(self):
        executor = CliCodexExecutor("/usr/bin/codex")
        with mock.patch.dict(os.environ, {"XYN_CODEX_BYPASS_SANDBOX": "true"}, clear=False):
            command = executor._build_exec_command(
                repo_path=Path("/workspace/xyn-platform"),
                summary_path=Path("/tmp/summary.txt"),
            )
        self.assertEqual(
            command,
            [
                "/usr/bin/codex",
                "--dangerously-bypass-approvals-and-sandbox",
                "exec",
                "-C",
                "/workspace/xyn-platform",
                "--json",
                "--output-last-message",
                "/tmp/summary.txt",
            ],
        )

    def test_cli_executor_uses_configured_sandbox_when_bypass_disabled(self):
        executor = CliCodexExecutor("/usr/bin/codex")
        with mock.patch.dict(
            os.environ,
            {"XYN_CODEX_BYPASS_SANDBOX": "", "XYN_CODEX_SANDBOX_MODE": "danger-full-access"},
            clear=False,
        ):
            command = executor._build_exec_command(
                repo_path=Path("/workspace/xyn-platform"),
                summary_path=Path("/tmp/summary.txt"),
            )
        self.assertEqual(
            command,
            [
                "/usr/bin/codex",
                "-a",
                "never",
                "-s",
                "danger-full-access",
                "exec",
                "-C",
                "/workspace/xyn-platform",
                "--json",
                "--output-last-message",
                "/tmp/summary.txt",
            ],
        )

    def test_executor_availability_bootstraps_login_from_api_key(self):
        responses = [
            mock.Mock(returncode=0, stdout="Codex help", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="Not logged in"),
            mock.Mock(returncode=0, stdout="Logged in", stderr=""),
            mock.Mock(returncode=0, stdout="Logged in", stderr=""),
        ]
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-openai"}, clear=False), mock.patch(
            "shutil.which", return_value="/usr/bin/codex"
        ), mock.patch("subprocess.run", side_effect=responses) as run:
            availability = check_codex_availability("codex")
        self.assertTrue(availability.available)
        login_call = run.call_args_list[2]
        self.assertEqual(login_call.args[0], ["/usr/bin/codex", "login", "--with-api-key"])
        self.assertIn("sk-test-openai", login_call.kwargs["input"])

    def test_executor_availability_requires_login_when_no_api_key_exists(self):
        responses = [
            mock.Mock(returncode=0, stdout="Codex help", stderr=""),
            mock.Mock(returncode=1, stdout="", stderr="Not logged in"),
        ]
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "XYN_OPENAI_API_KEY": ""}, clear=False), mock.patch(
            "shutil.which", return_value="/usr/bin/codex"
        ), mock.patch("subprocess.run", side_effect=responses):
            availability = check_codex_availability("codex")
        self.assertFalse(availability.available)
        self.assertIn("not logged in", availability.reason)

    def test_worker_registration_is_offline_when_codex_missing(self):
        with mock.patch("core.codex_local_worker.check_codex_availability") as availability:
            availability.return_value.available = False
            availability.return_value.reason = "missing"
            worker = register_codex_local_worker(self.db, worker_id=f"codex-test-{uuid.uuid4()}")
        self.worker_ids.append(worker.worker_id)
        self.assertEqual(worker.status, models.RuntimeWorkerStatus.OFFLINE)

    def test_worker_registration_is_idle_when_codex_present(self):
        with mock.patch("core.codex_local_worker.check_codex_availability") as availability:
            availability.return_value.available = True
            availability.return_value.reason = None
            worker = register_codex_local_worker(self.db, worker_id=f"codex-test-{uuid.uuid4()}")
        self.worker_ids.append(worker.worker_id)
        self.assertEqual(worker.status, models.RuntimeWorkerStatus.IDLE)

    def test_dispatcher_does_not_assign_to_unavailable_codex_worker(self):
        repo = self._temp_repo()
        payload = self._payload(str(repo), requested_outputs=["patch", "summary"])
        run = create_runtime_run(self.db, payload)
        run.priority = -1000
        self.run_ids.append(run.id)
        with mock.patch("core.codex_local_worker.check_codex_availability") as codex_availability:
            codex_availability.return_value.available = False
            codex_availability.return_value.reason = "missing"
            worker = register_codex_local_worker(self.db, worker_id=f"codex-test-{uuid.uuid4()}")
            self.worker_ids.append(worker.worker_id)
            self.db.commit()
            dispatched = dispatch_queued_run(self.db)
        self.assertIsNone(dispatched)
        self.db.refresh(run)
        self.assertEqual(run.status, models.RunStatus.QUEUED)


if __name__ == "__main__":
    unittest.main()
