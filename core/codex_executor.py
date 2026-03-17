"""Local Codex execution and validation adapters for Epic C."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol


class CodexExecutionError(RuntimeError):
    """Codex execution failed."""


class CodexTimeoutError(CodexExecutionError):
    """Codex execution exceeded the configured timeout."""


@dataclass
class CodexExecutionResult:
    summary_text: str
    log_text: str
    exit_code: int


@dataclass
class ValidationResult:
    success: bool
    summary: str
    log_text: str
    report_payload: Optional[dict[str, Any]] = None
    skipped: bool = False


@dataclass
class CodexAvailability:
    available: bool
    binary: str
    reason: Optional[str] = None


class CodexExecutor(Protocol):
    def execute(
        self,
        *,
        repo_path: Path,
        prompt_title: str,
        prompt_body: str,
        timeout_seconds: Optional[int],
    ) -> CodexExecutionResult:
        ...


class ValidationRunner(Protocol):
    def run(
        self,
        *,
        repo_path: Path,
        timeout_seconds: Optional[int],
    ) -> ValidationResult:
        ...


class CliCodexExecutor:
    """Shell adapter around the installed Codex CLI."""

    def __init__(self, binary: Optional[str] = None):
        self.binary = binary or os.getenv("XYN_CODEX_BINARY") or shutil.which("codex") or "codex"

    def availability(self) -> CodexAvailability:
        return check_codex_availability(self.binary)

    def execute(
        self,
        *,
        repo_path: Path,
        prompt_title: str,
        prompt_body: str,
        timeout_seconds: Optional[int],
    ) -> CodexExecutionResult:
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as summary_file:
            summary_path = Path(summary_file.name)
        prompt = f"{prompt_title.strip()}\n\n{prompt_body.strip()}".strip()
        auth_error = _ensure_codex_login(self.binary)
        if auth_error:
            raise CodexExecutionError(auth_error)
        cmd = self._build_exec_command(repo_path=repo_path, summary_path=summary_path)
        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            raise CodexTimeoutError(f"Codex execution timed out after {timeout_seconds}s.\n{stdout}\n{stderr}".strip())
        finally:
            summary_text = summary_path.read_text(encoding="utf-8").strip() if summary_path.exists() else ""
            try:
                summary_path.unlink(missing_ok=True)
            except TypeError:
                if summary_path.exists():
                    summary_path.unlink()
        log_text = "\n".join(part for part in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if part).strip()
        if proc.returncode != 0:
            raise CodexExecutionError(summary_text or log_text or f"Codex exited with status {proc.returncode}")
        return CodexExecutionResult(
            summary_text=summary_text or "Codex execution completed.",
            log_text=log_text,
            exit_code=proc.returncode,
        )

    def _build_exec_command(self, *, repo_path: Path, summary_path: Path) -> list[str]:
        command = [self.binary]
        if _codex_bypass_sandbox_enabled():
            # xyn-core already runs inside a dedicated container with controlled repo mounts.
            # The Codex workspace sandbox still presents those mounts as read-only here, so
            # local runtime execution needs the CLI bypass mode to make real repository edits.
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["-a", "never", "-s", _codex_sandbox_mode()])
        command.extend(
            [
                "exec",
                "-C",
                str(repo_path),
                "--json",
                "--output-last-message",
                str(summary_path),
            ]
        )
        return command


def check_codex_availability(binary: Optional[str] = None) -> CodexAvailability:
    resolved = binary or os.getenv("XYN_CODEX_BINARY") or shutil.which("codex") or "codex"
    binary_path = shutil.which(resolved) if os.path.sep not in resolved else resolved
    if not binary_path:
        return CodexAvailability(available=False, binary=resolved, reason="codex executable not found in PATH")
    try:
        proc = subprocess.run(
            [binary_path, "--help"],
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return CodexAvailability(available=False, binary=binary_path, reason="codex --help timed out")
    except OSError as exc:
        return CodexAvailability(available=False, binary=binary_path, reason=str(exc))
    if proc.returncode != 0:
        output = "\n".join(part for part in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if part).strip()
        return CodexAvailability(
            available=False,
            binary=binary_path,
            reason=output or f"codex --help exited with status {proc.returncode}",
        )
    auth_error = _ensure_codex_login(binary_path)
    if auth_error:
        return CodexAvailability(available=False, binary=binary_path, reason=auth_error)
    return CodexAvailability(available=True, binary=binary_path)


def _codex_api_key() -> str:
    return str(os.getenv("OPENAI_API_KEY") or os.getenv("XYN_OPENAI_API_KEY") or "").strip()


def _codex_sandbox_mode() -> str:
    return str(os.getenv("XYN_CODEX_SANDBOX_MODE") or "workspace-write").strip() or "workspace-write"


def _codex_bypass_sandbox_enabled() -> bool:
    value = str(os.getenv("XYN_CODEX_BYPASS_SANDBOX") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _ensure_codex_login(binary: str) -> Optional[str]:
    status = subprocess.run(
        [binary, "login", "status"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if status.returncode == 0:
        return None
    api_key = _codex_api_key()
    if not api_key:
        return "codex is not logged in and no OpenAI API key is configured for bootstrap auth"
    login = subprocess.run(
        [binary, "login", "--with-api-key"],
        input=f"{api_key}\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if login.returncode != 0:
        output = "\n".join(part for part in [(login.stdout or "").strip(), (login.stderr or "").strip()] if part).strip()
        return output or "codex login bootstrap failed"
    verify = subprocess.run(
        [binary, "login", "status"],
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    if verify.returncode == 0:
        return None
    output = "\n".join(part for part in [(verify.stdout or "").strip(), (verify.stderr or "").strip()] if part).strip()
    return output or "codex login bootstrap did not establish an authenticated session"


class LocalValidationRunner:
    """Best-effort deterministic validation runner for local repos."""

    def run(
        self,
        *,
        repo_path: Path,
        timeout_seconds: Optional[int],
    ) -> ValidationResult:
        cmd = self._resolve_command(repo_path)
        if not cmd:
            return ValidationResult(
                success=True,
                summary="Validation skipped: no deterministic test command available.",
                log_text="",
                skipped=True,
            )
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(repo_path),
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(
                success=False,
                summary=f"Validation timed out after {timeout_seconds}s.",
                log_text="",
                report_payload={"command": cmd, "timed_out": True},
            )
        log_text = "\n".join(part for part in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if part).strip()
        return ValidationResult(
            success=proc.returncode == 0,
            summary="Validation succeeded." if proc.returncode == 0 else f"Validation failed with exit code {proc.returncode}.",
            log_text=log_text,
            report_payload={"command": cmd, "exit_code": proc.returncode},
        )

    def _resolve_command(self, repo_path: Path) -> list[str] | None:
        if (repo_path / "pytest.ini").exists() or (repo_path / "pyproject.toml").exists():
            return ["python", "-m", "pytest", "-q"]
        if (repo_path / "manage.py").exists():
            return ["python", "manage.py", "test"]
        if (repo_path / "core" / "tests").exists() or (repo_path / "tests").exists():
            return ["python", "-m", "unittest"]
        package_json = repo_path / "package.json"
        if package_json.exists():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
            scripts = payload.get("scripts") if isinstance(payload, dict) else {}
            if isinstance(scripts, dict) and scripts.get("test"):
                return ["npm", "test", "--", "--runInBand"]
        return None
