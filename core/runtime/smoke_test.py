from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from core.models import Job


def handle_smoke_test(
    *,
    db: Session,
    job: Job,
    logs: list[str],
    parse_stage_input_fn: Callable[[dict[str, Any]], Any],
    workspace_model: Any,
    append_job_log_fn: Callable[[list[str], str], None],
    wait_for_container_http_ok_fn: Callable[..., bool],
    app_deploy_health_timeout_seconds: int,
    container_http_json_fn: Callable[..., tuple[int, dict[str, Any], str]],
    build_resolved_capability_manifest_fn: Callable[[dict[str, Any]], dict[str, Any]],
    exercise_runtime_contracts_fn: Callable[..., dict[str, Any]],
    root_platform_api_container: str,
    container_http_session_json_fn: Callable[..., tuple[int, dict[str, Any], str]],
    docker_container_running_fn: Callable[[str], bool],
    execute_sibling_palette_prompt_fn: Callable[..., tuple[int, dict[str, Any], str]],
    run_fn: Callable[..., tuple[int, str, str]],
    finalize_stage_note_fn: Callable[..., Any],
    update_execution_note_fn: Callable[..., Any],
    build_stage_output_fn: Callable[..., Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = parse_stage_input_fn(job.input_json).to_dict()
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    deployment = payload.get("deployment") if isinstance(payload.get("deployment"), dict) else {}
    sibling = payload.get("sibling") if isinstance(payload.get("sibling"), dict) else {}
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    policy_bundle = payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {}
    generated_artifact = payload.get("generated_artifact") if isinstance(payload.get("generated_artifact"), dict) else {}
    app_container_name = str(deployment.get("app_container_name") or "").strip()
    if not app_container_name:
        raise RuntimeError("smoke_test missing deployment.app_container_name")

    append_job_log_fn(logs, f"Waiting for app health in container: {app_container_name}")
    if not wait_for_container_http_ok_fn(
        app_container_name,
        "/health",
        port=8080,
        timeout_seconds=app_deploy_health_timeout_seconds,
    ):
        raise RuntimeError(f"App health endpoint did not become ready in {app_container_name}")

    workspace = db.query(workspace_model).filter(workspace_model.id == job.workspace_id).first()
    workspace_slug = str(getattr(workspace, "slug", "default") or "default")
    health_code, health_body, health_text = container_http_json_fn(app_container_name, "GET", "/health", port=8080)
    if health_code != 200:
        raise RuntimeError(f"App health check failed ({health_code}): {health_text}")
    resolved_manifest = build_resolved_capability_manifest_fn(app_spec)
    entity_contracts = resolved_manifest.get("entities") if isinstance(resolved_manifest.get("entities"), list) else []
    if not entity_contracts:
        raise RuntimeError("Generated app contract smoke requires resolved entity contracts")
    local_contract_checks = exercise_runtime_contracts_fn(
        container_name=app_container_name,
        port=8080,
        workspace_id=str(job.workspace_id),
        entity_contracts=entity_contracts,
        policy_bundle=policy_bundle,
    )

    generated_artifact_slug = str(generated_artifact.get("artifact_slug") or "").strip()
    generated_artifact_version = str(generated_artifact.get("artifact_version") or "").strip()
    generated_artifact_revision_id = str(
        generated_artifact.get("revision_id")
        or generated_artifact.get("artifact_revision_id")
        or ""
    ).strip()
    registry_catalog: dict[str, Any] = {}
    if generated_artifact_slug:
        registry_status, registry_body, registry_text = container_http_session_json_fn(
            root_platform_api_container,
            port=8000,
            steps=[
                {
                    "method": "POST",
                    "path": "/auth/dev-login",
                    "form": {"appId": "xyn-ui", "returnTo": "/app"},
                },
                {
                    "method": "GET",
                    "path": "/xyn/api/artifacts/catalog",
                },
            ],
        )
        if registry_status != 200:
            raise RuntimeError(f"Registry catalog check failed ({registry_status}): {registry_text}")
        registry_rows = registry_body.get("artifacts") if isinstance(registry_body.get("artifacts"), list) else []
        registry_match = next(
            (
                row
                for row in registry_rows
                if isinstance(row, dict)
                and str(row.get("slug") or "").strip() == generated_artifact_slug
                and str(row.get("package_version") or "").strip() == generated_artifact_version
            ),
            None,
        )
        if isinstance(registry_match, dict):
            if generated_artifact_revision_id:
                catalog_revision_id = str(
                    registry_match.get("artifact_revision_id")
                    or (registry_match.get("metadata") or {}).get("artifact_revision_id")
                    or (registry_match.get("metadata") or {}).get("revision_id")
                    or ""
                ).strip()
                if catalog_revision_id and catalog_revision_id != generated_artifact_revision_id:
                    raise RuntimeError(
                        "Registry generated artifact revision mismatch: "
                        f"expected={generated_artifact_revision_id} actual={catalog_revision_id}"
                    )
            registry_catalog = registry_match
        else:
            installed_artifact = sibling.get("installed_artifact") if isinstance(sibling.get("installed_artifact"), dict) else {}
            installed_slug = str(installed_artifact.get("artifact_slug") or "").strip()
            installed_id = str(installed_artifact.get("artifact_id") or "").strip()
            if installed_slug == generated_artifact_slug and installed_id:
                installed_revision_id = str(installed_artifact.get("artifact_revision_id") or "").strip()
                if generated_artifact_revision_id and installed_revision_id and installed_revision_id != generated_artifact_revision_id:
                    raise RuntimeError(
                        "Generated artifact revision mismatch during registry fallback: "
                        f"expected={generated_artifact_revision_id} actual={installed_revision_id}"
                    )
                registry_catalog = {
                    "source": "installed_artifact_fallback",
                    "artifact_slug": installed_slug,
                    "artifact_id": installed_id,
                    "artifact_version": generated_artifact_version,
                    "artifact_revision_id": installed_revision_id or generated_artifact_revision_id,
                }
                append_job_log_fn(
                    logs,
                    "Generated artifact not present in catalog; using installed artifact evidence from provisioning output.",
                )
            else:
                raise RuntimeError(
                    f"Generated artifact {generated_artifact_slug}@{generated_artifact_version} not found in registry catalog"
                )

    sibling_project = str(sibling.get("compose_project") or "").strip()
    sibling_api_container = f"{sibling_project}-api" if sibling_project else ""
    sibling_ui_container = f"{sibling_project}-ui" if sibling_project else ""
    if not sibling_api_container or not docker_container_running_fn(sibling_api_container):
        raise RuntimeError("Sibling API container is not running")
    if not sibling_ui_container or not docker_container_running_fn(sibling_ui_container):
        raise RuntimeError("Sibling UI container is not running")
    sibling_health_code = 0
    sibling_health_body: dict[str, Any] | str = {}
    sibling_health_text = ""
    for health_path in ("/health", "/api/v1/health", "/xyn/api/health", "/xyn/api/v1/health", "/", "/xyn/api/auth/mode", "/xyn/api/me"):
        code, body, text = container_http_json_fn(sibling_api_container, "GET", health_path, port=8000)
        if code in {200, 401}:
            sibling_health_code = code
            sibling_health_body = body or {"path": health_path}
            sibling_health_text = text
            break
    if sibling_health_code != 200:
        raise RuntimeError(f"Sibling API health check failed ({code}): {text}")
    append_job_log_fn(logs, f"Sibling health OK: {sibling.get('api_url')}")
    sibling_runtime = sibling.get("runtime_target") if isinstance(sibling.get("runtime_target"), dict) else {}
    sibling_runtime_container = str(sibling_runtime.get("app_container_name") or "").strip()
    sibling_runtime_base_url = str(sibling_runtime.get("runtime_base_url") or "").strip()
    sibling_runtime_public_url = str(
        sibling_runtime.get("public_app_url") or sibling_runtime.get("app_url") or sibling.get("ui_url") or ""
    ).strip()
    sibling_workspace_id = str((sibling.get("installed_artifact") or {}).get("workspace_id") or "").strip()
    sibling_workspace_slug = str((sibling.get("installed_artifact") or {}).get("workspace_slug") or workspace_slug).strip() or workspace_slug
    if not sibling_runtime_container or not docker_container_running_fn(sibling_runtime_container):
        raise RuntimeError("Sibling runtime container is not running")
    if not sibling_workspace_id:
        raise RuntimeError("Sibling installed artifact workspace id missing")
    if not wait_for_container_http_ok_fn(
        sibling_runtime_container,
        "/health",
        port=8080,
        timeout_seconds=app_deploy_health_timeout_seconds,
    ):
        raise RuntimeError(f"Sibling runtime health endpoint did not become ready in {sibling_runtime_container}")
    sibling_runtime_health_code, sibling_runtime_health_body, sibling_runtime_health_text = container_http_json_fn(
        sibling_runtime_container,
        "GET",
        "/health",
        port=8080,
    )
    if sibling_runtime_health_code != 200:
        raise RuntimeError(f"Sibling runtime health check failed ({sibling_runtime_health_code}): {sibling_runtime_health_text}")
    sibling_artifacts_status, sibling_artifacts_body, sibling_artifacts_text = container_http_session_json_fn(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "GET",
                "path": f"/xyn/api/workspaces/{sibling_workspace_id}/artifacts",
            },
        ],
    )
    if sibling_artifacts_status != 200:
        raise RuntimeError(f"Sibling artifact listing failed ({sibling_artifacts_status}): {sibling_artifacts_text}")
    sibling_artifacts = sibling_artifacts_body.get("artifacts") if isinstance(sibling_artifacts_body.get("artifacts"), list) else []
    if generated_artifact_slug:
        sibling_match = next(
            (
                row
                for row in sibling_artifacts
                if isinstance(row, dict)
                and str(row.get("slug") or "").strip() == generated_artifact_slug
                and str(row.get("package_version") or "").strip() == generated_artifact_version
            ),
            None,
        )
        if not isinstance(sibling_match, dict):
            raise RuntimeError(
                f"Sibling workspace is missing generated artifact {generated_artifact_slug}@{generated_artifact_version}"
            )
        if generated_artifact_revision_id:
            sibling_revision_id = str(
                sibling_match.get("artifact_revision_id")
                or (sibling_match.get("metadata") or {}).get("artifact_revision_id")
                or (sibling_match.get("metadata") or {}).get("revision_id")
                or ""
            ).strip()
            installed_revision_id = str((sibling.get("installed_artifact") or {}).get("artifact_revision_id") or "").strip()
            candidate_revision_id = sibling_revision_id or installed_revision_id
            if candidate_revision_id and candidate_revision_id != generated_artifact_revision_id:
                raise RuntimeError(
                    "Sibling installed artifact revision mismatch: "
                    f"expected={generated_artifact_revision_id} actual={candidate_revision_id}"
                )
    sibling_contract_checks = exercise_runtime_contracts_fn(
        container_name=sibling_runtime_container,
        port=8080,
        workspace_id=sibling_workspace_id,
        entity_contracts=entity_contracts,
        policy_bundle=policy_bundle,
    )

    manifest = build_resolved_capability_manifest_fn(app_spec)
    list_commands = [
        row
        for row in (manifest.get("commands") if isinstance(manifest.get("commands"), list) else [])
        if isinstance(row, dict) and str(row.get("operation_kind") or "") == "list"
    ]
    if not list_commands:
        raise RuntimeError("Generated app contract smoke requires at least one declared list command")
    primary_list_command = list_commands[0]
    palette_prompt = str(primary_list_command.get("prompt") or "").strip()
    palette_status, palette_result, palette_text = execute_sibling_palette_prompt_fn(
        sibling_api_container=sibling_api_container,
        workspace_slug=sibling_workspace_slug,
        prompt=palette_prompt,
    )
    if palette_status != 200:
        raise RuntimeError(f"Sibling palette request failed ({palette_status}): {palette_text}")
    if palette_result.get("kind") != "table":
        raise RuntimeError(f"Palette did not return table: {palette_result}")
    if not isinstance(palette_result.get("rows"), list) or not palette_result.get("rows"):
        raise RuntimeError(f"Palette {palette_prompt} returned no rows")
    palette_meta = palette_result.get("meta") if isinstance(palette_result.get("meta"), dict) else {}
    palette_base_url = str(palette_meta.get("base_url") or "").strip()
    allowed_runtime_urls = {value for value in (sibling_runtime_base_url, sibling_runtime_public_url) if value}
    if allowed_runtime_urls and palette_base_url not in allowed_runtime_urls:
        raise RuntimeError(
            "Sibling palette targeted unexpected runtime base URL: "
            f"{palette_meta.get('base_url')} not in {sorted(allowed_runtime_urls)}"
        )
    append_job_log_fn(logs, f"Palette check returned {len(palette_result.get('rows') or [])} rows for {palette_prompt}")

    stopped_root_runtime = {"status": "skipped"}
    restarted_root_runtime = {"status": "skipped"}
    palette_after_root_stop: dict[str, Any] = {}
    compose_path = Path(str(deployment.get("compose_path") or "").strip())
    compose_project = str(deployment.get("compose_project") or "").strip()
    if compose_path.exists() and compose_project:
        stop_code, stop_out, stop_err = run_fn(["docker", "compose", "-p", compose_project, "-f", str(compose_path), "stop"])
        if stop_code != 0:
            raise RuntimeError(f"Failed to stop root runtime during smoke validation: {stop_err or stop_out}")
        stopped_root_runtime = {"status": "stopped", "stdout": stop_out, "stderr": stop_err}
        try:
            palette_after_stop_status, palette_after_stop_result, palette_after_stop_text = execute_sibling_palette_prompt_fn(
                sibling_api_container=sibling_api_container,
                workspace_slug=sibling_workspace_slug,
                prompt=palette_prompt,
            )
            if palette_after_stop_status != 200:
                raise RuntimeError(
                    f"Sibling palette after root stop failed ({palette_after_stop_status}): {palette_after_stop_text}"
                )
            if not isinstance(palette_after_stop_result.get("rows"), list) or not palette_after_stop_result.get("rows"):
                raise RuntimeError("Sibling palette after root stop returned no rows")
            after_stop_meta = palette_after_stop_result.get("meta") if isinstance(palette_after_stop_result.get("meta"), dict) else {}
            after_stop_base_url = str(after_stop_meta.get("base_url") or "").strip()
            if allowed_runtime_urls and after_stop_base_url not in allowed_runtime_urls:
                raise RuntimeError(
                    "Sibling palette after root stop targeted unexpected runtime base URL: "
                    f"{after_stop_meta.get('base_url')} not in {sorted(allowed_runtime_urls)}"
                )
            palette_after_root_stop = palette_after_stop_result
        finally:
            restart_code, restart_out, restart_err = run_fn(["docker", "compose", "-p", compose_project, "-f", str(compose_path), "up", "-d"])
            restarted_root_runtime = {"status": "restarted" if restart_code == 0 else "failed", "stdout": restart_out, "stderr": restart_err}
            if restart_code != 0:
                raise RuntimeError(f"Failed to restart root runtime after smoke validation: {restart_err or restart_out}")
            if not wait_for_container_http_ok_fn(
                app_container_name,
                "/health",
                port=8080,
                timeout_seconds=app_deploy_health_timeout_seconds,
            ):
                raise RuntimeError("Root runtime did not become healthy after restart")
    if execution_note_artifact_id:
        finalize_stage_note_fn(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Completed platform plumbing checks and generated-application contract smoke for the deployed application.",
            append_validation=[
                "Platform plumbing smoke passed: local runtime, sibling API, sibling runtime, and artifact install all became reachable.",
                f"Generated app contract smoke passed for entities: {', '.join(str(row.get('key') or '') for row in entity_contracts)}.",
                f"Palette returned {len(palette_result.get('rows') or [])} rows for {palette_prompt}.",
                (
                    f"Sibling palette still returned {len(palette_after_root_stop.get('rows') or [])} rows after root runtime stop."
                    if palette_after_root_stop
                    else "Sibling palette was not revalidated after root runtime stop."
                ),
            ],
            status="completed",
            update_note=update_execution_note_fn,
        )

    stage_output = build_stage_output_fn(
        output_json={
            "platform_plumbing": {
                "app_health": {"code": health_code, "body": health_body or health_text},
                "sibling_health": {"code": sibling_health_code, "body": sibling_health_body or sibling_health_text},
                "sibling_runtime": {
                    "base_url": sibling_runtime_base_url,
                    "health": {"code": sibling_runtime_health_code, "body": sibling_runtime_health_body or sibling_runtime_health_text},
                },
                "generated_artifact": {
                    "registry_catalog": registry_catalog,
                    "installed_in_sibling": generated_artifact_slug,
                    "installed_version": generated_artifact_version,
                    "installed_revision_id": generated_artifact_revision_id
                    or str((sibling.get("installed_artifact") or {}).get("artifact_revision_id") or "").strip(),
                },
                "capability_entry": sibling.get("capability_entry") if isinstance(sibling.get("capability_entry"), dict) else {},
            },
            "generated_app_contract_smoke": {
                "local_runtime": local_contract_checks,
                "sibling_runtime": sibling_contract_checks,
                "palette": {"prompt": palette_prompt, "result": palette_result},
            },
            "sibling_xyn": sibling,
            "palette_after_root_runtime_stop": palette_after_root_stop,
            "root_runtime_stop": stopped_root_runtime,
            "root_runtime_restart": restarted_root_runtime,
            "status": "passed",
        },
    )
    return stage_output.output_json, [item.to_dict() for item in stage_output.follow_up]
