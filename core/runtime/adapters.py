from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

HTTP_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_JOB_HTTP_TIMEOUT", "10"))
COMMAND_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_JOB_COMMAND_TIMEOUT_SECONDS", "240"))


def run_command(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    timeout_seconds: int = COMMAND_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "").strip() if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "").strip() if isinstance(exc.stderr, str) else ""
        return 124, stdout, stderr or f"command timed out after {timeout_seconds}s"
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def container_http_json(
    container_name: str,
    method: str,
    path: str,
    *,
    port: int,
    payload: Optional[dict[str, Any]] = None,
    http_timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any], str]:
    script = f"""
import json
import urllib.error
import urllib.request

method = {method!r}
path = {path!r}
payload = {payload or {}!r}
url = "http://localhost:{port}" + path
data = None
headers = {{"Content-Type": "application/json"}}
if method in ("POST", "PUT", "PATCH"):
    data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(url, method=method, headers=headers, data=data)
try:
    with urllib.request.urlopen(req, timeout={http_timeout_seconds}) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
        print(json.dumps({{"code": int(resp.status), "body": body}}))
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="ignore")
    print(json.dumps({{"code": int(exc.code), "body": body}}))
except Exception as exc:
    print(json.dumps({{"code": 0, "body": str(exc)}}))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", container_name, "python", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=http_timeout_seconds + 5,
    )
    if proc.returncode != 0:
        return 0, {}, proc.stderr.strip() or proc.stdout.strip()
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return 0, {}, "empty container response"
    line = out[-1].strip()
    try:
        payload_json = json.loads(line)
    except json.JSONDecodeError:
        return 0, {}, line
    code = int(payload_json.get("code") or 0)
    raw_body = str(payload_json.get("body") or "")
    try:
        body_json = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body_json = {}
    return code, body_json, raw_body


def container_http_session_json(
    container_name: str,
    *,
    steps: list[dict[str, Any]],
    port: int,
    http_timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any], str]:
    script = f"""
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request

steps = {steps!r}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    NoRedirect(),
)
last = {{"code": 0, "body": "", "json": {{}}}}

for step in steps:
    method = str(step.get("method") or "GET").upper()
    path = str(step.get("path") or "/")
    body = step.get("body")
    form = step.get("form")
    headers = dict(step.get("headers") or {{}})
    data = None
    if form is not None:
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        data = urllib.parse.urlencode(form).encode("utf-8")
    elif body is not None:
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://localhost:{port}" + path, method=method, headers=headers, data=data)
    try:
        with opener.open(req, timeout={http_timeout_seconds}) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            try:
                payload = json.loads(raw) if raw else {{}}
            except json.JSONDecodeError:
                payload = {{}}
            last = {{"code": int(resp.status), "body": raw, "json": payload}}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw) if raw else {{}}
        except json.JSONDecodeError:
                payload = {{}}
        last = {{"code": int(exc.code), "body": raw, "json": payload}}
        if int(exc.code) not in {{301, 302, 303, 307, 308}}:
            break
    except Exception as exc:
        last = {{"code": 0, "body": str(exc), "json": {{}}}}
        break

print(json.dumps(last))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", container_name, "python", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=http_timeout_seconds + 10,
    )
    if proc.returncode != 0:
        return 0, {}, proc.stderr.strip() or proc.stdout.strip()
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return 0, {}, "empty container response"
    line = out[-1].strip()
    try:
        payload_json = json.loads(line)
    except json.JSONDecodeError:
        return 0, {}, line
    code = int(payload_json.get("code") or 0)
    raw_body = str(payload_json.get("body") or "")
    body_json = payload_json.get("json") if isinstance(payload_json.get("json"), dict) else {}
    return code, body_json, raw_body


def container_http_session_upload_json(
    container_name: str,
    *,
    port: int,
    upload_path: str,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    extra_form: Optional[dict[str, Any]] = None,
    http_timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any], str]:
    blob_b64 = base64.b64encode(file_bytes).decode("ascii")
    script = f"""
import base64
import json
import requests

session = requests.Session()
login = session.post(
    "http://localhost:{port}/auth/dev-login",
    data={{"appId": "xyn-ui", "returnTo": "/app"}},
    allow_redirects=False,
    timeout={http_timeout_seconds},
)
if login.status_code not in (200, 302, 303):
    print(json.dumps({{"code": int(login.status_code), "body": login.text}}))
    raise SystemExit(0)
session.get("http://localhost:{port}/xyn/api/me", timeout={http_timeout_seconds})
blob = base64.b64decode({blob_b64!r})
resp = session.post(
    "http://localhost:{port}" + {upload_path!r},
    data={extra_form or {}!r},
    files={{{file_field!r}: ({filename!r}, blob, "application/zip")}},
    timeout={http_timeout_seconds},
)
print(json.dumps({{"code": int(resp.status_code), "body": resp.text}}))
"""
    proc = subprocess.run(
        ["docker", "exec", "-i", container_name, "python", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=http_timeout_seconds + 15,
    )
    if proc.returncode != 0:
        return 0, {}, proc.stderr.strip() or proc.stdout.strip()
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        return 0, {}, "empty container response"
    line = out[-1].strip()
    try:
        payload_json = json.loads(line)
    except json.JSONDecodeError:
        return 0, {}, line
    code = int(payload_json.get("code") or 0)
    raw_body = str(payload_json.get("body") or "")
    try:
        body_json = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        body_json = {}
    return code, body_json, raw_body


def resolve_published_port(container_name: str, target: str) -> int:
    code, stdout, stderr = run_command(["docker", "port", container_name, target])
    if code != 0:
        raise RuntimeError(f"Failed to resolve published port for {container_name} {target}: {stderr or stdout}")
    first = (stdout.splitlines() or [""])[0].strip()
    if ":" not in first:
        raise RuntimeError(f"Unexpected docker port output: {first}")
    return int(first.rsplit(":", 1)[1])


def docker_container_running(container_name: str) -> bool:
    code, stdout, _ = run_command(["docker", "inspect", "-f", "{{.State.Running}}", container_name])
    return code == 0 and stdout.strip().lower() == "true"


def docker_network_exists(network_name: str) -> bool:
    code, stdout, _ = run_command(["docker", "network", "inspect", network_name])
    return code == 0 and bool(stdout.strip())


def wait_for_container_http_ok(
    container_name: str,
    path: str,
    *,
    port: int,
    timeout_seconds: int = 60,
    http_timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        code, _, _ = container_http_json(
            container_name,
            "GET",
            path,
            port=port,
            http_timeout_seconds=http_timeout_seconds,
        )
        if code == 200:
            return True
        time.sleep(2)
    return False
