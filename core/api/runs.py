"""Run API endpoints"""
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from sqlalchemy.orm import Session

from core.database import get_db
from core import models, schemas
from core.executor import SimpleExecutor
from core.runtime_contract import RunPayloadV1
from core.runtime_execution import continue_blocked_run, read_run_artifact_content, request_pause_run, retry_runtime_run, submit_runtime_run

router = APIRouter()


@router.post("/runtime/runs", response_model=schemas.Run, status_code=201)
async def submit_runtime_execution_run(
    payload: RunPayloadV1,
    db: Session = Depends(get_db),
):
    """Submit a typed Epic C runtime run for deterministic worker execution."""
    run = submit_runtime_run(db, payload, actor="runtime_submission")
    db.commit()
    db.refresh(run)
    return schemas.Run.from_orm_model(run)


@router.post("/runs", response_model=schemas.Run, status_code=201)
async def create_run(
    run_request: schemas.RunCreateRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    """Create and optionally execute a new run.

    Args:
        run_request: Run creation request
        request: FastAPI request (for correlation ID)
        db: Database session

    Returns:
        Created run
    """
    executor = SimpleExecutor(db)

    # Get correlation ID from request state (set by middleware)
    correlation_id = getattr(request.state, "correlation_id", None)

    # For v0, we create and execute a simple demo run with log capture
    run = await executor.execute_simple_run(
        name=run_request.name,
        inputs=run_request.inputs,
        simulate_failure=False,
        correlation_id=correlation_id
    )

    return schemas.Run.from_orm_model(run)


@router.get("/runs", response_model=schemas.RunListResponse)
async def list_runs(
    limit: int = Query(50, ge=1, le=500),
    cursor: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List runs with optional filtering and pagination.

    Args:
        limit: Maximum number of runs to return (1-500)
        cursor: Pagination cursor (run ID to start after)
        status: Filter by status
        db: Database session

    Returns:
        List of runs with optional next cursor
    """
    query = db.query(models.Run)

    # Apply filters
    if status:
        try:
            status_enum = models.RunStatus(status)
            query = query.filter(models.Run.status == status_enum)
        except ValueError:
            pass  # Invalid status, ignore

    # Order by created_at descending, then id descending for stable ordering
    query = query.order_by(models.Run.created_at.desc(), models.Run.id.desc())

    # Apply cursor pagination
    if cursor:
        try:
            cursor_id = uuid.UUID(cursor)
            # Find the cursor run to get its created_at
            cursor_run = db.query(models.Run).filter(models.Run.id == cursor_id).first()
            if cursor_run:
                # Filter to runs created before the cursor, or same timestamp with lower ID
                query = query.filter(
                    (models.Run.created_at < cursor_run.created_at) |
                    ((models.Run.created_at == cursor_run.created_at) & (models.Run.id < cursor_id))
                )
        except ValueError:
            pass  # Invalid cursor, ignore

    # Fetch limit + 1 to determine if there are more results
    runs = query.limit(limit + 1).all()

    # Determine next cursor
    next_cursor = None
    if len(runs) > limit:
        next_cursor = str(runs[limit - 1].id)
        runs = runs[:limit]

    # Convert to schema
    items = [schemas.Run.from_orm_model(r) for r in runs]

    return schemas.RunListResponse(items=items, next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=schemas.Run)
async def get_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db)
):
    """Get a specific run by ID.

    Args:
        run_id: Run UUID
        db: Database session

    Returns:
        Run details
    """
    run = db.query(models.Run).filter(models.Run.id == run_id).first()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    return schemas.Run.from_orm_model(run)


@router.post("/runs/{run_id}/cancel", response_model=schemas.Run)
async def cancel_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db)
):
    """Cancel a running run.

    Args:
        run_id: Run UUID
        db: Database session

    Returns:
        Updated run
    """
    run = db.query(models.Run).filter(models.Run.id == run_id).first()

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run.status not in [models.RunStatus.CREATED, models.RunStatus.RUNNING]:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot cancel run in status {run.status.value}"
        )

    run.status = models.RunStatus.CANCELLED
    db.commit()
    db.refresh(run)

    return schemas.Run.from_orm_model(run)


@router.post("/runs/{run_id}/pause", response_model=schemas.Run)
async def pause_runtime_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    run = db.query(models.Run).filter(models.Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    run = request_pause_run(db, run_id, actor="api")
    db.commit()
    db.refresh(run)
    return schemas.Run.from_orm_model(run)


@router.post("/runs/{run_id}/continue", response_model=schemas.Run)
async def continue_runtime_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    run = db.query(models.Run).filter(models.Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != models.RunStatus.BLOCKED:
        raise HTTPException(status_code=409, detail="Run is not blocked")
    run = continue_blocked_run(db, run_id, actor="api")
    db.commit()
    db.refresh(run)
    return schemas.Run.from_orm_model(run)


@router.post("/runs/{run_id}/retry", response_model=schemas.Run, status_code=201)
async def retry_run(
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    run = db.query(models.Run).filter(models.Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in {models.RunStatus.FAILED, models.RunStatus.BLOCKED, models.RunStatus.COMPLETED, models.RunStatus.CANCELLED}:
        raise HTTPException(status_code=409, detail="Run is not retryable")
    new_run = retry_runtime_run(db, run_id, actor="api_retry")
    db.commit()
    db.refresh(new_run)
    return schemas.Run.from_orm_model(new_run)


@router.get("/runs/{run_id}/steps", response_model=list[schemas.Step])
async def list_run_steps(
    run_id: uuid.UUID,
    db: Session = Depends(get_db)
):
    """List all steps for a run.

    Args:
        run_id: Run UUID
        db: Database session

    Returns:
        List of steps ordered by index
    """
    # Verify run exists
    run = db.query(models.Run).filter(models.Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    steps = db.query(models.Step).filter(
        models.Step.run_id == run_id
    ).order_by(models.Step.idx).all()

    return [schemas.Step.from_orm_model(s) for s in steps]


@router.get("/runs/{run_id}/steps/{step_id}", response_model=schemas.Step)
async def get_step(
    run_id: uuid.UUID,
    step_id: uuid.UUID,
    db: Session = Depends(get_db)
):
    """Get a specific step.

    Args:
        run_id: Run UUID
        step_id: Step UUID
        db: Database session

    Returns:
        Step details
    """
    step = db.query(models.Step).filter(
        models.Step.id == step_id,
        models.Step.run_id == run_id
    ).first()

    if not step:
        raise HTTPException(status_code=404, detail="Step not found")

    return schemas.Step.from_orm_model(step)


@router.get("/runs/{run_id}/artifacts", response_model=list[schemas.Artifact])
async def list_run_artifacts(
    run_id: uuid.UUID,
    db: Session = Depends(get_db)
):
    """List artifacts for a run ordered by creation time."""
    run = db.query(models.Run).filter(models.Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    artifacts = db.query(models.Artifact).filter(
        models.Artifact.run_id == run_id
    ).order_by(models.Artifact.created_at.asc(), models.Artifact.id.asc()).all()

    return [schemas.Artifact.from_orm_model(artifact) for artifact in artifacts]


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
async def get_run_artifact_content(
    run_id: uuid.UUID,
    artifact_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    run = db.query(models.Run).filter(models.Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        payload = read_run_artifact_content(db, run_id, artifact_id)
        return {**payload, "run_id": str(run_id)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Artifact content not found")
