"""Artifact API endpoints"""
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, Response
from pathlib import Path
from sqlalchemy.orm import Session

from core.database import get_db
from core import models, schemas
from core.artifact_store import get_runtime_artifact_store

router = APIRouter()

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

    # Read file content
    content = await file.read()

    # Store in artifact store
    storage_path, sha256_hash = await artifact_store.store(
        artifact_id=artifact_id,
        content=content,
        compute_sha256=True
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
        byte_length=len(content),
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
        occurred_at=models.datetime.datetime.utcnow(),
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
    cursor: Optional[str] = None,
    workspace_id: Optional[uuid.UUID] = None,
    run_id: Optional[uuid.UUID] = None,
    kind: Optional[str] = None,
    storage_scope: Optional[str] = None,
    sync_state: Optional[str] = None,
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

    # Fetch limit + 1 to determine if there are more results
    artifacts = query.limit(limit + 1).all()

    # Determine next cursor
    next_cursor = None
    if len(artifacts) > limit:
        next_cursor = str(artifacts[limit - 1].id)
        artifacts = artifacts[:limit]

    # Convert to schema
    items = [schemas.Artifact.from_orm_model(a) for a in artifacts]

    return schemas.ArtifactListResponse(items=items, next_cursor=next_cursor)


@router.get("/artifacts/{artifact_id}", response_model=schemas.Artifact)
async def get_artifact(
    artifact_id: uuid.UUID,
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
