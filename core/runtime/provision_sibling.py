from __future__ import annotations

"""Sibling provisioning orchestration.

DEBT-05 / DEMO-02 hardening:
- Emits a canonical ``capability_entry`` block in stage output.
- ``capability_entry`` is artifact-first: installed artifact identity/state is
  the primary open/use source of truth, with runtime URL fallback retained for
  compatibility when install evidence is unavailable.
"""

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from core.models import Job
from core.provisioning_local import ProvisionLocalRequest


def _build_capability_entry(
    *,
    installed_artifact: dict[str, Any] | None,
    generated_artifact: dict[str, Any] | None,
    sibling_output: dict[str, Any],
    sibling_runtime: dict[str, Any] | None,
) -> dict[str, Any]:
    installed = installed_artifact if isinstance(installed_artifact, dict) else {}
    generated = generated_artifact if isinstance(generated_artifact, dict) else {}
    runtime = sibling_runtime if isinstance(sibling_runtime, dict) else {}
    installed_id = str(installed.get("artifact_id") or "").strip()
    installed_slug = str(installed.get("artifact_slug") or "").strip()
    generated_slug = str(generated.get("artifact_slug") or "").strip()
    generated_revision_id = str(generated.get("revision_id") or generated.get("artifact_revision_id") or "").strip()
    installed_revision_id = str(installed.get("artifact_revision_id") or "").strip()
    runtime_base_url = str(runtime.get("runtime_base_url") or "").strip()
    runtime_public_url = str(runtime.get("public_app_url") or runtime.get("app_url") or sibling_output.get("ui_url") or "").strip()
    is_installed = bool(installed_id and installed_slug)

    return {
        "source_of_truth": "installed_artifact" if is_installed else "generated_artifact",
        "state": "installed" if is_installed else "generated_not_installed",
        "installed_artifact": {
            "artifact_id": installed_id,
            "artifact_slug": installed_slug,
            "workspace_id": str(installed.get("workspace_id") or "").strip(),
            "workspace_slug": str(installed.get("workspace_slug") or "").strip(),
            "artifact_revision_id": installed_revision_id,
            "artifact_version_label": str(installed.get("artifact_version_label") or "").strip(),
        },
        "generated_artifact": {
            "artifact_slug": generated_slug,
            "artifact_version": str(generated.get("artifact_version") or "").strip(),
            "artifact_revision_id": generated_revision_id,
            "artifact_version_label": str(generated.get("version_label") or generated.get("artifact_version_label") or "").strip(),
        },
        "open_preference": {
            "mode": "artifact_shell" if is_installed else "runtime_url_fallback",
            "runtime_base_url": runtime_base_url,
            "runtime_public_url": runtime_public_url,
        },
    }


