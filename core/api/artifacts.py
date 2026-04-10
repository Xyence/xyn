"""Artifact API endpoints."""
import datetime
import logging
import os
import uuid
from typing import Any, Optional
from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, Response
from pathlib import Path
from sqlalchemy.orm import Session

from core.database import get_db
from core import models, schemas
from core.artifact_code_review import (
    analyze_codebase,
    build_hierarchical_tree,
    build_source_index,
    compute_module_metrics,
    parse_artifact_source_files,
    read_file_chunk,
    search_files,
)
from core.artifact_source_resolution import (
    parse_packaged_artifact_metadata,
    resolve_artifact_source,
)
from core.artifact_provenance import extract_provenance_metadata, merge_provenance_metadata
from core.source_tree_bounds import apply_source_tree_bounds
from core.access_control import (
    CAP_ARTIFACTS_READ,
    CAP_CAMPAIGNS_MANAGE,
    CAP_REFRESHES_RUN,
    CAP_SOURCES_MANAGE,
    AccessPrincipal,
    require_capabilities,
)
from core.artifact_store import get_runtime_artifact_store

router = APIRouter()
logger = logging.getLogger(__name__)

# Initialize artifact store
artifact_store = get_runtime_artifact_store()


@router.post("/artifacts", response_model=schemas.Artifact, status_code=201)
async def create_artifact(
    name: str,
    kind: str,
    content_type: str,
    file: UploadFile = File(...),
    workspace_id: Optional[uuid.UUID] = None,
    run_id: Optional[uuid.UUID] = None,
    step_id: Optional[uuid.UUID] = None,
    storage_scope: str = "instance-local",
    sync_state: str = "local",
    principal: AccessPrincipal = Depends(
        require_capabilities(CAP_SOURCES_MANAGE, CAP_CAMPAIGNS_MANAGE, CAP_REFRESHES_RUN, require_all=False)
    ),
    db: Session = Depends(get_db)
):
    """Create and upload an artifact.

    Args:
        name: Artifact name
        kind: Artifact kind (log, report, bundle, file)
        content_type: Content type
        file: File upload
        run_id: Optional run association
        step_id: Optional step association
        db: Database session

    Returns:
        Created artifact metadata
    """
    # Generate artifact ID
    artifact_id = uuid.uuid4()

    # Store in artifact store using streaming path (avoids full-memory buffering)
    storage_path, sha256_hash, byte_length = await artifact_store.store_stream(
        artifact_id=artifact_id,
        stream=file.file,
        compute_sha256=True,
    )

    # Create artifact record
    artifact = models.Artifact(
        id=artifact_id,
        workspace_id=workspace_id,
        name=name,
        kind=kind,
        storage_scope=str(storage_scope or "instance-local").strip() or "instance-local",
        sync_state=str(sync_state or "local").strip() or "local",
        content_type=content_type,
        byte_length=byte_length,
        sha256=sha256_hash,
        run_id=run_id,
        step_id=step_id,
        created_by="user",  # v0: no auth
        storage_path=storage_path,
        extra_metadata={}
    )

    db.add(artifact)
    db.commit()
    db.refresh(artifact)

    # Emit artifact.created event
    event = models.Event(
        event_name="xyn.artifact.created",
        occurred_at=datetime.datetime.utcnow(),
        env_id=os.getenv("ENV_ID", "local-dev"),
        actor="user",
        correlation_id=str(uuid.uuid4()),
        run_id=run_id,
        step_id=step_id,
        resource_type="artifact",
        resource_id=str(artifact_id),
        data={"name": name, "kind": kind}
    )
    db.add(event)
    db.commit()

    return schemas.Artifact.from_orm_model(artifact)


