from __future__ import annotations

import copy
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError

from core.models import Artifact, WorkspaceSetting

DEFAULT_ARTIFACT_REGISTRY = "public.ecr.aws/i0h0h0n4/xyn/artifacts"
DEFAULT_REGISTRY_SLUG = "default-registry"
DEFAULT_CHANNEL = "dev"
DEFAULT_NAMING = {
    "ui_image_name": "xyn-ui",
    "api_image_name": "xyn-api",
}


@dataclass(frozen=True)
class ResolvedArtifactRegistry:
    slug: str
    source: str
    spec: Dict[str, Any]


def _default_registry_spec(slug: str = DEFAULT_REGISTRY_SLUG, endpoint: str = DEFAULT_ARTIFACT_REGISTRY) -> Dict[str, Any]:
    return {
        "slug": slug,
        "title": "Default Artifact Registry",
        "provider": "oci",
        "endpoint": endpoint,
        "visibility": "public",
        "auth": {"mode": "none", "secret_ref": None},
        "defaults": {
            "channel": DEFAULT_CHANNEL,
            "naming": dict(DEFAULT_NAMING),
        },
        "pin_policy": "tag",
        "allow_insecure": False,
        "notes": "",
    }


def validate_registry_spec(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("artifact registry spec must be an object")

    normalized = copy.deepcopy(raw)
    slug = str(normalized.get("slug") or "").strip().lower()
    if not slug:
        raise ValueError("slug is required")
    normalized["slug"] = slug

    title = str(normalized.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    normalized["title"] = title

    provider = str(normalized.get("provider") or "").strip().lower() or "oci"
    if provider not in {"oci", "s3"}:
        raise ValueError("provider must be oci|s3")
    normalized["provider"] = provider

    endpoint = str(normalized.get("endpoint") or "").strip().rstrip("/")
    if not endpoint:
        raise ValueError("endpoint is required")
    normalized["endpoint"] = endpoint

    visibility = str(normalized.get("visibility") or "public").strip().lower() or "public"
    if visibility not in {"public", "private"}:
        raise ValueError("visibility must be public|private")
    normalized["visibility"] = visibility

    auth = normalized.get("auth") if isinstance(normalized.get("auth"), dict) else {}
    mode = str(auth.get("mode") or "none").strip().lower() or "none"
    if mode not in {"none", "basic", "aws-ecr", "token"}:
        raise ValueError("auth.mode must be none|basic|aws-ecr|token")
    secret_ref_raw = auth.get("secret_ref")
    secret_ref = str(secret_ref_raw).strip() if secret_ref_raw not in {None, ""} else None
    if mode != "none" and not secret_ref:
        raise ValueError("auth.secret_ref is required when auth.mode != none")
    auth["mode"] = mode
    auth["secret_ref"] = secret_ref
    normalized["auth"] = auth

    defaults = normalized.get("defaults") if isinstance(normalized.get("defaults"), dict) else {}
    channel = str(defaults.get("channel") or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL
    naming = defaults.get("naming") if isinstance(defaults.get("naming"), dict) else {}
    naming = {
        "ui_image_name": str(naming.get("ui_image_name") or DEFAULT_NAMING["ui_image_name"]).strip() or DEFAULT_NAMING["ui_image_name"],
        "api_image_name": str(naming.get("api_image_name") or DEFAULT_NAMING["api_image_name"]).strip() or DEFAULT_NAMING["api_image_name"],
    }
    defaults["channel"] = channel
    defaults["naming"] = naming
    normalized["defaults"] = defaults

    pin_policy = str(normalized.get("pin_policy") or "tag").strip().lower() or "tag"
    if pin_policy not in {"tag", "digest"}:
        raise ValueError("pin_policy must be tag|digest")
    normalized["pin_policy"] = pin_policy

    normalized["allow_insecure"] = bool(normalized.get("allow_insecure", False))
    normalized["notes"] = str(normalized.get("notes") or "")
    return normalized


def _registry_spec_from_artifact(artifact: Artifact) -> Dict[str, Any]:
    metadata = artifact.extra_metadata if isinstance(artifact.extra_metadata, dict) else {}
    spec = metadata.get("spec") if isinstance(metadata.get("spec"), dict) else {}
    if not spec:
        spec = {"slug": artifact.name, "title": artifact.name, "provider": "oci", "endpoint": "", "visibility": "public"}
    return validate_registry_spec(spec)


def list_registry_specs(db: Session) -> list[Dict[str, Any]]:
    rows = db.query(Artifact).filter(Artifact.kind == "artifact-registry").order_by(Artifact.created_at.asc()).all()
    out: list[Dict[str, Any]] = []
    for row in rows:
        try:
            out.append(_registry_spec_from_artifact(row))
        except Exception:
            continue
    return out


def get_registry_spec_by_slug(db: Session, slug: str) -> Optional[Dict[str, Any]]:
    normalized_slug = str(slug or "").strip().lower()
    if not normalized_slug:
        return None
    rows = db.query(Artifact).filter(Artifact.kind == "artifact-registry").all()
    for row in rows:
        try:
            spec = _registry_spec_from_artifact(row)
        except Exception:
            continue
        if spec.get("slug") == normalized_slug:
            return spec
    return None


def upsert_registry_spec(db: Session, spec: Dict[str, Any], *, created_by: str = "xyn-seed") -> Dict[str, Any]:
    normalized = validate_registry_spec(spec)
    slug = normalized["slug"]

    target: Optional[Artifact] = None
    rows = db.query(Artifact).filter(Artifact.kind == "artifact-registry").all()
    for row in rows:
        try:
            row_spec = _registry_spec_from_artifact(row)
        except Exception:
            continue
        if row_spec.get("slug") == slug:
            target = row
            break

    payload_json = json.dumps(normalized, sort_keys=True)
    metadata = {"spec": normalized}
    if target is None:
        target = Artifact(
            name=slug,
            kind="artifact-registry",
            content_type="application/json",
            byte_length=len(payload_json.encode("utf-8")),
            sha256=None,
            created_by=created_by,
            extra_metadata=metadata,
            storage_path=None,
        )
        db.add(target)
    else:
        target.name = slug
        target.content_type = "application/json"
        target.byte_length = len(payload_json.encode("utf-8"))
        target.extra_metadata = metadata
    db.commit()
    return normalized


def ensure_seed_default_registry(db: Session) -> Dict[str, Any]:
    existing = get_registry_spec_by_slug(db, DEFAULT_REGISTRY_SLUG)
    if existing:
        return existing
    env_registry = str(os.getenv("XYN_ARTIFACT_REGISTRY") or "").strip() or DEFAULT_ARTIFACT_REGISTRY
    spec = _default_registry_spec(slug=DEFAULT_REGISTRY_SLUG, endpoint=env_registry)
    return upsert_registry_spec(db, spec, created_by="seed-bootstrap")


def get_workspace_default_registry_slug(db: Session, workspace_slug: str = "default") -> Optional[str]:
    try:
        row = db.query(WorkspaceSetting).filter(WorkspaceSetting.workspace_slug == workspace_slug).first()
    except (ProgrammingError, OperationalError):
        db.rollback()
        return None
    if not row:
        return None
    value = str(row.default_artifact_registry_slug or "").strip().lower()
    return value or None


def set_workspace_default_registry_slug(db: Session, workspace_slug: str, registry_slug: str) -> str:
    ws = str(workspace_slug or "default").strip().lower() or "default"
    target_slug = str(registry_slug or "").strip().lower()
    if not target_slug:
        raise ValueError("default_artifact_registry_slug is required")
    if not get_registry_spec_by_slug(db, target_slug):
        raise ValueError(f"artifact registry not found: {target_slug}")

    try:
        row = db.query(WorkspaceSetting).filter(WorkspaceSetting.workspace_slug == ws).first()
    except (ProgrammingError, OperationalError) as exc:
        db.rollback()
        raise ValueError("workspace settings schema is not initialized; run migrations") from exc
    if row is None:
        row = WorkspaceSetting(workspace_slug=ws, default_artifact_registry_slug=target_slug)
        db.add(row)
    else:
        row.default_artifact_registry_slug = target_slug
    db.commit()
    return target_slug


def resolve_registry(
    db: Session,
    *,
    explicit_registry_slug: Optional[str] = None,
    workspace_slug: str = "default",
    env_registry: Optional[str] = None,
) -> ResolvedArtifactRegistry:
    ensure_seed_default_registry(db)

    explicit = str(explicit_registry_slug or "").strip().lower()
    if explicit:
        found = get_registry_spec_by_slug(db, explicit)
        if not found:
            raise ValueError(f"artifact registry not found: {explicit}")
        return ResolvedArtifactRegistry(slug=explicit, source="explicit", spec=found)

    workspace_default = get_workspace_default_registry_slug(db, workspace_slug=workspace_slug)
    if workspace_default:
        found = get_registry_spec_by_slug(db, workspace_default)
        if found:
            return ResolvedArtifactRegistry(slug=workspace_default, source="workspace_default", spec=found)

    seeded = get_registry_spec_by_slug(db, DEFAULT_REGISTRY_SLUG)
    if seeded:
        return ResolvedArtifactRegistry(slug=DEFAULT_REGISTRY_SLUG, source="seeded_default", spec=seeded)

    endpoint = str(env_registry or "").strip() or str(os.getenv("XYN_ARTIFACT_REGISTRY") or "").strip()
    if endpoint:
        return ResolvedArtifactRegistry(
            slug="env-fallback",
            source="env_fallback",
            spec=validate_registry_spec(_default_registry_spec(slug="env-fallback", endpoint=endpoint)),
        )

    return ResolvedArtifactRegistry(
        slug=DEFAULT_REGISTRY_SLUG,
        source="builtin_default",
        spec=validate_registry_spec(_default_registry_spec()),
    )


def build_image_refs(spec: Dict[str, Any], *, channel: Optional[str] = None) -> Dict[str, str]:
    normalized = validate_registry_spec(spec)
    endpoint = str(normalized["endpoint"]).rstrip("/")
    defaults = normalized["defaults"]
    naming = defaults["naming"]
    effective_channel = str(channel or defaults.get("channel") or DEFAULT_CHANNEL).strip() or DEFAULT_CHANNEL

    return {
        "channel": effective_channel,
        "ui_image": f"{endpoint}/{naming['ui_image_name']}:{effective_channel}",
        "api_image": f"{endpoint}/{naming['api_image_name']}:{effective_channel}",
    }


def _run(cmd: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def pull_if_missing(image_ref: str) -> str:
    inspect_code, _, _ = _run(["docker", "image", "inspect", image_ref])
    if inspect_code == 0:
        return f"Using cached artifact image {image_ref}"

    code, _, _ = _run(["docker", "pull", image_ref])
    if code != 0:
        raise RuntimeError(
            "Unable to pull artifact image:\n"
            f"{image_ref}\n\n"
            "Possible fixes:\n"
            "- Verify network access\n"
            "- Override XYN_ARTIFACT_REGISTRY in .env\n"
            "- Provide local build contexts:\n"
            "    XYN_LOCAL_API_CONTEXT\n"
            "    XYN_LOCAL_UI_CONTEXT"
        )
    return f"Pulled artifact image {image_ref}"


def resolve_registry_images(
    db: Session,
    *,
    explicit_registry_slug: Optional[str] = None,
    workspace_slug: str = "default",
    channel: Optional[str] = None,
    ensure_local: bool = False,
) -> Dict[str, Any]:
    resolved = resolve_registry(
        db,
        explicit_registry_slug=explicit_registry_slug,
        workspace_slug=workspace_slug,
    )
    refs = build_image_refs(resolved.spec, channel=channel)
    operations: list[str] = []
    if ensure_local:
        for image_ref in (refs["api_image"], refs["ui_image"]):
            operations.append(pull_if_missing(image_ref))
    return {
        "registry_slug": resolved.slug,
        "registry_source": resolved.source,
        "registry": resolved.spec,
        "images": refs,
        "operations": operations,
    }


def apply_release_spec_artifact_resolution(
    db: Session,
    release_spec: Dict[str, Any],
    *,
    workspace_slug: str = "default",
) -> Dict[str, Any]:
    if not isinstance(release_spec, dict):
        return release_spec
    resolution = release_spec.get("artifactResolution")
    if not isinstance(resolution, dict):
        return release_spec

    registry_slug = str(resolution.get("registry_slug") or "").strip() or None
    channel = str(resolution.get("channel") or "").strip() or None
    component_rules = resolution.get("components") if isinstance(resolution.get("components"), dict) else {}

    resolved = resolve_registry_images(
        db,
        explicit_registry_slug=registry_slug,
        workspace_slug=workspace_slug,
        channel=channel,
        ensure_local=False,
    )
    refs = resolved["images"]
    naming = resolved["registry"].get("defaults", {}).get("naming", {})
    endpoint = str(resolved["registry"].get("endpoint") or "").rstrip("/")
    effective_channel = refs["channel"]

    def _default_name_for_component(component_name: str) -> Optional[str]:
        token = str(component_name or "").strip().lower()
        if token in {"ui", "xyn-ui"} or "ui" in token:
            return str(naming.get("ui_image_name") or DEFAULT_NAMING["ui_image_name"])
        if token in {"api", "xyn-api", "xyn-api"} or "api" in token or "backend" in token:
            return str(naming.get("api_image_name") or DEFAULT_NAMING["api_image_name"])
        return None

    updated = copy.deepcopy(release_spec)
    components = updated.get("components") if isinstance(updated.get("components"), list) else []
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("image") or "").strip():
            continue
        name = str(component.get("name") or "").strip()
        rule = component_rules.get(name) if isinstance(component_rules.get(name), dict) else {}
        explicit_image = str(rule.get("image") or "").strip()
        if explicit_image:
            component["image"] = explicit_image
            continue

        image_name = str(rule.get("image_name") or "").strip() or _default_name_for_component(name)
        if not image_name:
            continue
        digest = str(rule.get("digest") or "").strip()
        tag = str(rule.get("tag") or effective_channel).strip() or effective_channel
        if digest:
            component["image"] = f"{endpoint}/{image_name}@{digest}"
        else:
            component["image"] = f"{endpoint}/{image_name}:{tag}"
    return updated
