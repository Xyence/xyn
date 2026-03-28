from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from core.models import Job


def ports_yaml(ports: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for port in ports:
        host_port = int(port.get("host") or 0)
        container_port = int(port.get("container") or 0)
        protocol = str(port.get("protocol") or "tcp").strip().lower()
        if protocol not in {"tcp", "udp"}:
            protocol = "tcp"
        lines.append(f'      - "{host_port}:{container_port}/{protocol}"')
    return lines


def materialize_net_inventory_compose(
    *,
    app_spec: dict[str, Any],
    policy_bundle: dict[str, Any] | None = None,
    deployment_dir: Path,
    compose_project: str,
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
    build_resolved_capability_manifest_fn: Callable[[dict[str, Any]], dict[str, Any]],
    effective_net_inventory_image_fn: Callable[[], str],
    ports_yaml_fn: Callable[[list[dict[str, Any]]], list[str]],
) -> Path:
    services = [row for row in app_spec.get("services", []) if isinstance(row, dict)]
    db_service = next((row for row in services if "postgres" in str(row.get("image") or "").lower()), {})
    app_service = next((row for row in services if row is not db_service), {})
    app_service_name = str(app_service.get("name") or "generated-app-api").strip() or "generated-app-api"
    db_service_name = str(db_service.get("name") or "generated-app-db").strip() or "generated-app-db"
    db_env = db_service.get("env") if isinstance(db_service.get("env"), dict) else {}
    app_env = app_service.get("env") if isinstance(app_service.get("env"), dict) else {}
    db_name = str(db_env.get("POSTGRES_DB") or "generated_app").strip() or "generated_app"
    db_user = str(db_env.get("POSTGRES_USER") or "xyn").strip() or "xyn"
    db_password = str(db_env.get("POSTGRES_PASSWORD") or "xyn_dev_password").strip() or "xyn_dev_password"
    entity_contracts_json = json.dumps(
        build_resolved_capability_manifest_fn(app_spec).get("entities") or [],
        separators=(",", ":"),
        sort_keys=True,
    ).replace("'", "''")
    workflow_definitions_json = json.dumps(
        app_spec.get("workflow_definitions") if isinstance(app_spec.get("workflow_definitions"), list) else [],
        separators=(",", ":"),
        sort_keys=True,
    ).replace("'", "''")
    primitive_composition_json = json.dumps(
        app_spec.get("platform_primitive_composition") if isinstance(app_spec.get("platform_primitive_composition"), list) else [],
        separators=(",", ":"),
        sort_keys=True,
    ).replace("'", "''")
    requires_primitives_json = json.dumps(
        app_spec.get("requires_primitives") if isinstance(app_spec.get("requires_primitives"), list) else [],
        separators=(",", ":"),
        sort_keys=True,
    ).replace("'", "''")
    ui_surfaces_text = " ".join(str(app_spec.get("ui_surfaces") or "").splitlines()).replace('"', '\\"')
    shell_base_url = str(os.getenv("XYN_SHELL_BASE_URL", "http://localhost:3000") or "").strip()
    policy_bundle_json = json.dumps(policy_bundle or {}, separators=(",", ":"), sort_keys=True).replace("'", "''")
    app_image = str(app_service.get("image") or effective_net_inventory_image_fn())
    app_ports = ports_yaml_fn(list(app_service.get("ports") or [{"host": 0, "container": 8080, "protocol": "tcp"}]))
    app_network_lines: list[str] = []
    trailer_lines: list[str] = []
    if external_network_name:
        alias = str(external_network_alias or f"{compose_project}-api").strip() or f"{compose_project}-api"
        app_network_lines = [
            "    networks:",
            "      default:",
            "      sibling-runtime:",
            "        aliases:",
            f"          - {alias}",
        ]
        trailer_lines = [
            "",
            "networks:",
            "  sibling-runtime:",
            "    external: true",
            f"    name: {external_network_name}",
        ]
    compose = deployment_dir / "docker-compose.yml"
    compose.write_text(
        "\n".join(
            [
                "services:",
                f"  {db_service_name}:",
                f"    image: {db_service.get('image') or 'postgres:16-alpine'}",
                f"    container_name: {compose_project}-db",
                "    restart: unless-stopped",
                "    environment:",
                f"      POSTGRES_DB: \"{db_name}\"",
                f"      POSTGRES_USER: \"{db_user}\"",
                f"      POSTGRES_PASSWORD: \"{db_password}\"",
                "    healthcheck:",
                f"      test: [\"CMD-SHELL\", \"pg_isready -U {db_user} -d {db_name}\"]",
                "      interval: 5s",
                "      timeout: 5s",
                "      retries: 20",
                "",
                f"  {app_service_name}:",
                f"    image: {app_image}",
                f"    container_name: {compose_project}-api",
                "    restart: unless-stopped",
                "    environment:",
                f"      PORT: \"{str(app_env.get('PORT') or '8080')}\"",
                f"      SERVICE_NAME: \"{str(app_env.get('SERVICE_NAME') or app_service_name)}\"",
                f"      APP_TITLE: \"{str(app_env.get('APP_TITLE') or app_spec.get('title') or app_service_name)}\"",
                f"      DATABASE_URL: \"{str(app_env.get('DATABASE_URL') or f'postgresql://{db_user}:{db_password}@{db_service_name}:5432/{db_name}')}\"",
                f"      GENERATED_ENTITY_CONTRACTS_JSON: '{entity_contracts_json}'",
                f"      GENERATED_POLICY_BUNDLE_JSON: '{policy_bundle_json}'",
                f"      GENERATED_WORKFLOW_DEFINITIONS_JSON: '{workflow_definitions_json}'",
                f"      GENERATED_PLATFORM_PRIMITIVE_COMPOSITION_JSON: '{primitive_composition_json}'",
                f"      GENERATED_REQUIRES_PRIMITIVES_JSON: '{requires_primitives_json}'",
                f"      GENERATED_UI_SURFACES_TEXT: \"{ui_surfaces_text}\"",
                f"      SHELL_BASE_URL: \"{shell_base_url}\"",
                "      GENERATED_ENTITY_CONTRACTS_ALLOW_DEFAULTS: \"0\"",
                "    ports:",
                *app_ports,
                *app_network_lines,
                "    depends_on:",
                f"      {db_service_name}:",
                "        condition: service_healthy",
                "",
                *trailer_lines,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return compose


def deploy_generated_runtime(
    *,
    app_spec: dict[str, Any],
    policy_bundle: dict[str, Any] | None,
    deployment_dir: Path,
    compose_project: str,
    logs: list[str],
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
    materialize_compose_fn: Callable[..., Path],
    append_job_log_fn: Callable[[list[str], str], None],
    run_fn: Callable[..., tuple[int, str, str]],
    resolve_published_port_fn: Callable[[str, str], int],
) -> dict[str, Any]:
    compose_path = materialize_compose_fn(
        app_spec=app_spec,
        policy_bundle=policy_bundle,
        deployment_dir=deployment_dir,
        compose_project=compose_project,
        external_network_name=external_network_name,
        external_network_alias=external_network_alias,
    )
    append_job_log_fn(logs, f"Wrote compose: {compose_path}")

    down_cmd = ["docker", "compose", "-p", compose_project, "-f", str(compose_path), "down", "--remove-orphans", "--volumes"]
    up_cmd = ["docker", "compose", "-p", compose_project, "-f", str(compose_path), "up", "-d"]
    down_code, down_stdout, down_stderr = run_fn(down_cmd, cwd=deployment_dir)
    append_job_log_fn(logs, f"Executed: {' '.join(down_cmd)}")
    if down_stdout:
        append_job_log_fn(logs, f"compose down stdout: {down_stdout[-600:]}")
    if down_stderr:
        append_job_log_fn(logs, f"compose down stderr: {down_stderr[-600:]}")
    code, stdout, stderr = run_fn(up_cmd, cwd=deployment_dir)
    append_job_log_fn(logs, f"Executed: {' '.join(up_cmd)}")
    if stdout:
        append_job_log_fn(logs, f"compose stdout: {stdout[-600:]}")
    if code != 0:
        run_fn(["docker", "compose", "-p", compose_project, "-f", str(compose_path), "down", "--remove-orphans"], cwd=deployment_dir)
        raise RuntimeError(f"docker compose up failed: {stderr or stdout}")
    app_port = resolve_published_port_fn(f"{compose_project}-api", "8080/tcp")
    alias = str(external_network_alias or f"{compose_project}-api").strip() or f"{compose_project}-api"
    output = {
        "compose_project": compose_project,
        "deployment_dir": str(deployment_dir),
        "compose_path": str(compose_path),
        "app_container_name": f"{compose_project}-api",
        "app_url": f"http://localhost:{app_port}",
        "ports": {"app_tcp": app_port},
    }
    if external_network_name:
        output["runtime_base_url"] = f"http://{alias}:8080"
        output["runtime_owner"] = "sibling"
        output["external_network"] = external_network_name
        output["network_alias"] = alias
    return output


def handle_deploy_app_local(
    *,
    db: Session,
    job: Job,
    logs: list[str],
    parse_stage_input_fn: Callable[[dict[str, Any]], Any],
    safe_slug_fn: Callable[..., str],
    deployments_root_fn: Callable[[], Path],
    utc_now_fn: Callable[[], datetime],
    deploy_generated_runtime_fn: Callable[..., dict[str, Any]],
    record_stage_metadata_fn: Callable[..., Any],
    update_execution_note_fn: Callable[..., Any],
    append_job_log_fn: Callable[[list[str], str], None],
    build_stage_output_fn: Callable[..., Any],
    build_follow_up_fn: Callable[..., Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = parse_stage_input_fn(job.input_json).to_dict()
    execution_note_artifact_id = str(payload.get("execution_note_artifact_id") or "").strip()
    generated_artifact = payload.get("generated_artifact") if isinstance(payload.get("generated_artifact"), dict) else {}
    app_spec = payload.get("app_spec") if isinstance(payload.get("app_spec"), dict) else {}
    policy_bundle = payload.get("policy_bundle") if isinstance(payload.get("policy_bundle"), dict) else {}
    policy_source = str(payload.get("policy_source") or "reconstructed").strip() or "reconstructed"
    policy_artifact_ref = payload.get("policy_artifact_ref") if isinstance(payload.get("policy_artifact_ref"), dict) else {}
    policy_compatibility = str(payload.get("policy_compatibility") or "unknown").strip() or "unknown"
    policy_compatibility_reason = str(payload.get("policy_compatibility_reason") or "").strip()
    app_slug = safe_slug_fn(str(app_spec.get("app_slug") or "net-inventory"), default="net-inventory")
    stamp = utc_now_fn().strftime("%Y%m%d%H%M%S")
    deployment_dir = deployments_root_fn() / app_slug / stamp
    deployment_dir.mkdir(parents=True, exist_ok=True)
    compose_project = safe_slug_fn(f"xyn-app-{app_slug}", default="xyn-app")
    app_output = {
        "app_slug": app_slug,
        "policy_source": policy_source,
        "policy_artifact_ref": policy_artifact_ref,
        "policy_compatibility": policy_compatibility,
        "policy_compatibility_reason": policy_compatibility_reason,
        **deploy_generated_runtime_fn(
            app_spec=app_spec,
            policy_bundle=policy_bundle,
            deployment_dir=deployment_dir,
            compose_project=compose_project,
            logs=logs,
        ),
    }
    if execution_note_artifact_id:
        record_stage_metadata_fn(
            db,
            artifact_id=uuid.UUID(execution_note_artifact_id),
            implementation_summary="Materialized local docker-compose deployment for the generated app and resolved a running app URL.",
            append_validation=[
                f"Compose written: {app_output['compose_path']}",
                f"Local deployment started successfully at {app_output['app_url']}.",
            ],
            related_artifact_ids=[
                *[str(item) for item in (payload.get("app_spec_artifact_id"), execution_note_artifact_id) if item],
            ],
            extra_metadata_updates={"app_url": app_output["app_url"], "compose_project": compose_project},
            update_note=update_execution_note_fn,
        )
    append_job_log_fn(logs, f"Local app URL: {app_output['app_url']}")
    append_job_log_fn(logs, "Queued sibling provisioning stage")
    stage_output = build_stage_output_fn(
        output_json=app_output,
        follow_up=[
            build_follow_up_fn(
                job_type="provision_sibling_xyn",
                input_json={
                    "deployment": app_output,
                    "app_spec": app_spec,
                    "policy_bundle": policy_bundle,
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