def handle_provision_sibling_xyn(
    *,
    db: Session,
    job: Job,
    logs: list[str],
    parse_stage_input_fn: Callable[[dict[str, Any]], Any],
    safe_slug_fn: Callable[..., str],
    workspace_model: Any,
    find_revision_sibling_target_fn: Callable[..., dict[str, Any] | None],
    append_job_log_fn: Callable[[list[str], str], None],
    provision_local_instance_fn: Callable[[ProvisionLocalRequest], dict[str, Any]],
    prefer_local_platform_images_for_smoke_fn: Callable[[], bool],
    docker_container_running_fn: Callable[[str], bool],
    import_generated_artifact_package_into_registry_fn: Callable[..., dict[str, Any]],
    install_generated_artifact_in_sibling_fn: Callable[..., dict[str, Any]],
    generated_artifact_version: str,
    docker_network_exists_fn: Callable[[str], bool],
    deployments_root_fn: Callable[[], Path],
    deploy_generated_runtime_fn: Callable[..., dict[str, Any]],
    register_sibling_runtime_target_fn: Callable[..., dict[str, Any]],
    record_stage_metadata_fn: Callable[..., Any],
    update_execution_note_fn: Callable[..., Any],
    build_stage_output_fn: Callable[..., Any],
    build_follow_up_fn: Callable[..., Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = parse_stage_input_fn(job.input_json).to_dict()
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    deployment = payload.get("deployment") if isinstance(payload.get("deployment"), dict) else {}
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    app_slug = safe_slug_fn(str(app_spec.get("app_slug") or "net-inventory"), default="net-inventory")
    revision_anchor = app_spec.get("revision_anchor") if isinstance(app_spec.get("revision_anchor"), dict) else {}
    workspace = db.query(workspace_model).filter(workspace_model.id == job.workspace_id).first()
    workspace_slug = str(getattr(workspace, "slug", "default") or "default")
    sibling: dict[str, Any]
    reused_sibling = find_revision_sibling_target_fn(
        db,
        root_workspace_id=job.workspace_id,
        revision_anchor=revision_anchor,
        app_slug=app_slug,
    )
    if reused_sibling:
        sibling = {
            "deployment_id": reused_sibling.get("deployment_id"),
            "compose_project": reused_sibling.get("compose_project"),
            "ui_url": reused_sibling.get("ui_url"),
            "api_url": reused_sibling.get("api_url"),
        }
        append_job_log_fn(
            logs,
            "Reusing anchored sibling Xyn deployment "
            f"deployment_id={sibling.get('deployment_id')} ui_url={sibling.get('ui_url')}",
        )
    else:
        sibling_name = safe_slug_fn(f"smoke-{deployment.get('app_slug') or 'app'}-{str(job.id)[:6]}", default="smoke-app")
        ui_host = f"{sibling_name}.localhost"
        api_host = f"api.{sibling_name}.localhost"
        append_job_log_fn(logs, f"Provisioning sibling Xyn: name={sibling_name} ui_host={ui_host} api_host={api_host}")
        try:
            sibling = provision_local_instance_fn(
                ProvisionLocalRequest(
                    name=sibling_name,
                    force=True,
                    workspace_slug=workspace_slug,
                    ui_host=ui_host,
                    api_host=api_host,
                    prefer_local_images=prefer_local_platform_images_for_smoke_fn(),
                )
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}
            raise RuntimeError(f"Sibling provisioning failed: {detail}") from exc

    sibling_output = {
        "deployment_id": sibling.get("deployment_id"),
        "compose_project": sibling.get("compose_project"),
        "ui_url": sibling.get("ui_url"),
        "api_url": sibling.get("api_url"),
    }
    policy_source = str(payload.get("policy_source") or "reconstructed").strip() or "reconstructed"
    policy_artifact_ref = payload.get("policy_artifact_ref") if isinstance(payload.get("policy_artifact_ref"), dict) else {}
    policy_compatibility = str(payload.get("policy_compatibility") or "unknown").strip() or "unknown"
    policy_compatibility_reason = str(payload.get("policy_compatibility_reason") or "").strip()
    sibling_output["policy_source"] = policy_source
    sibling_output["policy_artifact_ref"] = policy_artifact_ref
    sibling_output["policy_compatibility"] = policy_compatibility
    sibling_output["policy_compatibility_reason"] = policy_compatibility_reason
    sibling_project = str(sibling.get("compose_project") or "").strip()
    sibling_api_container = f"{sibling_project}-api" if sibling_project else ""
    sibling_network = f"{sibling_project}_default" if sibling_project else ""
    installed_artifact: dict[str, Any] | None = None
    sibling_runtime: dict[str, Any] | None = None
    sibling_registry_import: dict[str, Any] = {}
    generated_artifact = payload.get("generated_artifact") if isinstance(payload.get("generated_artifact"), dict) else {}
    if sibling_api_container and docker_container_running_fn(sibling_api_container):
        preferred_artifact_slug = str(generated_artifact.get("artifact_slug") or "").strip()
        preferred_artifact_version = str(generated_artifact.get("artifact_version") or "").strip()
        preferred_artifact_revision_id = str(generated_artifact.get("revision_id") or generated_artifact.get("artifact_revision_id") or "").strip()
        preferred_artifact_version_label = str(generated_artifact.get("version_label") or generated_artifact.get("artifact_version_label") or "").strip()
        preferred_artifact_package_path = Path(str(generated_artifact.get("artifact_package_path") or "")).expanduser()
        if not preferred_artifact_slug or not preferred_artifact_package_path.exists():
            raise RuntimeError(
                f"Generated artifact package is missing for sibling install: "
                f"slug={preferred_artifact_slug or '<empty>'} path={preferred_artifact_package_path}"
            )
        sibling_registry_import = import_generated_artifact_package_into_registry_fn(
            container_name=sibling_api_container,
            artifact_slug=preferred_artifact_slug,
            package_path=preferred_artifact_package_path,
            port=8000,
            workspace_slug=workspace_slug,
        )
        append_job_log_fn(
            logs,
            "Imported generated artifact "
            f"{preferred_artifact_slug}@{preferred_artifact_version or generated_artifact_version}"
            + (f" revision={preferred_artifact_revision_id}" if preferred_artifact_revision_id else "")
            + " into sibling registry",
        )
        installed_artifact = install_generated_artifact_in_sibling_fn(
            sibling_api_container=sibling_api_container,
            workspace_slug=workspace_slug,
            artifact_slug=preferred_artifact_slug,
            artifact_version=preferred_artifact_version,
            artifact_revision_id=preferred_artifact_revision_id,
        )
        if preferred_artifact_version_label and isinstance(installed_artifact, dict) and not str(installed_artifact.get("artifact_version_label") or "").strip():
            installed_artifact["artifact_version_label"] = preferred_artifact_version_label
        append_job_log_fn(
            logs,
            "Installed generated artifact "
            f"{preferred_artifact_slug}@{preferred_artifact_version or 'latest'}"
            + (f" revision={str(installed_artifact.get('artifact_revision_id') or preferred_artifact_revision_id or '')}" if (installed_artifact or preferred_artifact_revision_id) else "")
            + " into sibling workspace",
        )
    sibling_output["installed_artifact"] = installed_artifact
    sibling_output["installed_artifact_source"] = "generated"
    if sibling_registry_import:
        sibling_output["generated_artifact_registry_import"] = sibling_registry_import
    append_job_log_fn(
        logs,
        "Installed sibling artifact "
        f"workspace={installed_artifact.get('workspace_slug')} artifact={installed_artifact.get('artifact_slug')} "
        "source=generated",
    )
    if not sibling_network or not docker_network_exists_fn(sibling_network):
        raise RuntimeError(f"Sibling network not available for runtime target registration: {sibling_network or '<empty>'}")
    sibling_stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    reused_runtime_target = (
        reused_sibling.get("runtime_target")
        if isinstance(reused_sibling, dict) and isinstance(reused_sibling.get("runtime_target"), dict)
        else {}
    )
    sibling_runtime_project = str(reused_runtime_target.get("compose_project") or "").strip() or safe_slug_fn(
        f"xyn-sibling-{app_slug}-{str(job.id)[:6]}",
        default="xyn-sibling-app",
    )
    sibling_runtime_dir = deployments_root_fn() / app_slug / f"sibling-{sibling_stamp}-{str(job.id)[:6]}"
    sibling_runtime_dir.mkdir(parents=True, exist_ok=True)
    sibling_runtime = deploy_generated_runtime_fn(
        app_spec=app_spec,
        policy_bundle=payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {},
        deployment_dir=sibling_runtime_dir,
        compose_project=sibling_runtime_project,
        logs=logs,
        external_network_name=str(reused_runtime_target.get("external_network") or sibling_network),
        external_network_alias=str(reused_runtime_target.get("network_alias") or f"{sibling_runtime_project}-api"),
    )
    sibling_runtime.update(
        {
            "app_slug": app_slug,
            "runtime_owner": "sibling",
            "source_build_job_id": str(payload.get("source_job_id") or ""),
            "source_workspace_id": str(job.workspace_id),
            "installed_revision_id": str((installed_artifact or {}).get("artifact_revision_id") or ""),
        }
    )
    registration = register_sibling_runtime_target_fn(
        sibling_api_container=sibling_api_container,
        workspace_id=str(installed_artifact.get("workspace_id") or ""),
        app_slug=app_slug,
        artifact_slug=str(installed_artifact.get("artifact_slug") or generated_artifact.get("artifact_slug") or f"app.{app_slug}"),
        title=str(app_spec.get("title") or app_slug),
        runtime_target=sibling_runtime,
        sibling_ui_url=str(sibling_output.get("ui_url") or ""),
        sibling_api_url=str(sibling_output.get("api_url") or ""),
    )
    sibling_output["runtime_target"] = sibling_runtime
    sibling_output["runtime_registration"] = registration
    sibling_output["capability_entry"] = _build_capability_entry(
        installed_artifact=installed_artifact,
        generated_artifact=generated_artifact,
        sibling_output=sibling_output,
        sibling_runtime=sibling_runtime,
    )
    append_job_log_fn(
        logs,
        "Registered sibling-owned runtime target "
        f"base_url={sibling_runtime.get('runtime_base_url')} workspace={installed_artifact.get('workspace_slug')}",
    )
    if execution_note_artifact_id:
        record_stage_metadata_fn(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Provisioned a sibling Xyn instance as the next validation environment for the generated application.",
            append_validation=[
                f"Sibling Xyn provisioned with ui_url={sibling_output.get('ui_url')}",
                f"Sibling Xyn provisioned with api_url={sibling_output.get('api_url')}",
                (
                    f"Installed generated artifact {installed_artifact.get('artifact_slug')} into sibling workspace "
                    f"{installed_artifact.get('workspace_slug')}"
                    if installed_artifact
                    else "No sibling artifact installation was recorded."
                ),
                (
                    f"Registered sibling-owned runtime target {sibling_runtime.get('runtime_base_url')}"
                    if sibling_runtime
                    else "No sibling-owned runtime target was registered."
                ),
            ],
            extra_metadata_updates={
                "sibling_ui_url": sibling_output.get("ui_url"),
                "sibling_api_url": sibling_output.get("api_url"),
                "sibling_installed_artifact_slug": installed_artifact.get("artifact_slug") if installed_artifact else None,
                "sibling_runtime_base_url": sibling_runtime.get("runtime_base_url") if sibling_runtime else None,
            },
            update_note=update_execution_note_fn,
        )
    stage_output = build_stage_output_fn(
        output_json=sibling_output,
        follow_up=[
            build_follow_up_fn(
                job_type="smoke_test",
                input_json={
                    "deployment": deployment,
                    "sibling": sibling_output,
                    "app_spec": app_spec,
                    "policy_bundle": payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {},
                    "policy_source": policy_source,
                    "policy_artifact_ref": policy_artifact_ref,
                    "policy_compatibility": policy_compatibility,
                    "policy_compatibility_reason": policy_compatibility_reason,
                    "generated_artifact": generated_artifact,
                    "execution_note_artifact_id": execution_note_artifact_id,
                    "source_job_id": str(job.id),
                },
            )
        ],
    )
    return stage_output.output_json, [item.to_dict() for item in stage_output.follow_up]