@router.get("/artifacts", response_model=schemas.ArtifactListResponse)
async def list_artifacts(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = None,
    workspace_id: Optional[uuid.UUID] = None,
    run_id: Optional[uuid.UUID] = None,
    kind: Optional[str] = None,
    storage_scope: Optional[str] = None,
    sync_state: Optional[str] = None,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db)
):
    """List artifacts with optional filtering and pagination.

    Args:
        limit: Maximum number of artifacts to return (1-500)
        cursor: Pagination cursor (artifact ID to start after)
        run_id: Filter by run ID
        kind: Filter by artifact kind
        db: Database session

    Returns:
        List of artifacts with optional next cursor
    """
    query = db.query(models.Artifact)

    # Apply filters
    if workspace_id:
        query = query.filter(models.Artifact.workspace_id == workspace_id)
    if run_id:
        query = query.filter(models.Artifact.run_id == run_id)
    if kind:
        query = query.filter(models.Artifact.kind == kind)
    if storage_scope:
        query = query.filter(models.Artifact.storage_scope == storage_scope.strip())
    if sync_state:
        query = query.filter(models.Artifact.sync_state == sync_state.strip())

    # Order by created_at descending, then id descending for stable ordering
    query = query.order_by(models.Artifact.created_at.desc(), models.Artifact.id.desc())

    # Apply cursor pagination
    if cursor:
        try:
            cursor_id = uuid.UUID(cursor)
            # Find the cursor artifact to get its created_at
            cursor_artifact = db.query(models.Artifact).filter(models.Artifact.id == cursor_id).first()
            if cursor_artifact:
                # Filter to artifacts created before the cursor, or same timestamp with lower ID
                query = query.filter(
                    (models.Artifact.created_at < cursor_artifact.created_at) |
                    ((models.Artifact.created_at == cursor_artifact.created_at) & (models.Artifact.id < cursor_id))
                )
        except ValueError:
            pass  # Invalid cursor, ignore

    # Offset-based pagination fallback for compatibility with clients expecting
    # limit/offset semantics.
    if offset:
        query = query.offset(offset)

    # Fetch limit + 1 to determine if there are more cursor results.
    artifacts = query.limit(limit + 1).all()

    # Determine next cursor
    next_cursor = None
    if len(artifacts) > limit:
        next_cursor = str(artifacts[limit - 1].id)
        artifacts = artifacts[:limit]

    # Convert to schema
    items = [schemas.Artifact.from_orm_model(a) for a in artifacts]

    return schemas.ArtifactListResponse(items=items, next_cursor=next_cursor)


def _artifact_slug(row: models.Artifact) -> str:
    metadata = row.extra_metadata if isinstance(row.extra_metadata, dict) else {}
    candidate = str(metadata.get("generated_artifact_slug") or "").strip()
    if candidate:
        return candidate
    return str(row.name or "").strip()


def _artifact_identity_payload(row: models.Artifact) -> dict[str, Any]:
    metadata = merge_provenance_metadata(row.extra_metadata if isinstance(row.extra_metadata, dict) else {})
    return {
        "id": str(row.id),
        "slug": _artifact_slug(row),
        "provenance": extract_provenance_metadata(metadata),
    }


def _resolve_artifact(
    db: Session,
    *,
    artifact_id: Optional[uuid.UUID],
    artifact_slug: Optional[str],
) -> models.Artifact:
    if artifact_id:
        row = db.query(models.Artifact).filter(models.Artifact.id == artifact_id).first()
        if row:
            return row
    slug = str(artifact_slug or "").strip()
    if slug:
        # First try direct name match.
        row = (
            db.query(models.Artifact)
            .filter(models.Artifact.name == slug)
            .order_by(models.Artifact.created_at.desc(), models.Artifact.id.desc())
            .first()
        )
        if row:
            return row
        # Fallback: metadata-backed generated artifact slug.
        # Keep this as a simple scan for MVP compatibility across DB backends.
        rows = (
            db.query(models.Artifact)
            .order_by(models.Artifact.created_at.desc(), models.Artifact.id.desc())
            .limit(5000)
            .all()
        )
        for item in rows:
            metadata = item.extra_metadata if isinstance(item.extra_metadata, dict) else {}
            if str(metadata.get("generated_artifact_slug") or "").strip() == slug:
                return item
    raise HTTPException(status_code=404, detail="Artifact not found")


async def _artifact_bytes(row: models.Artifact) -> bytes:
    payload = await artifact_store.retrieve(row.id)
    if payload is not None:
        return bytes(payload)
    artifact_path = artifact_store.get_path(row.id)
    if artifact_path and artifact_path.exists():
        return artifact_path.read_bytes()
    if row.storage_path:
        legacy = Path(str(row.storage_path))
        if legacy.exists():
            return legacy.read_bytes()
    raise HTTPException(status_code=404, detail="Artifact content not found")


