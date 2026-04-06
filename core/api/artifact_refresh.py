"""Artifact refresh helpers for local dev stacks."""
from __future__ import annotations

import os
import subprocess
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from core.access_control import CAP_REFRESHES_RUN, AccessPrincipal, require_capabilities

router = APIRouter()


class ArtifactRefreshRequest(BaseModel):
    artifacts: list[str] = Field(default_factory=lambda: ["xyn-ui", "xyn-api", "net-inventory-api"])
    channel: str = ""
    pull_net_inventory: bool = True
    restart_hint: bool = False


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _image_exists_locally(image: str) -> bool:
    code, _, _ = _run(["docker", "image", "inspect", image])
    return code == 0


@router.post("/artifacts/refresh", response_model=dict[str, Any])
async def refresh_artifacts(
    payload: ArtifactRefreshRequest,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_REFRESHES_RUN)),
):
    registry = str(os.getenv("XYN_ARTIFACT_REGISTRY", "public.ecr.aws/i0h0h0n4/xyn/artifacts")).strip().rstrip("/")
    channel = str(payload.channel or os.getenv("XYN_ARTIFACT_CHANNEL", "")).strip() or "dev"
    artifact_names = [str(item).strip() for item in (payload.artifacts or []) if str(item).strip()]
    if not artifact_names:
        artifact_names = ["xyn-ui", "xyn-api"]
    if payload.pull_net_inventory and "net-inventory-api" not in artifact_names:
        artifact_names.append("net-inventory-api")
    explicit_map = {
        "xyn-ui": str(os.getenv("XYN_UI_IMAGE", "")).strip(),
        "xyn-api": str(os.getenv("XYN_API_IMAGE", "")).strip(),
    }
    images = []
    for name in artifact_names:
        explicit_image = explicit_map.get(name, "")
        if explicit_image:
            images.append(explicit_image)
        else:
            images.append(f"{registry}/{name}:{channel}")

    results: list[dict[str, Any]] = []
    for image in images:
        code, stdout, stderr = _run(["docker", "pull", image])
        status = "succeeded" if code == 0 else "failed"
        if code != 0 and _image_exists_locally(image):
            status = "local_only"
        results.append(
            {
                "image": image,
                "status": status,
                "stdout": stdout[-800:],
                "stderr": stderr[-800:],
            }
        )
    has_failures = any(item["status"] == "failed" for item in results)
    has_local_only = any(item["status"] == "local_only" for item in results)
    return {
        "status": "failed" if has_failures else ("succeeded_with_warnings" if has_local_only else "succeeded"),
        "results": results,
        "restart_hint": "Run './xynctl stop && ./xynctl quickstart --force' to relaunch with latest artifacts."
        if payload.restart_hint
        else "",
        "requested": {"artifacts": artifact_names, "channel": channel},
    }
