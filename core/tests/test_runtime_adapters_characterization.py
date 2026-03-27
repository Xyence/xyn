from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from core.runtime import adapters


class RuntimeAdaptersCharacterizationTests(unittest.TestCase):
    def test_run_command_returns_stdout_stderr_and_code(self):
        proc = subprocess.CompletedProcess(args=["echo"], returncode=3, stdout=" out \n", stderr=" err \n")
        with mock.patch("core.runtime.adapters.subprocess.run", return_value=proc) as run_mock:
            code, stdout, stderr = adapters.run_command(["echo", "x"], timeout_seconds=17)

        self.assertEqual((code, stdout, stderr), (3, "out", "err"))
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 17)

    def test_run_command_timeout_returns_124(self):
        exc = subprocess.TimeoutExpired(cmd=["sleep", "5"], timeout=5)
        exc.stdout = "slow"
        exc.stderr = "too slow"
        with mock.patch("core.runtime.adapters.subprocess.run", side_effect=exc):
            code, stdout, stderr = adapters.run_command(["sleep", "5"], timeout_seconds=5)

        self.assertEqual(code, 124)
        self.assertEqual(stdout, "slow")
        self.assertEqual(stderr, "too slow")

    def test_container_http_json_success_parses_response(self):
        payload = {"code": 200, "body": json.dumps({"ok": True})}
        proc = subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout=f"noise\n{json.dumps(payload)}\n",
            stderr="",
        )
        with mock.patch("core.runtime.adapters.subprocess.run", return_value=proc) as run_mock:
            code, body, raw = adapters.container_http_json("api", "GET", "/health", port=8080, payload=None, http_timeout_seconds=7)

        self.assertEqual(code, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(raw, json.dumps({"ok": True}))
        self.assertEqual(run_mock.call_args.args[0], ["docker", "exec", "-i", "api", "python", "-"])
        self.assertEqual(run_mock.call_args.kwargs["timeout"], 12)

    def test_container_http_json_malformed_json_falls_back(self):
        proc = subprocess.CompletedProcess(args=["docker"], returncode=0, stdout="not-json\n", stderr="")
        with mock.patch("core.runtime.adapters.subprocess.run", return_value=proc):
            code, body, raw = adapters.container_http_json("api", "GET", "/health", port=8080)

        self.assertEqual((code, body, raw), (0, {}, "not-json"))

    def test_container_http_json_nonzero_return_uses_stderr_or_stdout(self):
        proc = subprocess.CompletedProcess(args=["docker"], returncode=1, stdout="fallback", stderr="bad")
        with mock.patch("core.runtime.adapters.subprocess.run", return_value=proc):
            code, body, raw = adapters.container_http_json("api", "GET", "/health", port=8080)

        self.assertEqual((code, body, raw), (0, {}, "bad"))

    def test_container_http_session_json_success(self):
        payload = {"code": 302, "body": "{}", "json": {"step": "done"}}
        proc = subprocess.CompletedProcess(args=["docker"], returncode=0, stdout=f"{json.dumps(payload)}\n", stderr="")
        with mock.patch("core.runtime.adapters.subprocess.run", return_value=proc):
            code, body, raw = adapters.container_http_session_json("api", steps=[{"method": "GET", "path": "/"}], port=8080)

        self.assertEqual(code, 302)
        self.assertEqual(body, {"step": "done"})
        self.assertEqual(raw, "{}")

    def test_container_http_session_upload_json_parses_body(self):
        payload = {"code": 201, "body": json.dumps({"artifact": "ok"})}
        proc = subprocess.CompletedProcess(args=["docker"], returncode=0, stdout=f"{json.dumps(payload)}\n", stderr="")
        with mock.patch("core.runtime.adapters.subprocess.run", return_value=proc) as run_mock:
            code, body, raw = adapters.container_http_session_upload_json(
                "api",
                port=8080,
                upload_path="/xyn/api/artifacts/import",
                file_field="file",
                filename="bundle.zip",
                file_bytes=b"zip-bytes",
                extra_form={"scope": "solution"},
            )

        self.assertEqual(code, 201)
        self.assertEqual(body, {"artifact": "ok"})
        self.assertEqual(raw, json.dumps({"artifact": "ok"}))
        self.assertEqual(run_mock.call_args.args[0], ["docker", "exec", "-i", "api", "python", "-"])

    def test_resolve_published_port_success_and_error(self):
        with mock.patch("core.runtime.adapters.run_command", return_value=(0, "0.0.0.0:49152\n", "")):
            self.assertEqual(adapters.resolve_published_port("ctr", "8000/tcp"), 49152)
        with mock.patch("core.runtime.adapters.run_command", return_value=(1, "", "boom")):
            with self.assertRaisesRegex(RuntimeError, "Failed to resolve published port"):
                adapters.resolve_published_port("ctr", "8000/tcp")

    def test_docker_container_and_network_detection(self):
        with mock.patch("core.runtime.adapters.run_command", return_value=(0, "true\n", "")):
            self.assertTrue(adapters.docker_container_running("ctr"))
        with mock.patch("core.runtime.adapters.run_command", return_value=(0, '{"Name":"net"}', "")):
            self.assertTrue(adapters.docker_network_exists("net"))
        with mock.patch("core.runtime.adapters.run_command", return_value=(1, "", "")):
            self.assertFalse(adapters.docker_network_exists("net"))

    def test_wait_for_container_http_ok_success_and_timeout(self):
        with mock.patch("core.runtime.adapters.container_http_json", side_effect=[(500, {}, ""), (200, {}, "")]):
            with mock.patch("core.runtime.adapters.time.sleep") as sleep_mock:
                self.assertTrue(
                    adapters.wait_for_container_http_ok("api", "/health", port=8080, timeout_seconds=60, http_timeout_seconds=3)
                )
                sleep_mock.assert_called_once_with(2)

        with mock.patch("core.runtime.adapters.container_http_json", return_value=(500, {}, "")):
            with mock.patch("core.runtime.adapters.time.time", side_effect=[0, 1, 3, 5]):
                with mock.patch("core.runtime.adapters.time.sleep") as sleep_mock:
                    self.assertFalse(
                        adapters.wait_for_container_http_ok(
                            "api",
                            "/health",
                            port=8080,
                            timeout_seconds=2,
                            http_timeout_seconds=3,
                        )
                    )
                    self.assertGreaterEqual(sleep_mock.call_count, 1)