def _resolved_artifact_source_payload(row: models.Artifact, payload: bytes) -> dict[str, Any]:
    packaged_files = parse_artifact_source_files(artifact_name=row.name, artifact_bytes=payload)
    row_metadata = merge_provenance_metadata(row.extra_metadata if isinstance(row.extra_metadata, dict) else {})
    packaged_metadata = merge_provenance_metadata(parse_packaged_artifact_metadata(packaged_files))
    merged_metadata = merge_provenance_metadata({**row_metadata, **packaged_metadata})
    resolved = resolve_artifact_source(
        artifact_slug=_artifact_slug(row),
        artifact_id=str(row.id),
        source_ref_type=str(merged_metadata.get("source_ref_type") or ""),
        source_ref_id=str(merged_metadata.get("source_ref_id") or ""),
        metadata=merged_metadata,
        packaged_files=packaged_files,
    )
    if resolved.source_mode == "packaged_fallback":
        source = resolved.provenance.get("source") if isinstance(resolved.provenance.get("source"), dict) else {}
        repo_key = str(source.get("repo_key") or "").strip() or "unknown"
        warnings = [str(item) for item in (resolved.warnings or []) if str(item).strip()]
        logger.warning(
            "artifact source fallback used artifact_id=%s artifact_slug=%s repo_key=%s source_origin=%s warnings=%s",
            row.id,
            _artifact_slug(row),
            repo_key,
            resolved.source_origin,
            warnings[:4],
        )
    return {
        "files": resolved.files,
        "source_mode": resolved.source_mode,
        "source_origin": resolved.source_origin,
        "resolution_branch": resolved.resolution_branch,
        "resolution_details": resolved.resolution_details,
        "provenance": resolved.provenance,
        "resolved_source_roots": resolved.resolved_source_roots,
        "warnings": resolved.warnings,
    }


def _normalize_extensions(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    out: list[str] = []
    for token in str(raw).split(","):
        item = str(token or "").strip()
        if not item:
            continue
        out.append(item if item.startswith(".") else f".{item}")
    return out


@router.get("/artifacts/source-tree")
async def get_artifact_source_tree(
    artifact_id: Optional[uuid.UUID] = Query(default=None),
    artifact_slug: Optional[str] = Query(default=None),
    include_line_counts: bool = Query(default=True),
    max_files: Optional[int] = Query(default=None, ge=1, le=20000),
    max_depth: Optional[int] = Query(default=None, ge=1, le=64),
    include_files: bool = Query(default=True),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db),
):
    row = _resolve_artifact(db, artifact_id=artifact_id, artifact_slug=artifact_slug)
    payload = await _artifact_bytes(row)
    resolved = _resolved_artifact_source_payload(row, payload)
    files = resolved["files"] if isinstance(resolved.get("files"), dict) else {}
    index_rows = build_source_index(files, include_line_counts=bool(include_line_counts))
    bounded_rows = apply_source_tree_bounds(index_rows, max_files=max_files, max_depth=max_depth)
    tree = build_hierarchical_tree(bounded_rows)
    return {
        "artifact": _artifact_identity_payload(row),
        "source_mode": resolved.get("source_mode") or "packaged_fallback",
        "source_origin": resolved.get("source_origin") or "packaged_fallback",
        "resolution_branch": resolved.get("resolution_branch") or "packaged_fallback",
        "resolution_details": resolved.get("resolution_details") if isinstance(resolved.get("resolution_details"), dict) else {},
        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
        "resolved_source_roots": resolved.get("resolved_source_roots") or [],
        "warnings": resolved.get("warnings") or [],
        "file_count": len(bounded_rows),
        "tree": tree,
        "files": bounded_rows if bool(include_files) else [],
    }


