from __future__ import annotations

from typing import Any, Optional

from core.runtime import adapters as runtime_adapters


def _project_containers(project: str) -> list[str]:
    token = str(project or "").strip()
    if not token:
        return []
    code, stdout, _stderr = runtime_adapters.run_command(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={token}",
            "--format",
            "{{.Names}}",
        ]
    )
    if code != 0:
        return []
    return [line.strip() for line in str(stdout or "").splitlines() if line.strip()]


def restart_project(project: str) -> tuple[bool, str]:
    containers = _project_containers(project)
    if not containers:
        return False, "No compose project containers found"
    code, stdout, stderr = runtime_adapters.run_command(["docker", "restart", *containers])
    if code != 0:
        return False, (stderr or stdout or "restart failed").strip()
    return True, (stdout or "restarted").strip()


def stop_project(project: str, *, remove_volumes: bool = False) -> tuple[bool, str]:
    containers = _project_containers(project)
    if not containers:
        return False, "No compose project containers found"

    code, stdout, stderr = runtime_adapters.run_command(["docker", "stop", *containers])
    if code != 0:
        return False, (stderr or stdout or "stop failed").strip()

    notes: list[str] = ["stopped"]
    if not remove_volumes:
        return True, "; ".join(notes)

    rm_code, rm_out, rm_err = runtime_adapters.run_command(["docker", "rm", "-f", *containers])
    if rm_code != 0:
        return False, (rm_err or rm_out or "container removal failed").strip()
    notes.append("containers removed")

    network_code, network_out, network_err = runtime_adapters.run_command(
        [
            "docker",
            "network",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "-q",
        ]
    )
    if network_code == 0:
        networks = [line.strip() for line in str(network_out or "").splitlines() if line.strip()]
        if networks:
            rm_network_code, _rm_network_out, rm_network_err = runtime_adapters.run_command(
                ["docker", "network", "rm", *networks]
            )
            if rm_network_code != 0:
                return False, (rm_network_err or "network removal failed").strip()
            notes.append("networks removed")
    elif network_err:
        return False, network_err.strip()

    volume_code, volume_out, volume_err = runtime_adapters.run_command(
        [
            "docker",
            "volume",
            "ls",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "-q",
        ]
    )
    if volume_code == 0:
        volumes = [line.strip() for line in str(volume_out or "").splitlines() if line.strip()]
        if volumes:
            rm_volume_code, _rm_volume_out, rm_volume_err = runtime_adapters.run_command(
                ["docker", "volume", "rm", *volumes]
            )
            if rm_volume_code != 0:
                return False, (rm_volume_err or "volume removal failed").strip()
            notes.append("volumes removed")
    elif volume_err:
        return False, volume_err.strip()

    return True, "; ".join(notes)


def build_compact_sibling_payload(row: object) -> dict[str, Any]:
    installed_artifacts_payload: list[dict[str, str]] = []
    installed_rows = getattr(row, "installed_artifacts", None)
    if isinstance(installed_rows, list):
        for item in installed_rows:
            installed_artifacts_payload.append(
                {
                    "artifact_slug": str(getattr(item, "artifact_slug", "") or ""),
                    "artifact_version": str(getattr(item, "artifact_version", "") or ""),
                    "artifact_revision_id": str(getattr(item, "artifact_revision_id", "") or ""),
                }
            )
    return {
        "id": str(getattr(row, "id", "") or ""),
        "environment_id": str(getattr(row, "environment_id", "") or ""),
        "status": str(getattr(row, "status", "") or ""),
        "compose_project": str(getattr(row, "compose_project", "") or ""),
        "ui_url": str(getattr(row, "ui_url", "") or ""),
        "api_url": str(getattr(row, "api_url", "") or ""),
        "runtime_base_url": str(getattr(row, "runtime_base_url", "") or ""),
        "workspace_app_instance_id": str(getattr(row, "workspace_app_instance_id", "") or ""),
        "installed_artifact_slug": str(getattr(row, "installed_artifact_slug", "") or ""),
        "installed_artifacts": installed_artifacts_payload,
    }
