"""App-intent job orchestration shell.

This module owns worker lifecycle and stage routing for the app-intent pipeline
(`generate_app_spec`, `deploy_app_local`, `provision_sibling_xyn`, `smoke_test`).

Most substantial logic clusters have been extracted into dedicated modules
(AppSpec inference, runtime adapters/deploy/provision/smoke orchestration,
runtime contract verification, policy compilation, persistence helpers).

Compatibility wrappers and symbol rebinds are intentionally preserved here to
keep existing patch points and tests stable while the orchestration shell
remains the canonical dispatch boundary.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
import uuid
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jsonschema import ValidationError, validate
from sqlalchemy.orm import Session

from core.database import SessionLocal
from core.context_packs import default_instance_workspace_root
from core.capability_manifest import build_manifest_suggestions, build_resolved_capability_manifest
from core.execution_notes import create_execution_note, update_execution_note
from core.environment_state import (
    create_or_update_activation,
    ensure_default_environment,
    mark_activation_failed,
    upsert_sibling_from_provision_output,
)
from core.models import Environment, Job, JobStatus, Workspace
from core.appspec import entity_inference as appspec_entity_inference
from core.appspec import canonicalize as appspec_canonicalize
from core.appspec import consistency as appspec_consistency
from core.appspec import contract_validation as appspec_contract_validation
from core.appspec import normalization as appspec_normalization
from core.appspec import primitive_inference as appspec_primitive_inference
from core.appspec import prompt_sections as appspec_prompt_sections
from core.appspec import semantic_extractor as appspec_semantic_extractor
from core.palette_engine import execute_palette_prompt
from core.primitives import get_primitive_catalog
from core.provisioning_local import provision_local_instance
from core.db_tenancy import allocate_database
from core.job_pipeline.stage_contracts import build_follow_up, build_stage_output, parse_stage_input
from core.job_pipeline.execution_note_coordinator import (
    begin_stage_note,
    finalize_stage_note,
    record_stage_failure,
    record_stage_metadata,
    resolve_execution_note_artifact_id,
)
from core.generated_artifacts.persistence import (
    link_generated_artifact_memberships as _link_generated_artifact_memberships,
    persist_appspec_artifact as _persist_appspec_artifact,
    persist_generated_json_artifact as _persist_generated_json_artifact,
    persist_policy_artifact as _persist_policy_artifact,
)
from core.generated_artifacts.lifecycle import (
    LEGACY_GENERATED_VERSION,
    GeneratedArtifactIdentity,
    generated_identity as _generated_artifact_identity,
)
from core.policy_bundle import compiler as policy_bundle_compiler
from core.runtime import adapters as runtime_adapters
from core.runtime import deploy_local as runtime_deploy_local
from core.runtime import provision_sibling as runtime_provision_sibling
from core.runtime import smoke_test as runtime_smoke_test
from core.runtime import contract_verification as runtime_contract_verification

POLL_SECONDS = float(os.getenv("XYN_APP_JOB_POLL_SECONDS", "2.0"))
HTTP_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_JOB_HTTP_TIMEOUT", "10"))
APP_DEPLOY_HEALTH_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_DEPLOY_HEALTH_TIMEOUT_SECONDS", "180"))
COMMAND_TIMEOUT_SECONDS = int(os.getenv("XYN_APP_JOB_COMMAND_TIMEOUT_SECONDS", "240"))
APPSPEC_SCHEMA_PATH = Path(__file__).resolve().parent / "contracts" / "appspec_v0.schema.json"
POLICY_BUNDLE_SCHEMA_PATH = Path(__file__).resolve().parent / "contracts" / "policy_bundle_v0.schema.json"
NET_INVENTORY_IMAGE = str(
    os.getenv("XYN_NET_INVENTORY_IMAGE", "public.ecr.aws/i0h0h0n4/xyn/artifacts/net-inventory-api:dev")
).strip()
# Legacy transport/package version remains accepted while revision-based lifecycle
# metadata is rolled out incrementally.
GENERATED_ARTIFACT_VERSION = LEGACY_GENERATED_VERSION
ROOT_PLATFORM_API_CONTAINER = str(os.getenv("XYN_PLATFORM_API_CONTAINER", "xyn-local-api")).strip() or "xyn-local-api"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _safe_slug(value: str, *, default: str = "app") -> str:
    raw = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in str(value or "").lower())
    collapsed = "-".join(part for part in raw.split("-") if part)
    return collapsed or default


def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _prefer_local_platform_images_for_smoke() -> bool:
    # Local app-builder smoke runs should validate against the platform code
    # currently running in this workspace, not a potentially stale :dev image.
    return _as_bool(os.getenv("XYN_APP_SMOKE_PREFER_LOCAL_IMAGES", "true"))


def _workspace_root() -> Path:
    root = Path(
        os.getenv("XYN_WORKSPACE_ROOT")
        or os.getenv("XYN_LOCAL_WORKSPACE_ROOT")
        or os.getenv("XYNSEED_WORKSPACE")
        or default_instance_workspace_root()
    ).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _deployments_root() -> Path:
    root = _workspace_root() / "app_deployments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _generated_artifacts_root() -> Path:
    root = _workspace_root() / "artifacts" / "generated"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _docker_image_exists(image_ref: str) -> bool:
    code, _, _ = _run(["docker", "image", "inspect", str(image_ref or "").strip()])
    return code == 0


def _effective_net_inventory_image() -> str:
    explicit = str(os.getenv("XYN_NET_INVENTORY_IMAGE", "") or "").strip()
    if explicit:
        return explicit
    return NET_INVENTORY_IMAGE


def _generated_artifact_slug(app_slug: str) -> str:
    return f"app.{_safe_slug(app_slug, default='generated-app')}"


def _policy_bundle_slug(app_slug: str) -> str:
    return f"policy.{_safe_slug(app_slug, default='generated-app')}"


def _run(cmd: list[str], *, cwd: Optional[Path] = None) -> tuple[int, str, str]:
    return runtime_adapters.run_command(cmd, cwd=cwd, timeout_seconds=COMMAND_TIMEOUT_SECONDS)


def _container_http_json(
    container_name: str,
    method: str,
    path: str,
    *,
    port: int,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any], str]:
    return runtime_adapters.container_http_json(
        container_name,
        method,
        path,
        port=port,
        payload=payload,
        http_timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )


def _container_http_session_json(
    container_name: str,
    *,
    steps: list[dict[str, Any]],
    port: int,
) -> tuple[int, dict[str, Any], str]:
    return runtime_adapters.container_http_session_json(
        container_name,
        steps=steps,
        port=port,
        http_timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )


def _container_http_session_upload_json(
    container_name: str,
    *,
    port: int,
    upload_path: str,
    file_field: str,
    filename: str,
    file_bytes: bytes,
    extra_form: Optional[dict[str, Any]] = None,
) -> tuple[int, dict[str, Any], str]:
    return runtime_adapters.container_http_session_upload_json(
        container_name,
        port=port,
        upload_path=upload_path,
        file_field=file_field,
        filename=filename,
        file_bytes=file_bytes,
        extra_form=extra_form,
        http_timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )


def _extract_ui_surface_lines(app_spec: dict[str, Any]) -> list[str]:
    ui_text = str(app_spec.get("ui_surfaces") or "").strip()
    if not ui_text:
        structured_plan = app_spec.get("structured_plan") if isinstance(app_spec.get("structured_plan"), dict) else {}
        ui_text = str(structured_plan.get("ui_surfaces") or "").strip()
    lines: list[str] = []
    for raw_line in ui_text.splitlines():
        token = str(raw_line or "").strip().lstrip("-").strip()
        if token:
            lines.append(token)
    return lines


def _is_admin_surface_token(token: str) -> bool:
    lowered = str(token or "").lower()
    admin_keywords = ("admin", "operator", "source", "connector", "mapping", "readiness", "activation", "inspection")
    return any(word in lowered for word in admin_keywords)


def _is_map_surface_token(token: str) -> bool:
    lowered = str(token or "").lower()
    map_keywords = ("map", "rectangle", "box selection", "area selection", "bounding")
    return any(word in lowered for word in map_keywords)


def _infer_admin_surface_required(app_spec: dict[str, Any], ui_lines: list[str]) -> bool:
    if any(_is_admin_surface_token(line) for line in ui_lines):
        return True
    workflow_defs = app_spec.get("workflow_definitions") if isinstance(app_spec.get("workflow_definitions"), list) else []
    for row in workflow_defs:
        if not isinstance(row, dict):
            continue
        joined = " ".join(
            [
                str(row.get("workflow_key") or ""),
                str(row.get("workflow_label") or ""),
                str(row.get("description") or ""),
            ]
        ).lower()
        if "admin" in joined or "operator" in joined or "source" in joined:
            return True
    return False


def _infer_map_surface_required(app_spec: dict[str, Any], ui_lines: list[str]) -> bool:
    if any(_is_map_surface_token(line) for line in ui_lines):
        return True
    requires_primitives = {
        str(value or "").strip().lower()
        for value in (app_spec.get("requires_primitives") if isinstance(app_spec.get("requires_primitives"), list) else [])
        if str(value or "").strip()
    }
    if "geospatial" in requires_primitives:
        return True
    workflow_defs = app_spec.get("workflow_definitions") if isinstance(app_spec.get("workflow_definitions"), list) else []
    for row in workflow_defs:
        if not isinstance(row, dict):
            continue
        description = str(row.get("description") or "").lower()
        if _is_map_surface_token(description):
            return True
    return False


def _build_generated_surface_definitions(*, app_spec: dict[str, Any], capability_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    entities = capability_manifest.get("entities") if isinstance(capability_manifest.get("entities"), list) else []

    def _add_surface(
        *,
        key: str,
        title: str,
        route: str,
        nav_section: str,
        order: int,
        surface_kind: str = "dashboard",
        nav_visibility: str = "always",
        renderer_type: str = "generic_dashboard",
        renderer_payload: dict[str, Any] | None = None,
    ) -> None:
        renderer: dict[str, Any] = {"type": renderer_type}
        if isinstance(renderer_payload, dict) and renderer_payload:
            renderer["payload"] = copy.deepcopy(renderer_payload)
        rows.append(
            {
                "key": key,
                "title": title,
                "route": route,
                "surface_kind": surface_kind,
                "nav_visibility": nav_visibility,
                "nav_section": nav_section,
                "order": order,
                "renderer": renderer,
            }
        )

    for idx, entity in enumerate(entities):
        if not isinstance(entity, dict):
            continue
        entity_key = str(entity.get("key") or "").strip()
        if not entity_key:
            continue
        # Generated artifact surfaces are now intentionally narrow: only campaign
        # workflows have a modern shell mapping contract in this build.
        if entity_key != "campaigns":
            continue
        plural_label = str(entity.get("plural_label") or entity_key).strip() or entity_key
        singular_label = str(entity.get("singular_label") or entity_key.rstrip("s")).strip() or entity_key.rstrip("s")
        section = "manage"
        _add_surface(
            key=f"entity-{entity_key}-list",
            title=plural_label.title(),
            route=f"/app/{entity_key}",
            nav_section=section,
            order=100 + (idx * 10),
            renderer_type="generic_dashboard",
        )
        _add_surface(
            key=f"entity-{entity_key}-detail",
            title=f"{singular_label.title()} Detail",
            route=f"/app/{entity_key}/:id",
            nav_section=section,
            order=102 + (idx * 10),
            nav_visibility="hidden",
            renderer_type="generic_dashboard",
            renderer_payload={
                "shell_renderer_key": "campaign_map_workflow",
                "mode": "detail",
                "campaign_id_param": "id",
            },
        )
        _add_surface(
            key=f"entity-{entity_key}-create",
            title=f"Create {singular_label.title()}",
            route=f"/app/{entity_key}/new",
            nav_section=section,
            order=101 + (idx * 10),
            surface_kind="editor",
            renderer_type="generic_editor",
            renderer_payload={
                "shell_renderer_key": "campaign_map_workflow",
                "mode": "create",
            },
        )
    rows.sort(key=lambda row: (str(row.get("nav_section") or ""), int(row.get("order") or 1000), str(row.get("key") or "")))
    return rows


def _surface_manifest_summary(surface_rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {"nav": [], "manage": [], "docs": []}
    for row in surface_rows:
        if not isinstance(row, dict):
            continue
        section = str(row.get("nav_section") or "manage").strip().lower()
        if section not in {"manage", "admin", "docs"}:
            section = "manage"
        label = str(row.get("title") or "").strip()
        path = str(row.get("route") or "").strip()
        order = int(row.get("order") or 1000)
        if not label or not path:
            continue
        if str(row.get("nav_visibility") or "").strip().lower() == "always":
            grouped["nav"].append(
                {
                    "label": label,
                    "path": path,
                    "order": order,
                    "group": "apps_admin" if section == "admin" else "apps",
                }
            )
        if section in {"manage", "admin"}:
            grouped["manage"].append({"label": label, "path": path, "order": order})
        if section == "docs":
            grouped["docs"].append({"label": label, "path": path, "order": order})
    for section in ("nav", "manage", "docs"):
        grouped[section].sort(key=lambda row: (int(row.get("order") or 1000), str(row.get("label") or "")))
    if not grouped["manage"]:
        grouped["manage"] = [{"label": "Workbench", "path": "/app/workbench", "order": 100}]
    if not grouped["docs"]:
        grouped["docs"] = [{"label": "Workbench", "path": "/app/workbench", "order": 1000}]
    if not grouped["nav"]:
        grouped["nav"] = [{"label": "Workbench", "path": "/app/workbench", "order": 100, "group": "apps"}]
    return grouped


def _build_generated_artifact_manifest(
    *,
    app_spec: dict[str, Any],
    runtime_config: dict[str, Any],
    lifecycle_identity: GeneratedArtifactIdentity | None = None,
) -> dict[str, Any]:
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "generated-app"), default="generated-app")
    artifact_slug = _generated_artifact_slug(app_slug)
    title = str(app_spec.get("title") or app_slug).strip() or app_slug
    capability_manifest = build_resolved_capability_manifest(app_spec)
    suggestions = build_manifest_suggestions(artifact_slug=artifact_slug, manifest=capability_manifest)
    generated_surface_defs = _build_generated_surface_definitions(app_spec=app_spec, capability_manifest=capability_manifest)
    manifest_surfaces = _surface_manifest_summary(generated_surface_defs)
    identity = lifecycle_identity or _generated_artifact_identity(artifact_slug=artifact_slug, version_label="dev")
    return {
        "artifact": {
            "id": artifact_slug,
            "type": "application",
            "slug": artifact_slug,
            "version": GENERATED_ARTIFACT_VERSION,
            "version_label": identity.version_label,
            "revision_id": identity.revision_id,
            "lineage_id": identity.lineage_id,
            "lifecycle_stage": identity.lifecycle_stage,
            "name": title,
            "generated": True,
        },
        "capability": {
            "visibility": "capabilities",
            "category": "application",
            "label": title,
            "description": "Generated application capability installed through the artifact registry.",
            "tags": ["generated", "application", app_slug],
            "order": 120,
        },
        "resolved_capability_manifest": capability_manifest,
        "suggestions": suggestions,
        "surfaces": manifest_surfaces,
        "content": {
            "app_spec": app_spec,
            "runtime_config": runtime_config,
            "resolved_capability_manifest": capability_manifest,
            "generated_surface_definitions": generated_surface_defs,
        },
        "lifecycle": identity.to_metadata(),
    }


def _build_generated_policy_artifact_manifest(
    *,
    app_spec: dict[str, Any],
    policy_bundle: dict[str, Any],
    lifecycle_identity: GeneratedArtifactIdentity | None = None,
) -> dict[str, Any]:
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "generated-app"), default="generated-app")
    artifact_slug = _policy_bundle_slug(app_slug)
    title = str(policy_bundle.get("title") or f"{str(app_spec.get('title') or app_slug).strip() or app_slug} Policy Bundle").strip()
    families = list(policy_bundle.get("policy_families") or [])
    identity = lifecycle_identity or _generated_artifact_identity(
        artifact_slug=_generated_artifact_slug(app_slug),
        version_label="dev",
    )
    return {
        "artifact": {
            "id": artifact_slug,
            "type": "policy_bundle",
            "slug": artifact_slug,
            "version": GENERATED_ARTIFACT_VERSION,
            "version_label": identity.version_label,
            "revision_id": identity.revision_id,
            "lineage_id": identity.lineage_id,
            "lifecycle_stage": identity.lifecycle_stage,
            "name": title,
            "generated": True,
        },
        "capability": {
            "visibility": "contextual",
            "category": "policy",
            "label": title,
            "description": "Generated application policy bundle for future validation, rendering, explanation, and enforcement flows.",
            "tags": ["generated", "policy_bundle", app_slug],
            "order": 140,
        },
        "summary": {
            "app_slug": app_slug,
            "policy_families": families,
            "policy_count": sum(
                len(policy_bundle.get("policies", {}).get(key) or [])
                for key in (
                    "validation_policies",
                    "relation_constraints",
                    "transition_policies",
                    "invariant_policies",
                    "derived_policies",
                    "trigger_policies",
                )
            ),
            "future_capabilities": list((policy_bundle.get("explanation") or {}).get("future_capabilities") or []),
        },
        "content": {
            "policy_bundle": policy_bundle,
            "app_slug": app_slug,
            "generated_artifact_slug": _generated_artifact_slug(app_slug),
        },
        "lifecycle": identity.to_metadata(),
    }


def _package_generated_app(
    *,
    workspace_id: uuid.UUID,
    source_job_id: str,
    app_spec: dict[str, Any],
    policy_bundle: dict[str, Any],
    runtime_config: dict[str, Any],
    lifecycle_identity: GeneratedArtifactIdentity | None = None,
) -> dict[str, Any]:
    app_slug = _safe_slug(str(app_spec.get("app_slug") or "generated-app"), default="generated-app")
    artifact_slug = _generated_artifact_slug(app_slug)
    policy_artifact_slug = _policy_bundle_slug(app_slug)
    package_root = _generated_artifacts_root() / app_slug
    payload_root = package_root / "payload"
    payload_root.mkdir(parents=True, exist_ok=True)

    identity = lifecycle_identity or _generated_artifact_identity(artifact_slug=artifact_slug, version_label="dev")
    artifact_manifest = _build_generated_artifact_manifest(
        app_spec=app_spec,
        runtime_config=runtime_config,
        lifecycle_identity=identity,
    )
    artifact_manifest["content"]["policy_bundle_summary"] = {
        "artifact_slug": policy_artifact_slug,
        "title": str(policy_bundle.get("title") or "").strip(),
        "policy_families": list(policy_bundle.get("policy_families") or []),
    }
    policy_artifact_manifest = _build_generated_policy_artifact_manifest(
        app_spec=app_spec,
        policy_bundle=policy_bundle,
        lifecycle_identity=identity,
    )
    artifact_manifest_path = package_root / "artifact.json"
    app_spec_path = payload_root / "app_spec.json"
    policy_bundle_path = payload_root / "policy_bundle.json"
    runtime_config_path = payload_root / "runtime_config.json"
    artifact_manifest_path.write_text(json.dumps(artifact_manifest, indent=2, sort_keys=True), encoding="utf-8")
    app_spec_path.write_text(json.dumps(app_spec, indent=2, sort_keys=True), encoding="utf-8")
    policy_bundle_path.write_text(json.dumps(policy_bundle, indent=2, sort_keys=True), encoding="utf-8")
    runtime_config_path.write_text(json.dumps(runtime_config, indent=2, sort_keys=True), encoding="utf-8")

    artifact_entry = {
        "type": "application",
        "slug": artifact_slug,
        "version": GENERATED_ARTIFACT_VERSION,
        "version_label": identity.version_label,
        "revision_id": identity.revision_id,
        "lineage_id": identity.lineage_id,
        "artifact_id": artifact_slug,
        "title": str(app_spec.get("title") or app_slug),
        "description": "Generated application artifact package",
        "dependencies": [],
        "bindings": [],
    }
    policy_artifact_entry = {
        "type": "policy_bundle",
        "slug": policy_artifact_slug,
        "version": GENERATED_ARTIFACT_VERSION,
        "version_label": identity.version_label,
        "revision_id": identity.revision_id,
        "lineage_id": identity.lineage_id,
        "artifact_id": policy_artifact_slug,
        "title": str(policy_bundle.get("title") or f"{str(app_spec.get('title') or app_slug).strip() or app_slug} Policy Bundle"),
        "description": "Generated application policy bundle",
        "dependencies": [],
        "bindings": [],
    }
    files: dict[str, bytes] = {}
    base = f"artifacts/application/{artifact_slug}/{GENERATED_ARTIFACT_VERSION}"
    policy_base = f"artifacts/policy_bundle/{policy_artifact_slug}/{GENERATED_ARTIFACT_VERSION}"
    artifact_zip_path = f"{base}/artifact.json"
    payload_zip_path = f"{base}/payload/payload.json"
    surfaces_zip_path = f"{base}/surfaces.json"
    runtime_roles_zip_path = f"{base}/runtime_roles.json"
    policy_artifact_zip_path = f"{policy_base}/artifact.json"
    policy_payload_zip_path = f"{policy_base}/payload/payload.json"
    policy_surfaces_zip_path = f"{policy_base}/surfaces.json"
    policy_runtime_roles_zip_path = f"{policy_base}/runtime_roles.json"
    combined_payload = {
        "app_spec": app_spec,
        "policy_bundle": policy_bundle,
        "runtime_config": runtime_config,
        "generated_artifact_identity": identity.to_metadata(),
        "generated": True,
        "source_job_id": source_job_id,
        "source_workspace_id": str(workspace_id),
    }
    policy_payload = {
        "policy_bundle": policy_bundle,
        "generated_artifact_identity": identity.to_metadata(),
        "generated": True,
        "source_job_id": source_job_id,
        "source_workspace_id": str(workspace_id),
        "app_slug": app_slug,
    }
    files[artifact_zip_path] = json.dumps(artifact_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[payload_zip_path] = json.dumps(combined_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    generated_surface_defs = artifact_manifest.get("content", {}).get("generated_surface_definitions")
    if not isinstance(generated_surface_defs, list):
        generated_surface_defs = []
    files[surfaces_zip_path] = json.dumps(
        generated_surface_defs,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    files[runtime_roles_zip_path] = b"[]"
    files[policy_artifact_zip_path] = json.dumps(policy_artifact_manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[policy_payload_zip_path] = json.dumps(policy_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    files[policy_surfaces_zip_path] = b"[]"
    files[policy_runtime_roles_zip_path] = b"[]"
    manifest = {
        "format_version": 1,
        "package_name": artifact_slug,
        "package_version": GENERATED_ARTIFACT_VERSION,
        "version_label": identity.version_label,
        "revision_id": identity.revision_id,
        "lineage_id": identity.lineage_id,
        "built_at": _iso_now(),
        "platform_compatibility": {"min_version": "1.0.0", "required_features": ["artifact_packages_v1"]},
        "artifacts": [
            {
                **artifact_entry,
                "artifact_hash": hashlib.sha256(files[artifact_zip_path]).hexdigest(),
            },
            {
                **policy_artifact_entry,
                "artifact_hash": hashlib.sha256(files[policy_artifact_zip_path]).hexdigest(),
            },
        ],
        "checksums": {path: hashlib.sha256(content).hexdigest() for path, content in files.items()},
    }
    files["manifest.json"] = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    package_zip_path = package_root / "package.zip"
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files.keys()):
            archive.writestr(path, files[path])
    package_zip_path.write_bytes(blob.getvalue())
    return {
        "artifact_slug": artifact_slug,
        "artifact_version": GENERATED_ARTIFACT_VERSION,
        "revision_id": identity.revision_id,
        "version_label": identity.version_label,
        "lineage_id": identity.lineage_id,
        "lifecycle_stage": identity.lifecycle_stage,
        "legacy_version": identity.legacy_version,
        "policy_bundle_slug": policy_artifact_slug,
        "artifact_manifest_path": str(artifact_manifest_path),
        "artifact_package_path": str(package_zip_path),
        "artifact_dir": str(package_root),
        "runtime_config_path": str(runtime_config_path),
        "app_spec_path": str(app_spec_path),
        "policy_bundle_path": str(policy_bundle_path),
        "package_size_bytes": package_zip_path.stat().st_size,
    }


def _import_generated_artifact_package_into_registry(
    *,
    container_name: str,
    artifact_slug: str,
    package_path: Path,
    port: int = 8000,
    workspace_slug: str = "",
) -> dict[str, Any]:
    if not package_path.exists():
        raise RuntimeError(f"Generated artifact package not found: {package_path}")
    if not artifact_slug.startswith("app."):
        raise RuntimeError(f"Generated artifact slug must use app.* namespace: {artifact_slug}")
    if not _docker_container_running(container_name):
        raise RuntimeError(f"Platform API container is not running: {container_name}")
    workspace_query = str(workspace_slug or "").strip()
    upload_path = "/xyn/api/artifacts/import"
    if workspace_query:
        upload_path = f"{upload_path}?workspace_slug={workspace_query}"
    code, body, text = _container_http_session_upload_json(
        container_name,
        port=port,
        upload_path=upload_path,
        file_field="file",
        filename=package_path.name,
        file_bytes=package_path.read_bytes(),
    )
    if code not in {200, 201}:
        raise RuntimeError(f"Generated artifact import failed ({code}): {text}")
    artifacts = body.get("artifacts") if isinstance(body.get("artifacts"), list) else []
    imported = next((item for item in artifacts if isinstance(item, dict) and str(item.get("slug") or "") == artifact_slug), None)
    if not isinstance(imported, dict):
        raise RuntimeError(f"Generated artifact import response missing slug {artifact_slug}")
    return {
        "status": "imported",
        "package": body.get("package") if isinstance(body.get("package"), dict) else {},
        "receipt": body.get("receipt") if isinstance(body.get("receipt"), dict) else {},
        "artifact": imported,
    }


def _import_generated_artifact_package(
    *,
    artifact_slug: str,
    package_path: Path,
    workspace_slug: str = "",
) -> dict[str, Any]:
    return _import_generated_artifact_package_into_registry(
        container_name=ROOT_PLATFORM_API_CONTAINER,
        artifact_slug=artifact_slug,
        package_path=package_path,
        port=8000,
        workspace_slug=workspace_slug,
    )


def _install_generated_artifact_in_sibling(
    *,
    sibling_api_container: str,
    workspace_slug: str,
    artifact_slug: str,
    artifact_version: str = "",
    artifact_revision_id: str = "",
) -> dict[str, Any]:
    code, body, text = _container_http_session_json(
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
                "path": "/xyn/api/me",
            },
            {
                "method": "GET",
                "path": "/xyn/api/workspaces",
            },
        ],
    )
    if code != 200:
        raise RuntimeError(f"Failed to enumerate sibling workspaces ({code}): {text}")
    rows = body.get("workspaces") if isinstance(body.get("workspaces"), list) else []
    workspace = next((row for row in rows if str(row.get("slug") or "").strip() == workspace_slug), None)
    if not isinstance(workspace, dict):
        raise RuntimeError(f"Sibling workspace with slug '{workspace_slug}' not found")
    workspace_id = str(workspace.get("id") or "").strip()
    if not workspace_id:
        raise RuntimeError("Sibling workspace id missing from workspace list response")

    install_body_payload = {
        "artifact_id": artifact_slug,
        "artifact_version": artifact_version,
        "enabled": True,
    }
    requested_revision_id = str(artifact_revision_id or "").strip()
    used_revision_fallback = False
    if requested_revision_id:
        install_body_payload["artifact_revision_id"] = requested_revision_id
    install_code, install_body, install_text = _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "POST",
                "path": f"/xyn/api/workspaces/{workspace_id}/artifacts",
                "body": install_body_payload,
            },
        ],
    )
    if install_code not in {200, 201} and requested_revision_id:
        used_revision_fallback = True
        install_code, install_body, install_text = _container_http_session_json(
            sibling_api_container,
            port=8000,
            steps=[
                {
                    "method": "POST",
                    "path": "/auth/dev-login",
                    "form": {"appId": "xyn-ui", "returnTo": "/app"},
                },
                {
                    "method": "POST",
                    "path": f"/xyn/api/workspaces/{workspace_id}/artifacts",
                    "body": {
                        "artifact_id": artifact_slug,
                        "artifact_version": artifact_version,
                        "enabled": True,
                    },
                },
            ],
        )
    if install_code not in {200, 201}:
        raise RuntimeError(f"Failed to install sibling artifact '{artifact_slug}' ({install_code}): {install_text}")
    artifact = install_body.get("artifact") if isinstance(install_body.get("artifact"), dict) else {}
    resolved_revision_id = str(
        artifact.get("artifact_revision_id")
        or (artifact.get("metadata") or {}).get("artifact_revision_id")
        or (artifact.get("metadata") or {}).get("revision_id")
        or requested_revision_id
        or ""
    ).strip()
    return {
        "workspace_id": workspace_id,
        "workspace_slug": workspace_slug,
        "artifact_slug": str(artifact.get("slug") or artifact_slug),
        "artifact_id": str(artifact.get("artifact_id") or ""),
        "binding_id": str(artifact.get("binding_id") or ""),
        "artifact_revision_id": resolved_revision_id,
        "artifact_version": str(artifact.get("package_version") or artifact_version or GENERATED_ARTIFACT_VERSION),
        "artifact_version_label": str((artifact.get("metadata") or {}).get("version_label") or "").strip(),
        "revision_fallback_used": used_revision_fallback,
    }


def _register_sibling_runtime_target(
    *,
    sibling_api_container: str,
    workspace_id: str,
    app_slug: str,
    artifact_slug: str,
    title: str,
    runtime_target: dict[str, Any],
    sibling_ui_url: str = "",
    sibling_api_url: str = "",
) -> dict[str, Any]:
    sibling_ui = str(sibling_ui_url or "").strip()
    sibling_api = str(sibling_api_url or "").strip()
    registration_body: dict[str, Any] = {
        "app_slug": app_slug,
        "artifact_slug": artifact_slug,
        "title": title,
        "runtime_target": runtime_target,
    }
    if sibling_ui:
        registration_body["sibling_ui_url"] = sibling_ui
    if sibling_api:
        registration_body["sibling_api_url"] = sibling_api
    register_code, register_body, register_text = _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "POST",
                "path": f"/xyn/api/workspaces/{workspace_id}/app-runtime-targets",
                "body": registration_body,
            },
        ],
    )
    if register_code not in {200, 201}:
        raise RuntimeError(f"Failed to register sibling runtime target ({register_code}): {register_text}")
    return register_body if isinstance(register_body, dict) else {}


def _find_revision_sibling_target(
    db: Session,
    *,
    root_workspace_id: uuid.UUID,
    revision_anchor: dict[str, Any],
    app_slug: str,
) -> Optional[dict[str, Any]]:
    anchor_workspace_id = str(revision_anchor.get("workspace_id") or "").strip()
    anchor_instance_id = str(revision_anchor.get("workspace_app_instance_id") or "").strip()
    anchor_artifact_slug = str(revision_anchor.get("artifact_slug") or "").strip()
    if not anchor_workspace_id or not anchor_artifact_slug:
        return None

    candidates = (
        db.query(Job)
        .filter(
            Job.workspace_id == root_workspace_id,
            Job.type == "provision_sibling_xyn",
            Job.status == JobStatus.SUCCEEDED.value,
        )
        .order_by(Job.updated_at.desc())
        .all()
    )
    for candidate in candidates:
        output = candidate.output_json if isinstance(candidate.output_json, dict) else {}
        installed_artifact = output.get("installed_artifact") if isinstance(output.get("installed_artifact"), dict) else {}
        runtime_registration = output.get("runtime_registration") if isinstance(output.get("runtime_registration"), dict) else {}
        runtime_instance = runtime_registration.get("instance") if isinstance(runtime_registration.get("instance"), dict) else {}
        runtime_target = output.get("runtime_target") if isinstance(output.get("runtime_target"), dict) else {}
        sibling_compose_project = str(output.get("compose_project") or "").strip()
        sibling_ui_url = str(output.get("ui_url") or "").strip()
        sibling_api_url = str(output.get("api_url") or "").strip()
        installed_workspace_id = str(installed_artifact.get("workspace_id") or "").strip()
        installed_artifact_slug = str(installed_artifact.get("artifact_slug") or "").strip()
        runtime_app_slug = str(runtime_target.get("app_slug") or "").strip()
        runtime_instance_id = str(runtime_instance.get("id") or "").strip()
        if installed_workspace_id != anchor_workspace_id:
            continue
        if installed_artifact_slug != anchor_artifact_slug:
            continue
        if runtime_app_slug and runtime_app_slug != app_slug:
            continue
        if anchor_instance_id and runtime_instance_id and runtime_instance_id != anchor_instance_id:
            continue
        if not sibling_compose_project or not sibling_ui_url or not sibling_api_url:
            continue
        return {
            "deployment_id": str(output.get("deployment_id") or ""),
            "compose_project": sibling_compose_project,
            "ui_url": sibling_ui_url,
            "api_url": sibling_api_url,
            "installed_artifact": installed_artifact,
            "runtime_target": runtime_target,
            "runtime_registration": runtime_registration,
        }
    return None


def _execute_sibling_palette_prompt(
    *,
    sibling_api_container: str,
    workspace_slug: str,
    prompt: str,
) -> tuple[int, dict[str, Any], str]:
    return _container_http_session_json(
        sibling_api_container,
        port=8000,
        steps=[
            {
                "method": "POST",
                "path": "/auth/dev-login",
                "form": {"appId": "xyn-ui", "returnTo": "/app"},
            },
            {
                "method": "POST",
                "path": f"/xyn/api/palette/execute?workspace_slug={workspace_slug}",
                "body": {"prompt": prompt, "workspace_slug": workspace_slug},
            },
        ],
    )


def _wait_for_container_http_ok(container_name: str, path: str, *, port: int, timeout_seconds: int = 60) -> bool:
    return runtime_adapters.wait_for_container_http_ok(
        container_name,
        path,
        port=port,
        timeout_seconds=timeout_seconds,
        http_timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )


def _append_job_log(log_lines: list[str], message: str) -> None:
    log_lines.append(f"[{_iso_now()}] {message}")


def _load_appspec_schema() -> dict[str, Any]:
    return json.loads(APPSPEC_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_policy_bundle_schema() -> dict[str, Any]:
    return json.loads(POLICY_BUNDLE_SCHEMA_PATH.read_text(encoding="utf-8"))


def _persist_json_artifact(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    name: str,
    kind: str,
    payload: dict[str, Any],
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    return _persist_generated_json_artifact(
        db,
        workspace_id=workspace_id,
        name=name,
        kind=kind,
        payload=payload,
        metadata=metadata,
        workspace_root_factory=_workspace_root,
        now_fn=_utc_now,
    )


def _normalize_unique_strings(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


# Policy bundle compilation extraction compatibility shim:
# Preserve existing private symbol name for callers/tests while delegating
# deterministic policy compilation to core.policy_bundle.compiler.
def _build_policy_bundle(
    *,
    workspace_id: uuid.UUID,
    app_spec: dict[str, Any],
    raw_prompt: str,
) -> dict[str, Any]:
    return policy_bundle_compiler.build_policy_bundle(
        workspace_id=workspace_id,
        app_spec=app_spec,
        raw_prompt=raw_prompt,
    )


def _title_case_words(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[\s_]+", str(value or "").strip()) if part)


def _pluralize_label(value: str) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    if not lower:
        return "records"
    if lower.endswith("y") and lower[-2:] not in {"ay", "ey", "iy", "oy", "uy"}:
        return f"{text[:-1]}ies"
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return f"{text}es"
    return f"{text}s"


def _extract_objective_sections(objective: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {
        "core_entities": [],
        "behavior": [],
        "views": [],
        "validation": [],
    }
    text = re.sub(r"\s+", " ", str(objective or "")).strip()
    if not text:
        return sections
    section_patterns = {
        "core_entities": re.compile(
            r"core entities\s*:\s*(.*?)(?=\bbehavior\s*:|\bviews\s*/\s*usability\s*:|\bviews\s*:|\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "behavior": re.compile(
            r"behavior\s*:\s*(.*?)(?=\bviews\s*/\s*usability\s*:|\bviews\s*:|\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "views": re.compile(
            r"(?:views\s*/\s*usability|views)\s*:\s*(.*?)(?=\bvalidation\s*/\s*rules\s*:|\bvalidation\s*:|$)",
            re.IGNORECASE,
        ),
        "validation": re.compile(r"(?:validation\s*/\s*rules|validation)\s*:\s*(.*)$", re.IGNORECASE),
    }
    for section_name, pattern in section_patterns.items():
        match = pattern.search(text)
        if not match:
            continue
        body = str(match.group(1) or "").strip()
        if not body:
            continue
        if section_name == "core_entities":
            sections[section_name].extend(part.strip() for part in re.split(r"\s+(?=\d+\.)", body) if part.strip())
            continue
        sections[section_name].extend(
            re.sub(r"^\s*[-*]\s*", "", part).strip()
            for part in re.split(r"\s+-\s+", body)
            if re.sub(r"^\s*[-*]\s*", "", part).strip()
        )
    return sections


def _extract_app_name_from_prompt(raw_prompt: str, *, fallback: str) -> str:
    text = str(raw_prompt or "").strip()
    patterns = [
        re.compile(r'called\s+[“"]([^”"]+)[”"]', re.IGNORECASE),
        re.compile(r'build\s+(?:a|an)\s+.*?\s+called\s+([^.;]+)', re.IGNORECASE),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            value = str(match.group(1) or "").strip().strip(".")
            if value:
                return value
    return str(fallback or "").strip() or "Generated App"


def _extract_objective_entities(raw_prompt: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in _extract_objective_sections(raw_prompt).get("core_entities", []):
        cleaned_line = re.sub(r"^\s*\d+\.\s*", "", line).strip()
        if not cleaned_line:
            continue
        parts = [part.strip() for part in re.split(r"\s+-\s+", cleaned_line) if part.strip()]
        if not parts:
            continue
        label = _title_case_words(parts[0])
        fields = [part.strip() for part in parts[1:] if part.strip()]
        if label:
            rows.append({"label": label, "fields": fields})
    return rows


def _field_options_from_token(token: str) -> list[str]:
    match = re.search(r"\(([^)]+)\)", token)
    if not match:
        return []
    return _normalize_unique_strings(
        part.strip()
        for part in re.split(r"[,/]|\\bor\\b", str(match.group(1) or ""), flags=re.IGNORECASE)
    )


def _sanitize_field_label(token: str) -> str:
    cleaned = re.sub(r"\([^)]*\)", "", str(token or "")).strip()
    return cleaned


def _field_key(token: str) -> str:
    return _safe_slug(str(token or "").replace("/", " ").replace("-", " "), default="field").replace("-", "_")


def _field_type_for_token(token: str, *, options: list[str]) -> str:
    key = _field_key(token)
    if key.endswith("_id"):
        return "uuid"
    if key in {"created_at", "updated_at", "poll_date", "date"}:
        return "datetime" if key in {"created_at", "updated_at"} else "string"
    if options and {item.casefold() for item in options} <= {"yes", "no", "true", "false"}:
        return "string"
    return "string"


def _build_entity_contracts_from_prompt(raw_prompt: str) -> list[dict[str, Any]]:
    entity_rows = _extract_objective_entities(raw_prompt)
    if not entity_rows:
        return []

    singular_index: dict[str, tuple[str, str]] = {}
    for row in entity_rows:
        label = str(row["label"])
        singular_label = label.lower()
        plural_label = _pluralize_label(singular_label)
        entity_key = _safe_slug(plural_label, default="records").replace("-", "_")
        singular_index[_safe_slug(singular_label, default="record").replace("-", "_")] = (entity_key, singular_label)

    contracts: list[dict[str, Any]] = []
    for row in entity_rows:
        label = str(row["label"])
        singular_label = label.lower()
        plural_label = _pluralize_label(singular_label)
        entity_key = _safe_slug(plural_label, default="records").replace("-", "_")
        field_rows: list[dict[str, Any]] = [
            {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
            {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
        ]
        seen_field_names = {"id", "workspace_id"}
        relationships: list[dict[str, Any]] = []
        required_on_create: list[str] = ["workspace_id"]
        allowed_on_update: list[str] = []

        raw_fields = row.get("fields") if isinstance(row.get("fields"), list) else []
        for token in raw_fields:
            cleaned = _sanitize_field_label(str(token))
            options = _field_options_from_token(str(token))
            normalized = _field_key(cleaned)
            relation_target = singular_index.get(normalized)
            if relation_target:
                field_name = f"{normalized}_id"
                relation = {
                    "target_entity": relation_target[0],
                    "target_field": "id",
                    "relation_kind": "belongs_to",
                }
                field_rows.append(
                    {
                        "name": field_name,
                        "type": "uuid",
                        "required": True,
                        "readable": True,
                        "writable": True,
                        "identity": False,
                        "relation": relation,
                    }
                )
                seen_field_names.add(field_name)
                relationships.append(
                    {
                        "field": field_name,
                        "target_entity": relation_target[0],
                        "target_field": "id",
                        "relation_kind": "belongs_to",
                        "required": True,
                    }
                )
                required_on_create.append(field_name)
                allowed_on_update.append(field_name)
                continue

            field_name = normalized
            if field_name in seen_field_names:
                continue
            field: dict[str, Any] = {
                "name": field_name,
                "type": _field_type_for_token(field_name, options=options),
                "required": field_name not in {"notes"},
                "readable": True,
                "writable": field_name not in {"created_at", "updated_at"},
                "identity": field_name in {"name", "title", "voter_name"},
            }
            if options:
                field["options"] = options
            field_rows.append(field)
            seen_field_names.add(field_name)
            if field["writable"]:
                allowed_on_update.append(field_name)
            if field["required"] and field["writable"]:
                required_on_create.append(field_name)

        for standard_field in (
            {"name": "created_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
            {"name": "updated_at", "type": "datetime", "required": True, "readable": True, "writable": False, "identity": False},
        ):
            if standard_field["name"] in seen_field_names:
                continue
            field_rows.append(standard_field)
            seen_field_names.add(standard_field["name"])
        title_field = next(
            (
                candidate
                for candidate in ("title", "name", "voter_name")
                if any(str(field.get("name") or "") == candidate for field in field_rows)
            ),
            "id",
        )
        default_list_fields = [name for name in (title_field, "status", "poll_id", "lunch_option_id") if any(str(field.get("name") or "") == name for field in field_rows)]
        if not default_list_fields:
            default_list_fields = [str(field.get("name") or "") for field in field_rows if str(field.get("name") or "") not in {"id", "workspace_id", "created_at", "updated_at"}][:4]
        default_detail_fields = ["id", title_field]
        for name in [str(field.get("name") or "") for field in field_rows]:
            if name and name not in default_detail_fields and name not in {"updated_at"}:
                default_detail_fields.append(name)
        contracts.append(
            {
                "key": entity_key,
                "singular_label": singular_label,
                "plural_label": plural_label,
                "collection_path": f"/{entity_key}",
                "item_path_template": f"/{entity_key}" + "/{id}",
                "operations": {
                    "list": {"declared": True, "method": "GET", "path": f"/{entity_key}"},
                    "get": {"declared": True, "method": "GET", "path": f"/{entity_key}" + "/{id}"},
                    "create": {"declared": True, "method": "POST", "path": f"/{entity_key}"},
                    "update": {"declared": True, "method": "PATCH", "path": f"/{entity_key}" + "/{id}"},
                    "delete": {"declared": True, "method": "DELETE", "path": f"/{entity_key}" + "/{id}"},
                },
                "fields": field_rows,
                "presentation": {
                    "default_list_fields": _normalize_unique_strings(default_list_fields),
                    "default_detail_fields": _normalize_unique_strings(default_detail_fields),
                    "title_field": title_field,
                },
                "validation": {
                    "required_on_create": _normalize_unique_strings(required_on_create),
                    "allowed_on_update": _normalize_unique_strings(allowed_on_update),
                },
                "relationships": relationships,
            }
        )
    return _augment_contracts_with_inferred_selection_flags(raw_prompt=raw_prompt, contracts=contracts)


def _infer_entities_from_prompt(raw_prompt: str) -> list[str]:
    structured_contracts = _build_entity_contracts_from_prompt(raw_prompt)
    if structured_contracts:
        return [str(row.get("key") or "").strip() for row in structured_contracts if str(row.get("key") or "").strip()]
    prompt = str(raw_prompt or "").lower()
    entity_map = {
        "devices": ("device", "devices"),
        "locations": ("location", "locations", "site", "sites", "rack", "racks", "room", "rooms"),
        "interfaces": ("interface", "interfaces"),
        "ip_addresses": ("ip address", "ip addresses", "ip_address", "ip_addresses"),
        "vlans": ("vlan", "vlans"),
    }
    entities: list[str] = []
    for slug, tokens in entity_map.items():
        if any(token in prompt for token in tokens):
            entities.append(slug)
    if "devices" not in entities and any(token in prompt for token in ("inventory", "network")):
        entities.append("devices")
    return _normalize_unique_strings(entities)


def _infer_requested_visuals_from_prompt(raw_prompt: str) -> list[str]:
    prompt = str(raw_prompt or "").lower()
    visuals: list[str] = []
    if any(token in prompt for token in ("chart", "report")) and "devices" in prompt and "status" in prompt:
        visuals.append("devices_by_status_chart")
    if any(token in prompt for token in ("chart", "report")) and "interfaces" in prompt and "status" in prompt:
        visuals.append("interfaces_by_status_chart")
    return _normalize_unique_strings(visuals)


def _extract_prompt_sections(raw_prompt: str) -> dict[str, str]:
    text = str(raw_prompt or "").replace("\r\n", "\n")
    if not text.strip():
        return {}
    lines = text.split("\n")
    sections: dict[str, list[str]] = {}
    current_heading = "__preamble__"
    sections[current_heading] = []
    heading_re = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
    label_re = re.compile(r"^\s*([A-Z][A-Za-z0-9 /()'\"_-]{2,})\s*:\s*$")
    for line in lines:
        heading_match = heading_re.match(line)
        if heading_match:
            current_heading = str(heading_match.group(1) or "").strip().lower()
            sections.setdefault(current_heading, [])
            continue
        label_match = label_re.match(line)
        if label_match and not line.strip().startswith("-"):
            current_heading = str(label_match.group(1) or "").strip().lower()
            sections.setdefault(current_heading, [])
            continue
        sections.setdefault(current_heading, []).append(line)
    cleaned: dict[str, str] = {}
    for heading, body_lines in sections.items():
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        cleaned[str(heading or "").strip().lower()] = body
    return cleaned


def _pick_prompt_section(sections: dict[str, str], *candidates: str) -> str:
    for candidate in candidates:
        token = str(candidate or "").strip().lower()
        if not token:
            continue
        for key, value in sections.items():
            if token in key and str(value or "").strip():
                return str(value).strip()
    return ""


def _contains_phrase(text: str, *phrases: str) -> bool:
    corpus = str(text or "").lower()
    return any(str(phrase or "").lower() in corpus for phrase in phrases if str(phrase or "").strip())


def _infer_primitives_from_text(text: str) -> list[str]:
    prompt = str(text or "").lower()
    primitives: list[str] = []
    primitive_keyword_map: list[tuple[str, tuple[str, ...]]] = [
        ("access_control", ("authorization", "access control", "capability enforcement", "role-based access")),
        ("lifecycle", ("lifecycle", "transition", "stateful control")),
        ("orchestration", ("orchestration", "scheduling", "job execution", "pipeline")),
        ("run_history", ("run history", "operational records", "job history")),
        ("source_connector", ("source connector", "source connector / import", "source import", "ingestion")),
        ("source_governance", ("source governance", "readiness", "activation")),
        ("artifact_storage", ("artifact persistence", "raw artifact", "snapshot artifact")),
        ("changed_data_publication", ("changed-data publication", "reconciled-state", "reconciled property-level state")),
        ("provenance_audit", ("provenance", "audit", "lineage")),
        ("geospatial", ("postgis", "geospatial", "map", "spatial")),
        ("parcel_identity", ("parcel identity", "crosswalk", "canonical parcel", "handle")),
        ("matching", ("matching", "match evaluation")),
        ("watch_subscription", ("watch", "subscription", "campaign")),
        ("notifications", ("notification", "signal feed", "signal list")),
    ]
    for primitive, keywords in primitive_keyword_map:
        if any(keyword in prompt for keyword in keywords):
            primitives.append(primitive)
    if _contains_phrase(prompt, "location", "locations", "address", "site", "building", "room"):
        primitives.append("location")
    return _normalize_unique_strings(primitives)


def _extract_workflow_blocks_from_prompt(raw_prompt: str) -> list[dict[str, Any]]:
    sections = _extract_prompt_sections(raw_prompt)
    blocks: list[dict[str, Any]] = []
    for heading, body in sections.items():
        if "workflow" not in heading:
            continue
        workflow_key = _safe_slug(heading.replace("workflow", "").strip() or heading, default="workflow")
        blocks.append(
            {
                "workflow_key": workflow_key,
                "workflow_label": _title_case_words(heading.replace("workflow", "").strip() or heading),
                "description": str(body).strip(),
                "requires_primitives": _infer_primitives_from_text(body),
            }
        )
    return blocks


def _build_structured_plan_snapshot(raw_prompt: str) -> dict[str, Any]:
    sections = _extract_prompt_sections(raw_prompt)
    workflow_blocks = _extract_workflow_blocks_from_prompt(raw_prompt)
    snapshot = {
        "application_overview": _pick_prompt_section(sections, "application overview", "purpose"),
        "domain_model": _pick_prompt_section(sections, "domain model", "property model", "signal model"),
        "workflow_definitions": workflow_blocks,
        "platform_primitive_composition": [
            {
                "workflow_key": str(item.get("workflow_key") or ""),
                "workflow_label": str(item.get("workflow_label") or ""),
                "requires_primitives": _normalize_unique_strings(item.get("requires_primitives") if isinstance(item.get("requires_primitives"), list) else []),
            }
            for item in workflow_blocks
            if isinstance(item, dict)
        ],
        "evaluation_semantics": _pick_prompt_section(sections, "evaluation semantics", "changed-data and evaluation semantics"),
        "admin_user_separation": _pick_prompt_section(sections, "admin vs user separation", "role separation", "admin/operator workflow", "end-user workflow"),
        "ui_surfaces": _pick_prompt_section(sections, "ui surface", "ui expectations", "mvp ui", "ui with at least"),
        "configurability": _pick_prompt_section(sections, "configurability", "campaign constraints"),
        "explicit_exclusions": _pick_prompt_section(sections, "explicit exclusions"),
    }
    if not any(str(value or "").strip() for key, value in snapshot.items() if key != "workflow_definitions" and key != "platform_primitive_composition") and not workflow_blocks:
        return {}
    return snapshot


def _infer_entities_from_app_spec(app_spec: dict[str, Any]) -> list[str]:
    contract_rows = app_spec.get("entity_contracts") if isinstance(app_spec.get("entity_contracts"), list) else []
    contract_keys = _normalize_unique_strings(
        [str(row.get("key") or "").strip() for row in contract_rows if isinstance(row, dict)]
    )
    if contract_keys:
        return contract_keys
    entities = _normalize_unique_strings(app_spec.get("entities") if isinstance(app_spec.get("entities"), list) else [])
    if entities:
        return entities
    inferred: list[str] = []
    service_names = {
        str(service.get("name") or "").strip().lower()
        for service in app_spec.get("services", [])
        if isinstance(service, dict)
    }
    if "net-inventory-api" in service_names:
        inferred.extend(["devices", "locations"])
    reports = _normalize_unique_strings(app_spec.get("reports") if isinstance(app_spec.get("reports"), list) else [])
    if any(report == "interfaces_by_status" for report in reports):
        inferred.append("interfaces")
    source_prompt = str(app_spec.get("source_prompt") or "")
    inferred.extend(_infer_entities_from_prompt(source_prompt))
    return _normalize_unique_strings(inferred)


def _score_prompt_structure(raw_prompt: str) -> float:
    sections = appspec_prompt_sections._extract_objective_sections(raw_prompt)
    prompt_sections = appspec_prompt_sections._extract_prompt_sections(raw_prompt)
    objective_entities = appspec_entity_inference._extract_objective_entities(raw_prompt)
    contracts = appspec_entity_inference._build_entity_contracts_from_prompt(raw_prompt)
    section_presence = sum(
        1
        for key in ("core_entities", "behavior", "views", "validation")
        if isinstance(sections.get(key), list) and len(sections.get(key) or []) > 0
    )
    heading_presence = len(prompt_sections)
    field_total = 0
    for row in objective_entities:
        fields = row.get("fields") if isinstance(row.get("fields"), list) else []
        field_total += len(fields)
    score = 0.0
    if section_presence >= 2:
        score += 0.4
    elif section_presence == 1:
        score += 0.2
    if heading_presence >= 3:
        score += 0.2
    elif heading_presence >= 1:
        score += 0.1
    if len(objective_entities) >= 3 or len(contracts) >= 3:
        score += 0.25
    elif objective_entities or contracts:
        score += 0.15
    if field_total >= 4:
        score += 0.15
    elif field_total > 0:
        score += 0.05
    return max(0.0, min(1.0, score))


def _build_app_spec_with_diagnostics(
    *,
    workspace_id: uuid.UUID,
    title: str,
    raw_prompt: str,
    initial_intent: Optional[dict[str, Any]] = None,
    current_app_spec: Optional[dict[str, Any]] = None,
    current_app_summary: Optional[dict[str, Any]] = None,
    revision_anchor: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = raw_prompt.lower()
    mentions_inventory = any(token in prompt for token in ("inventory", "device", "devices", "network"))
    base_spec = copy.deepcopy(current_app_spec) if isinstance(current_app_spec, dict) else {
        "schema_version": "xyn.appspec.v0",
        "ingress": {"enabled": False},
        "data": {"postgres": {"required": True}},
        "reports": [],
    }

    extracted_title = appspec_entity_inference._extract_app_name_from_prompt(
        raw_prompt,
        fallback=title or str(base_spec.get("title") or "Generated App"),
    )
    app_slug = str(base_spec.get("app_slug") or "").strip() or (
        "net-inventory" if mentions_inventory else _safe_slug(extracted_title, default=_safe_slug(title, default="generated-app"))
    )
    app_title = str(extracted_title or title or base_spec.get("title") or "Generated App").strip() or "Generated App"
    db_name = _safe_slug(app_slug, default="generated-app").replace("-", "_")
    app_service_name = f"{app_slug}-api"
    db_service_name = f"{app_slug}-db"
    requested_entities = appspec_normalization._normalize_unique_strings(
        (
            (initial_intent or {}).get("requested_entities")
            if isinstance((initial_intent or {}).get("requested_entities"), list)
            else []
        )
    )
    requested_visuals = appspec_normalization._normalize_unique_strings(
        (
            (initial_intent or {}).get("requested_visuals")
            if isinstance((initial_intent or {}).get("requested_visuals"), list)
            else []
        )
    )
    structure_score = _score_prompt_structure(raw_prompt)
    route = "A" if structure_score >= 0.8 else "B" if structure_score >= 0.4 else "C"
    semantic_used = route in {"B", "C"}
    semantic_payload: dict[str, Any] = {"entities": [], "entity_contracts": [], "requested_visuals": []}
    semantic_diagnostics: dict[str, Any] = {
        "llm_used": False,
        "fallback_used": False,
        "repair_used": False,
    }
    if semantic_used:
        semantic_payload, semantic_diagnostics = appspec_semantic_extractor.extract_semantic_inference_with_diagnostics(
            raw_prompt,
            prefer_llm=(route in {"B", "C"}),
        )
    inferred_entities = appspec_entity_inference._infer_entities_from_prompt(raw_prompt)
    inferred_visuals = appspec_entity_inference._infer_requested_visuals_from_prompt(raw_prompt)
    semantic_entities = appspec_normalization._normalize_unique_strings(
        semantic_payload.get("entities") if isinstance(semantic_payload.get("entities"), list) else []
    )
    semantic_visuals = appspec_normalization._normalize_unique_strings(
        semantic_payload.get("requested_visuals") if isinstance(semantic_payload.get("requested_visuals"), list) else []
    )
    semantic_contracts = [
        row
        for row in (semantic_payload.get("entity_contracts") if isinstance(semantic_payload.get("entity_contracts"), list) else [])
        if isinstance(row, dict)
    ]
    existing_entities = appspec_entity_inference._infer_entities_from_app_spec(base_spec)
    summary_entities = appspec_normalization._normalize_unique_strings(
        (
            (current_app_summary or {}).get("entities")
            if isinstance((current_app_summary or {}).get("entities"), list)
            else []
        )
    )
    deterministic_contracts = appspec_entity_inference._build_entity_contracts_from_prompt(raw_prompt)
    if route == "A":
        generated_contracts = deterministic_contracts
    elif route == "B":
        generated_contracts = deterministic_contracts or semantic_contracts
    else:
        generated_contracts = semantic_contracts or deterministic_contracts
    current_contracts = (
        copy.deepcopy(base_spec.get("entity_contracts"))
        if isinstance(base_spec.get("entity_contracts"), list)
        else []
    )
    merged_contract_candidates = copy.deepcopy(current_contracts or generated_contracts)
    primitive_candidates = appspec_normalization._normalize_unique_strings(
        base_spec.get("requires_primitives") if isinstance(base_spec.get("requires_primitives"), list) else []
    )
    primitive_candidates.extend(appspec_primitive_inference._infer_primitives_from_text(raw_prompt))
    interpretation = appspec_canonicalize.canonicalize_interpretation(
        route=route,
        existing_entities=existing_entities,
        summary_entities=summary_entities,
        requested_entities=requested_entities,
        deterministic_entities=inferred_entities,
        semantic_entities=semantic_entities,
        deterministic_contracts=deterministic_contracts,
        semantic_contracts=semantic_contracts,
        requested_visuals=requested_visuals,
        deterministic_visuals=inferred_visuals,
        semantic_visuals=semantic_visuals,
        primitive_keys=primitive_candidates,
    )
    consistency_result = appspec_consistency.validate_interpretation_consistency(interpretation)
    interpretation = consistency_result.interpretation
    entity_contracts = [copy.deepcopy(row.contract) for row in interpretation.entity_contracts]
    if not entity_contracts and merged_contract_candidates:
        entity_contracts = [copy.deepcopy(row) for row in merged_contract_candidates if isinstance(row, dict)]
    contract_validation = appspec_contract_validation.validate_and_normalize_entity_contracts(entity_contracts)
    entity_contracts = contract_validation.contracts
    entities = appspec_normalization._normalize_unique_strings([row.key for row in interpretation.entities])
    if not entities:
        raise RuntimeError(
            "AppSpec generation could not derive any entity contracts from the request. "
            "The generic builder must not silently fall back to inventory semantics."
        )

    existing_reports = appspec_normalization._normalize_unique_strings(
        base_spec.get("reports") if isinstance(base_spec.get("reports"), list) else []
    )
    reports = existing_reports[:]
    visuals = appspec_normalization._normalize_unique_strings(
        appspec_normalization._normalize_unique_strings(
            base_spec.get("requested_visuals") if isinstance(base_spec.get("requested_visuals"), list) else []
        )
        + [row.key for row in interpretation.visuals]
    )
    if not entity_contracts and "devices" in entities and "devices_by_status_chart" not in visuals and "devices_by_status" not in reports:
        visuals.append("devices_by_status_chart")
    visual_report_map = {
        "devices_by_status_chart": "devices_by_status",
        "interfaces_by_status_chart": "interfaces_by_status",
    }
    for visual in visuals:
        report = visual_report_map.get(visual)
        if report and report not in reports:
            reports.append(report)

    requires_primitives = appspec_normalization._normalize_unique_strings(
        [row.key for row in interpretation.primitives]
    )
    if "locations" in entities and "location" not in requires_primitives:
        requires_primitives.append("location")
    structured_plan = appspec_prompt_sections._build_structured_plan_snapshot(raw_prompt)

    phase_1_scope = appspec_normalization._normalize_unique_strings(
        (
            (initial_intent or {}).get("phase_1_scope")
            if isinstance((initial_intent or {}).get("phase_1_scope"), list)
            else []
        )
    )
    if not phase_1_scope:
        phase_1_scope = entities[:]

    spec = copy.deepcopy(base_spec)
    spec["schema_version"] = "xyn.appspec.v0"
    spec["app_slug"] = app_slug
    spec["title"] = app_title
    spec["workspace_id"] = str(workspace_id)
    spec["source_prompt"] = raw_prompt
    spec["purpose"] = str(raw_prompt or "").strip()
    spec["entities"] = entities
    spec["phase_1_scope"] = phase_1_scope
    spec["requested_visuals"] = visuals
    spec["reports"] = reports
    if entity_contracts:
        spec["entity_contracts"] = entity_contracts
    if structured_plan:
        spec["structured_plan"] = structured_plan
        if structured_plan.get("workflow_definitions"):
            spec["workflow_definitions"] = copy.deepcopy(structured_plan.get("workflow_definitions"))
        if structured_plan.get("platform_primitive_composition"):
            spec["platform_primitive_composition"] = copy.deepcopy(structured_plan.get("platform_primitive_composition"))
        if str(structured_plan.get("ui_surfaces") or "").strip():
            spec["ui_surfaces"] = str(structured_plan.get("ui_surfaces") or "").strip()
        if str(structured_plan.get("domain_model") or "").strip():
            spec["domain_model"] = str(structured_plan.get("domain_model") or "").strip()
    spec["services"] = [
        {
            "name": app_service_name,
            "image": _effective_net_inventory_image(),
            "env": {
                "PORT": "8080",
                "SERVICE_NAME": app_service_name,
                "APP_TITLE": app_title,
                "DATABASE_URL": f"postgresql://xyn:xyn_dev_password@{db_service_name}:5432/{db_name}",
            },
            "ports": [{"container": 8080, "host": 0, "protocol": "tcp"}],
            "depends_on": [db_service_name],
        },
        {
            "name": db_service_name,
            "image": "postgres:16-alpine",
            "env": {
                "POSTGRES_DB": db_name,
                "POSTGRES_USER": "xyn",
                "POSTGRES_PASSWORD": "xyn_dev_password",
            },
            "ports": [{"container": 5432, "host": 0, "protocol": "tcp"}],
            "depends_on": [],
        },
    ]
    spec.setdefault("data", {})
    if not isinstance(spec.get("data"), dict):
        spec["data"] = {}
    spec["data"].setdefault("postgres", {})
    if not isinstance(spec["data"].get("postgres"), dict):
        spec["data"]["postgres"] = {}
    spec["data"]["postgres"]["required"] = True
    spec["data"]["postgres"]["service"] = db_service_name
    if requires_primitives:
        spec["requires_primitives"] = appspec_normalization._normalize_unique_strings(requires_primitives)
    if revision_anchor:
        spec["revision_anchor"] = copy.deepcopy(revision_anchor)
    consistency_warnings = list(consistency_result.warnings) + list(contract_validation.warnings)
    if semantic_used and bool(semantic_diagnostics.get("limited_mode")):
        reason = str(semantic_diagnostics.get("limited_mode_reason") or "limited mode").strip()
        consistency_warnings.append(
            f"Semantic extraction ran in limited heuristic mode ({reason}); deterministic inference remains authoritative."
        )

    diagnostics = {
        "structure_score": round(structure_score, 3),
        "route": route,
        "llm_used": bool(semantic_diagnostics.get("llm_used")),
        "appspec_semantic_capability_state": (
            str(semantic_diagnostics.get("capability_state") or "limited_no_llm")
            if semantic_used
            else "deterministic_only"
        ),
        "semantic_limited_mode": bool(semantic_diagnostics.get("limited_mode")) if semantic_used else False,
        "semantic_limited_mode_reason": (
            str(semantic_diagnostics.get("limited_mode_reason") or "").strip() if semantic_used else ""
        ),
        "consistency_warnings": consistency_warnings,
        "consistency_errors": list(consistency_result.errors) + list(contract_validation.errors),
        "fallback_or_repair_used": bool(
            semantic_diagnostics.get("fallback_used") or semantic_diagnostics.get("repair_used")
        ),
    }
    return spec, diagnostics


def _build_app_spec(
    *,
    workspace_id: uuid.UUID,
    title: str,
    raw_prompt: str,
    initial_intent: Optional[dict[str, Any]] = None,
    current_app_spec: Optional[dict[str, Any]] = None,
    current_app_summary: Optional[dict[str, Any]] = None,
    revision_anchor: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    spec, _ = _build_app_spec_with_diagnostics(
        workspace_id=workspace_id,
        title=title,
        raw_prompt=raw_prompt,
        initial_intent=initial_intent,
        current_app_spec=current_app_spec,
        current_app_summary=current_app_summary,
        revision_anchor=revision_anchor,
    )
    return spec


def _ports_yaml(ports: list[dict[str, Any]]) -> list[str]:
    return runtime_deploy_local.ports_yaml(ports)


def _resolve_published_port(container_name: str, target: str) -> int:
    return runtime_adapters.resolve_published_port(container_name, target)


def _docker_container_running(container_name: str) -> bool:
    return runtime_adapters.docker_container_running(container_name)

def _docker_network_exists(network_name: str) -> bool:
    return runtime_adapters.docker_network_exists(network_name)


def _materialize_net_inventory_compose(
    *,
    app_spec: dict[str, Any],
    policy_bundle: dict[str, Any] | None = None,
    deployment_dir: Path,
    compose_project: str,
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
) -> Path:
    return runtime_deploy_local.materialize_net_inventory_compose(
        app_spec=app_spec,
        policy_bundle=policy_bundle,
        deployment_dir=deployment_dir,
        compose_project=compose_project,
        external_network_name=external_network_name,
        external_network_alias=external_network_alias,
        build_resolved_capability_manifest_fn=build_resolved_capability_manifest,
        effective_net_inventory_image_fn=_effective_net_inventory_image,
        ports_yaml_fn=_ports_yaml,
    )


def _deploy_generated_runtime(
    *,
    app_spec: dict[str, Any],
    policy_bundle: dict[str, Any] | None,
    deployment_dir: Path,
    compose_project: str,
    logs: list[str],
    external_network_name: str | None = None,
    external_network_alias: str | None = None,
) -> dict[str, Any]:
    return runtime_deploy_local.deploy_generated_runtime(
        app_spec=app_spec,
        policy_bundle=policy_bundle,
        deployment_dir=deployment_dir,
        compose_project=compose_project,
        logs=logs,
        external_network_name=external_network_name,
        external_network_alias=external_network_alias,
        materialize_compose_fn=_materialize_net_inventory_compose,
        append_job_log_fn=_append_job_log,
        run_fn=_run,
        resolve_published_port_fn=_resolve_published_port,
    )


def _handle_generate_app_spec(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = parse_stage_input(job.input_json).to_dict()
    title = str(payload.get("title") or "Network Inventory").strip() or "Network Inventory"
    content = payload.get("content_json") if isinstance(payload.get("content_json"), dict) else {}
    raw_prompt = str(content.get("raw_prompt") or payload.get("raw_prompt") or title).strip()
    initial_intent = content.get("initial_intent") if isinstance(content.get("initial_intent"), dict) else {}
    revision_anchor = content.get("revision_anchor") if isinstance(content.get("revision_anchor"), dict) else None
    current_app_summary = content.get("current_app_summary") if isinstance(content.get("current_app_summary"), dict) else None
    current_app_spec = content.get("current_app_spec") if isinstance(content.get("current_app_spec"), dict) else None
    policy_bundle_override = (
        content.get("policy_bundle_override") if isinstance(content.get("policy_bundle_override"), dict) else None
    )
    policy_artifact_ref = content.get("policy_artifact_ref") if isinstance(content.get("policy_artifact_ref"), dict) else {}
    policy_source_hint = str(content.get("policy_source") or "").strip().lower()
    policy_compatibility = str(content.get("policy_compatibility") or "unknown").strip() or "unknown"
    policy_compatibility_reason = str(content.get("policy_compatibility_reason") or "").strip()
    primitive_catalog = get_primitive_catalog()
    _append_job_log(logs, f"Loaded primitive catalog ({len(primitive_catalog)} entries)")
    _append_job_log(logs, f"Generating AppSpec from prompt: {raw_prompt}")
    note = begin_stage_note(
        db,
        workspace_id=job.workspace_id,
        prompt_or_request=raw_prompt,
        findings=[
            "App-intent draft submit reached the non-trivial generation path.",
            "Primitive catalog inspection is required before finalizing AppSpec generation.",
            "The prompt requests a generated application contract that must remain faithful to the user's described domain.",
        ],
        root_cause="A durable AppSpec is required before deployment so runtime behavior remains auditable and artifact-linked.",
        proposed_fix="Generate an AppSpec first, persist it as an artifact, then queue deployment and validation stages while carrying the execution note forward.",
        implementation_summary="Started findings-first execution record for app generation.",
        validation_summary=["AppSpec generation not yet validated at note creation time."],
        debt_recorded=[],
        related_artifact_ids=[],
        status="in_progress",
        extra_metadata={"job_id": str(job.id), "job_type": job.type},
        create_note=create_execution_note,
    )
    _append_job_log(logs, f"Created execution-note artifact: {note.id}")

    # TODO(artifact-first, DEBT-07):
    # The generated artifact now acts as the canonical runtime identity.
    # AppSpec remains primarily a build intermediate. Future work may
    # consolidate AppSpec into an ArtifactSpec so prompts generate artifacts
    # directly while preserving the current packaging and install semantics.
    app_spec, inference_diagnostics = _build_app_spec_with_diagnostics(
        workspace_id=job.workspace_id,
        title=title,
        raw_prompt=raw_prompt,
        initial_intent=initial_intent,
        current_app_spec=current_app_spec,
        current_app_summary=current_app_summary,
        revision_anchor=revision_anchor,
    )
    try:
        validate(instance=app_spec, schema=_load_appspec_schema())
    except ValidationError as exc:
        raise RuntimeError(f"AppSpec validation failed: {exc.message}") from exc

    if policy_bundle_override:
        policy_bundle = copy.deepcopy(policy_bundle_override)
        policy_source = "artifact"
    else:
        policy_bundle = _build_policy_bundle(
            workspace_id=job.workspace_id,
            app_spec=app_spec,
            raw_prompt=raw_prompt,
        )
        policy_source = "reconstructed"
    if policy_source_hint == "artifact" and not policy_bundle_override:
        _append_job_log(logs, "Policy override requested but unavailable; using reconstructed policy bundle.")
    try:
        validate(instance=policy_bundle, schema=_load_policy_bundle_schema())
    except ValidationError as exc:
        raise RuntimeError(f"Policy bundle validation failed: {exc.message}") from exc

    generated_artifact_slug = _generated_artifact_slug(str(app_spec.get("app_slug") or "generated-app"))
    generated_lifecycle_identity = _generated_artifact_identity(
        artifact_slug=generated_artifact_slug,
        version_label="dev",
    )
    artifact_id = _persist_appspec_artifact(
        db,
        workspace_id=job.workspace_id,
        app_spec=app_spec,
        job_id=str(job.id),
        inference_diagnostics=inference_diagnostics,
        generated_artifact_slug=generated_lifecycle_identity.artifact_slug,
        revision_id=generated_lifecycle_identity.revision_id,
        version_label=generated_lifecycle_identity.version_label,
        lineage_id=generated_lifecycle_identity.lineage_id,
        lifecycle_stage=generated_lifecycle_identity.lifecycle_stage,
        persist_fn=_persist_json_artifact,
    )
    _append_job_log(logs, f"Persisted AppSpec artifact: {artifact_id}")
    policy_bundle_artifact_id = _persist_policy_artifact(
        db,
        workspace_id=job.workspace_id,
        app_slug=str(app_spec.get("app_slug") or "generated-app"),
        policy_bundle=policy_bundle,
        job_id=str(job.id),
        app_spec_artifact_id=artifact_id,
        generated_artifact_slug=generated_lifecycle_identity.artifact_slug,
        revision_id=generated_lifecycle_identity.revision_id,
        version_label=generated_lifecycle_identity.version_label,
        lineage_id=generated_lifecycle_identity.lineage_id,
        lifecycle_stage=generated_lifecycle_identity.lifecycle_stage,
        policy_slug_fn=_policy_bundle_slug,
        persist_fn=_persist_json_artifact,
    )
    _append_job_log(logs, f"Persisted policy bundle artifact: {policy_bundle_artifact_id}")
    _link_generated_artifact_memberships(_db=db)

    selected_images = {svc.get("name"): svc.get("image") for svc in app_spec.get("services", []) if isinstance(svc, dict)}
    selected_ports = {
        svc.get("name"): svc.get("ports")
        for svc in app_spec.get("services", [])
        if isinstance(svc, dict)
    }
    generated_artifact_runtime_config = {
        "app_slug": app_spec["app_slug"],
        "artifact_slug": generated_lifecycle_identity.artifact_slug,
        "artifact_version": GENERATED_ARTIFACT_VERSION,
        "artifact_revision_id": generated_lifecycle_identity.revision_id,
        "artifact_version_label": generated_lifecycle_identity.version_label,
        "artifact_lineage_id": generated_lifecycle_identity.lineage_id,
        "artifact_lifecycle_stage": generated_lifecycle_identity.lifecycle_stage,
        "app_spec_artifact_id": artifact_id,
        "policy_bundle_artifact_id": policy_bundle_artifact_id,
        "images": selected_images,
        "ports": selected_ports,
        "services": app_spec.get("services") if isinstance(app_spec.get("services"), list) else [],
        "workspace_id": str(job.workspace_id),
        "source_job_id": str(job.id),
        "policy_source": policy_source,
        "policy_artifact_ref": policy_artifact_ref if isinstance(policy_artifact_ref, dict) else {},
        "policy_compatibility": policy_compatibility,
        "policy_compatibility_reason": policy_compatibility_reason,
    }
    packaged_artifact = _package_generated_app(
        workspace_id=job.workspace_id,
        source_job_id=str(job.id),
        app_spec=app_spec,
        policy_bundle=policy_bundle,
        runtime_config=generated_artifact_runtime_config,
        lifecycle_identity=generated_lifecycle_identity,
    )
    _append_job_log(
        logs,
        f"Packaged generated artifact {packaged_artifact['artifact_slug']} at {packaged_artifact['artifact_package_path']}",
    )
    registry_artifact: dict[str, Any] = {}
    registry_import_error = ""
    workspace = db.query(Workspace).filter(Workspace.id == job.workspace_id).first()
    workspace_slug = str(getattr(workspace, "slug", "development") or "development")
    try:
        registry_artifact = _import_generated_artifact_package(
            artifact_slug=str(packaged_artifact["artifact_slug"]),
            package_path=Path(str(packaged_artifact["artifact_package_path"])),
            workspace_slug=workspace_slug,
        )
        _append_job_log(
            logs,
            f"Imported generated artifact {packaged_artifact['artifact_slug']} into Django registry",
        )
    except Exception as exc:
        registry_import_error = f"{exc.__class__.__name__}: {exc}"
        _append_job_log(logs, f"Generated artifact import fallback engaged: {registry_import_error}")
    record_stage_metadata(
        db,
        artifact_id=note.id,
        implementation_summary="Generated and validated AppSpec, persisted it as an instance-local artifact, and packaged the generated app as an importable Django artifact bundle.",
        validation_summary=[
            "Primitive catalog loaded successfully.",
            "AppSpec validated against xyn.appspec.v0 schema.",
            "Policy bundle validated against xyn.policy_bundle.v0 schema.",
            f"Policy source: {policy_source}.",
            f"Policy compatibility: {policy_compatibility}{f' ({policy_compatibility_reason})' if policy_compatibility_reason else ''}.",
            f"AppSpec artifact persisted: {artifact_id}.",
            f"Policy bundle artifact persisted: {policy_bundle_artifact_id}.",
            f"Generated artifact package created: {packaged_artifact['artifact_slug']}@{packaged_artifact['artifact_version']}.",
            (
                f"Generated artifact imported into registry: {packaged_artifact['artifact_slug']}"
                if registry_artifact
                else f"Generated artifact registry import deferred: {registry_import_error or 'unknown error'}."
            ),
        ],
        related_artifact_ids=[artifact_id, policy_bundle_artifact_id],
        extra_metadata_updates={
            "app_spec_artifact_id": artifact_id,
            "policy_bundle_artifact_id": policy_bundle_artifact_id,
            "inference_diagnostics": inference_diagnostics,
            "policy_source": policy_source,
            "policy_artifact_ref": policy_artifact_ref if isinstance(policy_artifact_ref, dict) else {},
            "policy_compatibility": policy_compatibility,
            "policy_compatibility_reason": policy_compatibility_reason,
        },
        update_note=update_execution_note,
    )
    stage_output = build_stage_output(
        output_json={
            "app_spec": app_spec,
            "policy_bundle": policy_bundle,
            "app_spec_artifact_id": artifact_id,
            "policy_bundle_artifact_id": policy_bundle_artifact_id,
            "app_spec_schema": "xyn.appspec.v0",
            "policy_bundle_schema": "xyn.policy_bundle.v0",
            "policy_source": policy_source,
            "policy_artifact_ref": policy_artifact_ref if isinstance(policy_artifact_ref, dict) else {},
            "policy_compatibility": policy_compatibility,
            "policy_compatibility_reason": policy_compatibility_reason,
            "inference_diagnostics": inference_diagnostics,
            "primitive_catalog": primitive_catalog,
            "selected_images": selected_images,
            "selected_ports": selected_ports,
            "derived_urls": {"seed_ui": "http://localhost", "seed_api": "http://seed.localhost"},
            "generated_artifact": {
                **packaged_artifact,
                "registry_import": registry_artifact,
                "registry_import_error": registry_import_error,
            },
            "execution_note_artifact_id": str(note.id),
        },
        follow_up=[
            build_follow_up(
                job_type="deploy_app_local",
                input_json={
                    "app_spec": app_spec,
                    "policy_bundle": policy_bundle,
                    "app_spec_artifact_id": artifact_id,
                    "policy_bundle_artifact_id": policy_bundle_artifact_id,
                    "policy_source": policy_source,
                    "policy_artifact_ref": policy_artifact_ref if isinstance(policy_artifact_ref, dict) else {},
                    "policy_compatibility": policy_compatibility,
                    "policy_compatibility_reason": policy_compatibility_reason,
                    "generated_artifact": {
                        **packaged_artifact,
                        "registry_import": registry_artifact,
                        "registry_import_error": registry_import_error,
                    },
                    "execution_note_artifact_id": str(note.id),
                    "source_job_id": str(job.id),
                },
            )
        ],
    )
    return stage_output.output_json, [item.to_dict() for item in stage_output.follow_up]


def _handle_deploy_app_local(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return runtime_deploy_local.handle_deploy_app_local(
        db=db,
        job=job,
        logs=logs,
        parse_stage_input_fn=parse_stage_input,
        safe_slug_fn=_safe_slug,
        deployments_root_fn=_deployments_root,
        utc_now_fn=_utc_now,
        deploy_generated_runtime_fn=_deploy_generated_runtime,
        record_stage_metadata_fn=record_stage_metadata,
        update_execution_note_fn=update_execution_note,
        append_job_log_fn=_append_job_log,
        build_stage_output_fn=build_stage_output,
        build_follow_up_fn=build_follow_up,
    )


def _handle_provision_sibling_xyn(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return runtime_provision_sibling.handle_provision_sibling_xyn(
        db=db,
        job=job,
        logs=logs,
        parse_stage_input_fn=parse_stage_input,
        safe_slug_fn=_safe_slug,
        workspace_model=Workspace,
        environment_model=Environment,
        find_revision_sibling_target_fn=_find_revision_sibling_target,
        append_job_log_fn=_append_job_log,
        provision_local_instance_fn=provision_local_instance,
        prefer_local_platform_images_for_smoke_fn=_prefer_local_platform_images_for_smoke,
        docker_container_running_fn=_docker_container_running,
        import_generated_artifact_package_into_registry_fn=_import_generated_artifact_package_into_registry,
        install_generated_artifact_in_sibling_fn=_install_generated_artifact_in_sibling,
        generated_artifact_version=GENERATED_ARTIFACT_VERSION,
        docker_network_exists_fn=_docker_network_exists,
        deployments_root_fn=_deployments_root,
        deploy_generated_runtime_fn=_deploy_generated_runtime,
        register_sibling_runtime_target_fn=_register_sibling_runtime_target,
        record_stage_metadata_fn=record_stage_metadata,
        update_execution_note_fn=update_execution_note,
        build_stage_output_fn=build_stage_output,
        build_follow_up_fn=build_follow_up,
        ensure_default_environment_fn=ensure_default_environment,
        upsert_sibling_from_provision_output_fn=upsert_sibling_from_provision_output,
        create_or_update_activation_fn=create_or_update_activation,
        allocate_database_fn=allocate_database,
    )


def _field_map_from_contract(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return runtime_contract_verification._field_map_from_contract(contract)


def _extract_items_from_response(body: Any) -> list[dict[str, Any]]:
    return runtime_contract_verification._extract_items_from_response(body)


def _sample_field_value(
    *,
    contract: dict[str, Any],
    field: dict[str, Any],
    workspace_id: str,
    created_records: dict[str, dict[str, Any]],
) -> Any:
    return runtime_contract_verification._sample_field_value(
        contract=contract,
        field=field,
        workspace_id=workspace_id,
        created_records=created_records,
        normalize_unique_strings_fn=_normalize_unique_strings,
    )


def _build_contract_seed_payload(
    *,
    contract: dict[str, Any],
    workspace_id: str,
    created_records: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return runtime_contract_verification._build_contract_seed_payload(
        contract=contract,
        workspace_id=workspace_id,
        created_records=created_records,
        normalize_unique_strings_fn=_normalize_unique_strings,
    )


def _build_contract_update_payload(contract: dict[str, Any]) -> dict[str, Any]:
    return runtime_contract_verification._build_contract_update_payload(
        contract,
        normalize_unique_strings_fn=_normalize_unique_strings,
    )


def _policy_bundle_entries(policy_bundle: dict[str, Any], family: str) -> list[dict[str, Any]]:
    return runtime_contract_verification._policy_bundle_entries(policy_bundle, family)


def _compiled_runtime_policies(
    *,
    policy_bundle: dict[str, Any],
    family: str,
    runtime_rule: str,
    entity_key: str,
) -> list[dict[str, Any]]:
    return runtime_contract_verification._compiled_runtime_policies(
        policy_bundle=policy_bundle,
        family=family,
        runtime_rule=runtime_rule,
        entity_key=entity_key,
    )


def _allowed_transition_path(
    *,
    current_status: str,
    allowed_statuses: list[str],
    allowed_transitions: dict[str, list[str]],
) -> list[str] | None:
    return runtime_contract_verification._allowed_transition_path(
        current_status=current_status,
        allowed_statuses=allowed_statuses,
        allowed_transitions=allowed_transitions,
    )


def _ensure_parent_status_gate_prerequisites(
    *,
    container_name: str,
    port: int,
    workspace_id: str,
    contract: dict[str, Any],
    entity_contracts: list[dict[str, Any]],
    created_records: dict[str, dict[str, Any]],
    policy_bundle: dict[str, Any],
) -> None:
    return runtime_contract_verification.ensure_parent_status_gate_prerequisites(
        container_name=container_name,
        port=port,
        workspace_id=workspace_id,
        contract=contract,
        entity_contracts=entity_contracts,
        created_records=created_records,
        policy_bundle=policy_bundle,
        container_http_json_fn=_container_http_json,
    )


def _exercise_runtime_contracts(
    *,
    container_name: str,
    port: int,
    workspace_id: str,
    entity_contracts: list[dict[str, Any]],
    policy_bundle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return runtime_contract_verification.exercise_runtime_contracts(
        container_name=container_name,
        port=port,
        workspace_id=workspace_id,
        entity_contracts=entity_contracts,
        policy_bundle=policy_bundle,
        container_http_json_fn=_container_http_json,
        normalize_unique_strings_fn=_normalize_unique_strings,
    )


def _handle_smoke_test(db: Session, job: Job, logs: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return runtime_smoke_test.handle_smoke_test(
        db=db,
        job=job,
        logs=logs,
        parse_stage_input_fn=parse_stage_input,
        workspace_model=Workspace,
        append_job_log_fn=_append_job_log,
        wait_for_container_http_ok_fn=_wait_for_container_http_ok,
        app_deploy_health_timeout_seconds=APP_DEPLOY_HEALTH_TIMEOUT_SECONDS,
        container_http_json_fn=_container_http_json,
        build_resolved_capability_manifest_fn=build_resolved_capability_manifest,
        exercise_runtime_contracts_fn=_exercise_runtime_contracts,
        root_platform_api_container=ROOT_PLATFORM_API_CONTAINER,
        container_http_session_json_fn=_container_http_session_json,
        docker_container_running_fn=_docker_container_running,
        execute_sibling_palette_prompt_fn=_execute_sibling_palette_prompt,
        run_fn=_run,
        finalize_stage_note_fn=finalize_stage_note,
        update_execution_note_fn=update_execution_note,
        build_stage_output_fn=build_stage_output,
        upsert_sibling_from_provision_output_fn=upsert_sibling_from_provision_output,
        create_or_update_activation_fn=create_or_update_activation,
    )


def _claim_next_job(db: Session) -> Optional[Job]:
    # Queue ownership: this shell exclusively claims queued jobs and marks them
    # RUNNING before stage dispatch.
    row = (
        db.query(Job)
        .filter(Job.status == JobStatus.QUEUED.value)
        .order_by(Job.created_at.asc())
        .first()
    )
    if not row:
        return None
    row.status = JobStatus.RUNNING.value
    row.updated_at = _utc_now()
    prefix = row.logs_text.rstrip() + "\n" if row.logs_text else ""
    row.logs_text = f"{prefix}[{_iso_now()}] Worker claimed job {row.id} ({row.type})"
    db.commit()
    db.refresh(row)
    return row


def _enqueue_job(db: Session, *, workspace_id: uuid.UUID, job_type: str, input_json: dict[str, Any]) -> str:
    next_job = Job(
        workspace_id=workspace_id,
        type=job_type,
        status=JobStatus.QUEUED.value,
        input_json=input_json,
        output_json={},
        logs_text=f"[{_iso_now()}] Queued by app-job-worker.",
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    db.add(next_job)
    db.flush()
    return str(next_job.id)


def _recover_running_jobs(db: Session) -> None:
    running = db.query(Job).filter(Job.status == JobStatus.RUNNING.value).all()
    if not running:
        return
    for row in running:
        row.status = JobStatus.FAILED.value
        payload = row.output_json if isinstance(row.output_json, dict) else {}
        payload["error"] = "Job interrupted by process restart before completion."
        row.output_json = payload
        prefix = row.logs_text.rstrip() + "\n" if row.logs_text else ""
        row.logs_text = f"{prefix}[{_iso_now()}] Worker startup recovered stale RUNNING job as FAILED."
        row.updated_at = _utc_now()
    db.commit()


def _execute_job(job_id: uuid.UUID) -> None:
    # Stage router: dispatch by job.type and preserve fail-fast error semantics.
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return
        logs: list[str] = []
        output_json: dict[str, Any] = {}
        follow_up_jobs: list[dict[str, Any]] = []
        _append_job_log(logs, f"Executing job type={job.type}")
        try:
            if job.type == "generate_app_spec":
                output_json, follow_up_jobs = _handle_generate_app_spec(db, job, logs)
            elif job.type == "deploy_app_local":
                output_json, follow_up_jobs = _handle_deploy_app_local(db, job, logs)
            elif job.type == "provision_sibling_xyn":
                output_json, follow_up_jobs = _handle_provision_sibling_xyn(db, job, logs)
            elif job.type == "smoke_test":
                output_json, follow_up_jobs = _handle_smoke_test(db, job, logs)
            else:
                raise RuntimeError(f"Unsupported job type: {job.type}")
            queued_ids = []
            for item in follow_up_jobs:
                next_id = _enqueue_job(
                    db,
                    workspace_id=job.workspace_id,
                    job_type=str(item.get("type") or "").strip(),
                    input_json=item.get("input_json") if isinstance(item.get("input_json"), dict) else {},
                )
                queued_ids.append({"job_type": item.get("type"), "job_id": next_id})
            if queued_ids:
                output_json["queued_jobs"] = queued_ids
                for item in queued_ids:
                    _append_job_log(logs, f"Queued follow-up job: {item['job_type']} ({item['job_id']})")
            job.status = JobStatus.SUCCEEDED.value
            job.output_json = output_json
            _append_job_log(logs, "Job completed successfully")
        except Exception as exc:
            job.status = JobStatus.FAILED.value
            output_json = output_json or {}
            output_json["error"] = str(exc)
            job.output_json = output_json
            activation_id = (
                str((job.input_json or {}).get("activation_id") or "").strip()
                or str((output_json or {}).get("activation_id") or "").strip()
            )
            if activation_id:
                try:
                    mark_activation_failed(
                        db,
                        activation_id=activation_id,
                        error_text=str(exc),
                        source_job_id=job.id,
                    )
                except Exception:
                    pass
            execution_note_artifact_id = resolve_execution_note_artifact_id(job.input_json, output_json)
            if execution_note_artifact_id:
                try:
                    record_stage_failure(
                        db,
                        artifact_id=uuid.UUID(execution_note_artifact_id),
                        job_type=job.type,
                        error=exc,
                        update_note=update_execution_note,
                    )
                except Exception:
                    pass
            _append_job_log(logs, f"Job failed: {exc}")
        existing = job.logs_text.rstrip() + "\n" if job.logs_text else ""
        job.logs_text = existing + "\n".join(logs)
        job.updated_at = _utc_now()
        db.commit()
    finally:
        db.close()


def _worker_loop(stop_event: threading.Event) -> None:
    # Worker lifecycle shell: recover stale RUNNING rows, then claim/execute.
    bootstrap_db = SessionLocal()
    try:
        _recover_running_jobs(bootstrap_db)
    finally:
        bootstrap_db.close()
    while not stop_event.is_set():
        db = SessionLocal()
        try:
            row = _claim_next_job(db)
            if not row:
                time.sleep(POLL_SECONDS)
                continue
            _execute_job(row.id)
        finally:
            db.close()


@dataclass
class AppJobWorkerHandle:
    thread: threading.Thread
    stop_event: threading.Event


def start_app_job_worker() -> AppJobWorkerHandle:
    stop_event = threading.Event()
    thread = threading.Thread(target=_worker_loop, args=(stop_event,), daemon=True, name="xyn-app-job-worker")
    thread.start()
    return AppJobWorkerHandle(thread=thread, stop_event=stop_event)


def stop_app_job_worker(handle: Optional[AppJobWorkerHandle]) -> None:
    if not handle:
        return
    handle.stop_event.set()
    handle.thread.join(timeout=5)


# AppSpec extraction compatibility shims:
# Preserve existing private symbol names for callers/tests while delegating to
# extracted modules without changing behavior.
_safe_slug = appspec_normalization._safe_slug
_normalize_unique_strings = appspec_normalization._normalize_unique_strings
_title_case_words = appspec_normalization._title_case_words
_pluralize_label = appspec_normalization._pluralize_label
_contains_phrase = appspec_primitive_inference._contains_phrase
_infer_primitives_from_text = appspec_primitive_inference._infer_primitives_from_text
_extract_objective_sections = appspec_prompt_sections._extract_objective_sections
_extract_prompt_sections = appspec_prompt_sections._extract_prompt_sections
_pick_prompt_section = appspec_prompt_sections._pick_prompt_section
_extract_workflow_blocks_from_prompt = appspec_prompt_sections._extract_workflow_blocks_from_prompt
_build_structured_plan_snapshot = appspec_prompt_sections._build_structured_plan_snapshot
_extract_app_name_from_prompt = appspec_entity_inference._extract_app_name_from_prompt
_extract_objective_entities = appspec_entity_inference._extract_objective_entities
_field_options_from_token = appspec_entity_inference._field_options_from_token
_sanitize_field_label = appspec_entity_inference._sanitize_field_label
_field_key = appspec_entity_inference._field_key
_field_type_for_token = appspec_entity_inference._field_type_for_token
_build_entity_contracts_from_prompt = appspec_entity_inference._build_entity_contracts_from_prompt
_infer_entities_from_prompt = appspec_entity_inference._infer_entities_from_prompt
_infer_requested_visuals_from_prompt = appspec_entity_inference._infer_requested_visuals_from_prompt
_infer_entities_from_app_spec = appspec_entity_inference._infer_entities_from_app_spec
_augment_contracts_with_inferred_selection_flags = (
    appspec_entity_inference._augment_contracts_with_inferred_selection_flags
)