@router.get("/artifacts/source-file")
async def read_artifact_source_file(
    path: str = Query(..., min_length=1),
    artifact_id: Optional[uuid.UUID] = Query(default=None),
    artifact_slug: Optional[str] = Query(default=None),
    start_line: Optional[int] = Query(default=None, ge=1),
    end_line: Optional[int] = Query(default=None, ge=1),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db),
):
    row = _resolve_artifact(db, artifact_id=artifact_id, artifact_slug=artifact_slug)
    payload = await _artifact_bytes(row)
    resolved = _resolved_artifact_source_payload(row, payload)
    files = resolved["files"] if isinstance(resolved.get("files"), dict) else {}
    try:
        chunk = read_file_chunk(files=files, path=path, start_line=start_line, end_line=end_line)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "artifact": _artifact_identity_payload(row),
        "source_mode": resolved.get("source_mode") or "packaged_fallback",
        "source_origin": resolved.get("source_origin") or "packaged_fallback",
        "resolution_branch": resolved.get("resolution_branch") or "packaged_fallback",
        "resolution_details": resolved.get("resolution_details") if isinstance(resolved.get("resolution_details"), dict) else {},
        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
        "resolved_source_roots": resolved.get("resolved_source_roots") or [],
        "warnings": resolved.get("warnings") or [],
        **chunk,
    }


@router.get("/artifacts/source-search")
async def search_artifact_source(
    query: str = Query(..., min_length=1),
    artifact_id: Optional[uuid.UUID] = Query(default=None),
    artifact_slug: Optional[str] = Query(default=None),
    path_glob: Optional[str] = Query(default=None),
    file_extensions: Optional[str] = Query(default=None),
    regex: bool = Query(default=False),
    case_sensitive: bool = Query(default=False),
    limit: int = Query(default=200, ge=1, le=2000),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db),
):
    row = _resolve_artifact(db, artifact_id=artifact_id, artifact_slug=artifact_slug)
    payload = await _artifact_bytes(row)
    resolved = _resolved_artifact_source_payload(row, payload)
    files = resolved["files"] if isinstance(resolved.get("files"), dict) else {}
    try:
        results = search_files(
            files=files,
            query=query,
            path_glob=path_glob,
            file_extensions=_normalize_extensions(file_extensions),
            regex=bool(regex),
            case_sensitive=bool(case_sensitive),
            limit=int(limit),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "artifact": _artifact_identity_payload(row),
        "source_mode": resolved.get("source_mode") or "packaged_fallback",
        "source_origin": resolved.get("source_origin") or "packaged_fallback",
        "resolution_branch": resolved.get("resolution_branch") or "packaged_fallback",
        "resolution_details": resolved.get("resolution_details") if isinstance(resolved.get("resolution_details"), dict) else {},
        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
        "resolved_source_roots": resolved.get("resolved_source_roots") or [],
        "warnings": resolved.get("warnings") or [],
        **results,
    }


@router.get("/artifacts/analyze-codebase")
async def analyze_artifact_codebase(
    artifact_id: Optional[uuid.UUID] = Query(default=None),
    artifact_slug: Optional[str] = Query(default=None),
    mode: str = Query(default="general"),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db),
):
    row = _resolve_artifact(db, artifact_id=artifact_id, artifact_slug=artifact_slug)
    payload = await _artifact_bytes(row)
    resolved = _resolved_artifact_source_payload(row, payload)
    files = resolved["files"] if isinstance(resolved.get("files"), dict) else {}
    resolved_mode = str(mode or "general").strip().lower() or "general"
    if resolved_mode not in {"general", "python_api"}:
        raise HTTPException(status_code=400, detail="Unsupported analysis mode")
    analysis = analyze_codebase(files, mode=resolved_mode)
    return {
        "artifact": _artifact_identity_payload(row),
        "source_mode": resolved.get("source_mode") or "packaged_fallback",
        "source_origin": resolved.get("source_origin") or "packaged_fallback",
        "resolution_branch": resolved.get("resolution_branch") or "packaged_fallback",
        "resolution_details": resolved.get("resolution_details") if isinstance(resolved.get("resolution_details"), dict) else {},
        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
        "resolved_source_roots": resolved.get("resolved_source_roots") or [],
        "warnings": resolved.get("warnings") or [],
        "analysis_version": "mvp.v1",
        **analysis,
    }


@router.get("/artifacts/analyze-python-api")
async def analyze_python_api_artifact(
    artifact_id: Optional[uuid.UUID] = Query(default=None),
    artifact_slug: Optional[str] = Query(default=None),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db),
):
    row = _resolve_artifact(db, artifact_id=artifact_id, artifact_slug=artifact_slug)
    payload = await _artifact_bytes(row)
    resolved = _resolved_artifact_source_payload(row, payload)
    files = resolved["files"] if isinstance(resolved.get("files"), dict) else {}
    analysis = analyze_codebase(files, mode="python_api")
    return {
        "artifact": _artifact_identity_payload(row),
        "source_mode": resolved.get("source_mode") or "packaged_fallback",
        "source_origin": resolved.get("source_origin") or "packaged_fallback",
        "resolution_branch": resolved.get("resolution_branch") or "packaged_fallback",
        "resolution_details": resolved.get("resolution_details") if isinstance(resolved.get("resolution_details"), dict) else {},
        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
        "resolved_source_roots": resolved.get("resolved_source_roots") or [],
        "warnings": resolved.get("warnings") or [],
        "analysis_version": "mvp.v1",
        **analysis,
    }


@router.get("/artifacts/module-metrics")
async def get_artifact_module_metrics(
    artifact_id: Optional[uuid.UUID] = Query(default=None),
    artifact_slug: Optional[str] = Query(default=None),
    top_n: int = Query(default=200, ge=1, le=2000),
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db),
):
    row = _resolve_artifact(db, artifact_id=artifact_id, artifact_slug=artifact_slug)
    payload = await _artifact_bytes(row)
    resolved = _resolved_artifact_source_payload(row, payload)
    files = resolved["files"] if isinstance(resolved.get("files"), dict) else {}
    rows = compute_module_metrics(files)
    return {
        "artifact": _artifact_identity_payload(row),
        "source_mode": resolved.get("source_mode") or "packaged_fallback",
        "source_origin": resolved.get("source_origin") or "packaged_fallback",
        "resolution_branch": resolved.get("resolution_branch") or "packaged_fallback",
        "resolution_details": resolved.get("resolution_details") if isinstance(resolved.get("resolution_details"), dict) else {},
        "provenance": resolved.get("provenance") if isinstance(resolved.get("provenance"), dict) else {},
        "resolved_source_roots": resolved.get("resolved_source_roots") or [],
        "warnings": resolved.get("warnings") or [],
        "count": len(rows),
        "items": rows[: int(top_n)],
    }


@router.get("/artifacts/{artifact_id}", response_model=schemas.Artifact)
async def get_artifact(
    artifact_id: uuid.UUID,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db)
):
    """Get artifact metadata.

    Args:
        artifact_id: Artifact UUID
        db: Database session

    Returns:
        Artifact metadata
    """
    artifact = db.query(models.Artifact).filter(
        models.Artifact.id == artifact_id
    ).first()

    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    return schemas.Artifact.from_orm_model(artifact)


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: uuid.UUID,
    principal: AccessPrincipal = Depends(require_capabilities(CAP_ARTIFACTS_READ)),
    db: Session = Depends(get_db)
):
    """Download artifact content.

    Args:
        artifact_id: Artifact UUID
        db: Database session

    Returns:
        Artifact file content
    """
    artifact = db.query(models.Artifact).filter(
        models.Artifact.id == artifact_id
    ).first()

    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    # Prefer direct file serving when available (local filesystem backend).
    artifact_path = artifact_store.get_path(artifact_id)
    if artifact_path and artifact_path.exists():
        return FileResponse(
            path=str(artifact_path),
            media_type=artifact.content_type,
            filename=artifact.name
        )

    # Fall back to backend retrieval (required for non-filesystem providers).
    payload = await artifact_store.retrieve(artifact_id)
    if payload is not None:
        return Response(
            content=payload,
            media_type=artifact.content_type or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{artifact.name}"'},
        )

    # Legacy fallback for artifact rows that persisted explicit local paths.
    if artifact.storage_path:
        artifact_path = Path(str(artifact.storage_path))
        if artifact_path.exists():
            return FileResponse(
                path=str(artifact_path),
                media_type=artifact.content_type,
                filename=artifact.name
            )
    raise HTTPException(status_code=404, detail="Artifact content not found")
