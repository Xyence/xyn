"""Epic C runtime execution services built on the existing run/event tables."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from sqlalchemy.orm import Session

from core import models
from core.artifact_store import get_runtime_artifact_store
from core.context_packs import default_instance_workspace_root
from core.runtime_contract import (
    AllowedRequestedOutput,
    RunPayloadV1,
    WorkerArtifactDescriptor,
    WorkerInfoPayload,
    WorkerRunSnapshot,
)
from core.runtime_events import publish_runtime_event
from core.runtime_workers import (
    DEFAULT_HEALTH_WINDOW_SECONDS,
    heartbeat as heartbeat_registered_worker,
    get_worker_driver,
    heartbeat as heartbeat_worker,
    list_healthy_workers,
    mark_worker_busy,
    mark_worker_idle,
    register_worker,
)


RUN_TERMINAL_STATUSES = {
    models.RunStatus.COMPLETED,
    models.RunStatus.FAILED,
    models.RunStatus.BLOCKED,
    models.RunStatus.CANCELLED,
}
STEP_TERMINAL_STATUSES = {
    models.StepStatus.COMPLETED,
    models.StepStatus.FAILED,
}
RUN_STATUS_TRANSITIONS = {
    models.RunStatus.QUEUED: {models.RunStatus.RUNNING, models.RunStatus.BLOCKED, models.RunStatus.CANCELLED},
    models.RunStatus.RUNNING: {models.RunStatus.COMPLETED, models.RunStatus.FAILED, models.RunStatus.BLOCKED, models.RunStatus.CANCELLED, models.RunStatus.QUEUED},
    models.RunStatus.FAILED: {models.RunStatus.QUEUED},
    models.RunStatus.BLOCKED: {models.RunStatus.QUEUED},
    models.RunStatus.COMPLETED: set(),
    models.RunStatus.CANCELLED: set(),
    models.RunStatus.CREATED: {models.RunStatus.RUNNING, models.RunStatus.CANCELLED},
}
STEP_STATUS_TRANSITIONS = {
    models.StepStatus.QUEUED: {models.StepStatus.RUNNING, models.StepStatus.FAILED},
    models.StepStatus.CREATED: {models.StepStatus.RUNNING, models.StepStatus.FAILED},
    models.StepStatus.RUNNING: {models.StepStatus.COMPLETED, models.StepStatus.FAILED},
    models.StepStatus.COMPLETED: set(),
    models.StepStatus.FAILED: set(),
    models.StepStatus.SKIPPED: set(),
}
ALLOWED_ARTIFACT_TYPES = {"patch", "log", "report", "code", "summary"}


def _runtime_artifact_root() -> Path:
    # Legacy fallback path for artifacts written before runtime store abstraction.
    root = Path(default_instance_workspace_root()).resolve() / "runtime_runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_json(payload: Dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _artifact_uri(run_id: uuid.UUID, artifact_id: uuid.UUID, file_name: Optional[str] = None) -> str:
    if file_name:
        return f"artifact://runs/{run_id}/{file_name}"
    return f"artifact://runs/{run_id}/artifacts/{artifact_id}"


def _runtime_controls(run: models.Run) -> Dict[str, Any]:
    policy = dict(run.execution_policy or {})
    controls = policy.get("runtime_controls") if isinstance(policy.get("runtime_controls"), dict) else {}
    return dict(controls or {})


def _set_runtime_controls(run: models.Run, controls: Dict[str, Any]) -> None:
    policy = dict(run.execution_policy or {})
    policy["runtime_controls"] = dict(controls or {})
    run.execution_policy = policy


def pause_requested(run: models.Run) -> bool:
    return bool(_runtime_controls(run).get("pause_requested"))


def create_runtime_run(db: Session, payload: RunPayloadV1, *, actor: str = "runtime") -> models.Run:
    run = models.Run(
        id=uuid.UUID(payload.run_id),
        name=payload.prompt.title,
        status=models.RunStatus.QUEUED,
        actor=actor,
        correlation_id=str(uuid.uuid4()),
        inputs={"runtime_execution": True, "prompt": payload.prompt.model_dump()},
        queued_at=datetime.utcnow(),
        run_at=datetime.utcnow(),
        attempt=0,
        max_attempts=payload.policy.max_retries,
        work_item_id=payload.work_item_id,
        worker_type=payload.worker_type,
        prompt_payload=payload.model_dump(mode="json"),
        execution_policy=payload.policy.model_dump(mode="json"),
    )
    db.add(run)
    db.flush()
    return run


def submit_runtime_run(db: Session, payload: RunPayloadV1, *, actor: str = "runtime") -> models.Run:
    existing = db.query(models.Run).filter(models.Run.id == uuid.UUID(payload.run_id)).first()
    if existing is not None:
        RunPayloadV1.model_validate(existing.prompt_payload or {})
        return existing
    run = create_runtime_run(db, payload, actor=actor)
    db.flush()
    return run


def register_runtime_worker(db: Session, worker_info: WorkerInfoPayload) -> models.RuntimeWorker:
    return register_worker(db, worker_info)


def transition_run_status(
    db: Session,
    run: models.Run,
    next_status: models.RunStatus,
    *,
    summary: Optional[str] = None,
    failure_reason: Optional[str] = None,
    escalation_reason: Optional[str] = None,
) -> models.Run:
    allowed = RUN_STATUS_TRANSITIONS.get(run.status, set())
    if next_status not in allowed:
        raise ValueError(f"invalid run status transition: {run.status.value} -> {next_status.value}")
    run.status = next_status
    if next_status == models.RunStatus.RUNNING and run.started_at is None:
        run.started_at = datetime.utcnow()
    if next_status in RUN_TERMINAL_STATUSES:
        run.completed_at = datetime.utcnow()
    if summary is not None:
        run.summary = summary
    if failure_reason is not None:
        run.failure_reason = failure_reason
    if escalation_reason is not None:
        run.escalation_reason = escalation_reason
    db.flush()
    return run


def transition_step_status(
    db: Session,
    step: models.Step,
    next_status: models.StepStatus,
    *,
    summary: Optional[str] = None,
) -> models.Step:
    allowed = STEP_STATUS_TRANSITIONS.get(step.status, set())
    if next_status not in allowed:
        raise ValueError(f"invalid step status transition: {step.status.value} -> {next_status.value}")
    step.status = next_status
    if next_status == models.StepStatus.RUNNING and step.started_at is None:
        step.started_at = datetime.utcnow()
    if next_status in STEP_TERMINAL_STATUSES:
        step.completed_at = datetime.utcnow()
    if summary is not None:
        step.summary = summary
    db.flush()
    return step


def dispatch_queued_run(
    db: Session,
    *,
    now: Optional[datetime] = None,
    heartbeat_timeout_seconds: int = DEFAULT_HEALTH_WINDOW_SECONDS,
) -> Optional[models.Run]:
    current = now or datetime.utcnow()
    queued_runs = db.query(models.Run).filter(
        models.Run.status == models.RunStatus.QUEUED,
        models.Run.worker_type.isnot(None),
    ).order_by(
        models.Run.priority.asc(),
        models.Run.queued_at.asc().nulls_last(),
        models.Run.created_at.asc(),
        models.Run.id.asc(),
    ).all()
    for run in queued_runs:
        payload = RunPayloadV1.model_validate(run.prompt_payload or {})
        driver = get_worker_driver(run.worker_type or "")
        if driver is None or not driver.can_accept(payload):
            continue
        workers = list_healthy_workers(
            db,
            run.worker_type or "",
            now=current,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        )
        worker = next((item for item in workers if item.status == models.RuntimeWorkerStatus.IDLE), None)
        if worker is None:
            continue
        transition_run_status(db, run, models.RunStatus.RUNNING)
        run.worker_id = worker.worker_id
        run.heartbeat_at = current
        run.locked_at = current
        run.lease_expires_at = current + timedelta(seconds=heartbeat_timeout_seconds)
        run.attempt_count = (run.attempt_count or 0) + 1
        mark_worker_busy(db, worker.worker_id, run.id)
        accepted = driver.start_run(payload)
        publish_runtime_event(
            db,
            event_name="run.started",
            run_id=run.id,
            actor=worker.worker_id,
            correlation_id=run.correlation_id,
            data={
                "run_id": str(run.id),
                "work_item_id": run.work_item_id,
                "worker_id": worker.worker_id,
                "worker_type": run.worker_type,
                "accepted": accepted.accepted,
            },
        )
        db.flush()
        return run
    return None


def record_run_heartbeat(db: Session, worker_id: str, run_id: uuid.UUID) -> Optional[models.Run]:
    worker = heartbeat_worker(db, worker_id)
    run = db.query(models.Run).filter(models.Run.id == run_id, models.Run.locked_by == worker_id).first()
    if worker is None or run is None:
        return None
    now = datetime.utcnow()
    run.heartbeat_at = now
    run.lease_expires_at = now + timedelta(seconds=DEFAULT_HEALTH_WINDOW_SECONDS)
    publish_runtime_event(
        db,
        event_name="run.heartbeat",
        run_id=run.id,
        actor=worker_id,
        correlation_id=run.correlation_id,
        data={"heartbeat_at": now.isoformat(), "status": run.status.value},
    )
    db.flush()
    return run


def report_run_step(
    db: Session,
    *,
    run_id: uuid.UUID,
    step_key: str,
    label: str,
    status: str,
    sequence_no: int,
    summary: Optional[str] = None,
) -> models.Step:
    step = db.query(models.Step).filter(
        models.Step.run_id == run_id,
        models.Step.step_key == step_key,
    ).order_by(models.Step.idx.desc(), models.Step.created_at.desc()).first()
    desired = models.StepStatus(status)
    if step is None or (
        desired == models.StepStatus.RUNNING
        and step.status in STEP_TERMINAL_STATUSES
    ):
        latest_idx = db.query(models.Step).filter(models.Step.run_id == run_id).order_by(models.Step.idx.desc()).first()
        next_idx = (latest_idx.idx + 1) if latest_idx is not None else sequence_no
        step = models.Step(
            id=uuid.uuid4(),
            run_id=run_id,
            name=step_key,
            step_key=step_key,
            label=label,
            idx=next_idx,
            kind="runtime",
            status=models.StepStatus.QUEUED,
            created_at=datetime.utcnow(),
        )
        db.add(step)
        db.flush()
    if step.status != desired:
        transition_step_status(db, step, desired, summary=summary)
    elif summary is not None:
        step.summary = summary
        db.flush()
    event_name = "run.step.started" if desired == models.StepStatus.RUNNING else "run.step.completed" if desired == models.StepStatus.COMPLETED else "run.step.failed"
    publish_runtime_event(
        db,
        event_name=event_name,
        run_id=run_id,
        step_id=step.id,
        data={
            "step_id": str(step.id),
            "step_key": step_key,
            "label": label,
            "status": desired.value,
            "sequence_no": sequence_no,
            "summary": summary,
        },
    )
    return step


def capture_run_artifact(
    db: Session,
    *,
    run_id: uuid.UUID,
    artifact_type: str,
    label: str,
    metadata_json: Optional[Dict[str, Any]] = None,
    content: Optional[Dict[str, Any] | str] = None,
    file_name: Optional[str] = None,
) -> models.Artifact:
    if artifact_type not in ALLOWED_ARTIFACT_TYPES:
        raise ValueError(f"unknown artifact type: {artifact_type}")
    artifact_id = uuid.uuid4()
    uri = _artifact_uri(run_id, artifact_id, file_name=file_name)
    if isinstance(content, dict):
        payload_bytes = _serialize_json(content)
        content_type = "application/json"
    else:
        text_content = "" if content is None else str(content)
        payload_bytes = text_content.encode("utf-8")
        content_type = "text/plain"
    artifact_store = get_runtime_artifact_store()
    storage_key, sha256_hash = artifact_store.store_bytes(artifact_id=artifact_id, content=payload_bytes, compute_sha256=True)
    artifact = models.Artifact(
        id=artifact_id,
        run_id=run_id,
        name=label,
        kind=artifact_type,
        content_type=content_type,
        byte_length=len(payload_bytes),
        sha256=sha256_hash,
        created_by="runtime",
        extra_metadata={
            **dict(metadata_json or {}),
            "storage_key": storage_key,
            "storage_provider": artifact_store.provider,
            "runtime_artifact_uri": uri,
        },
        storage_path=uri,
        created_at=datetime.utcnow(),
    )
    db.add(artifact)
    db.flush()
    publish_runtime_event(
        db,
        event_name="run.artifact.created",
        run_id=run_id,
        data={
            "artifact_id": str(artifact.id),
            "artifact_type": artifact_type,
            "uri": uri,
            "label": label,
        },
    )
    return artifact


def complete_run(
    db: Session,
    run_id: uuid.UUID,
    *,
    summary: Optional[str] = None,
) -> models.Run:
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    transition_run_status(db, run, models.RunStatus.COMPLETED, summary=summary)
    if run.worker_id:
        mark_worker_idle(db, run.worker_id)
    publish_runtime_event(
        db,
        event_name="run.completed",
        run_id=run.id,
        actor=run.worker_id or "runtime",
        correlation_id=run.correlation_id,
        data={"summary": summary},
    )
    return run


def request_pause_run(
    db: Session,
    run_id: uuid.UUID,
    *,
    actor: str = "conversation",
) -> models.Run:
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    controls = _runtime_controls(run)
    controls["pause_requested"] = True
    controls["pause_requested_by"] = actor
    controls["pause_requested_at"] = _utcnow().isoformat()
    _set_runtime_controls(run, controls)
    if run.status == models.RunStatus.QUEUED:
        transition_run_status(
            db,
            run,
            models.RunStatus.BLOCKED,
            summary="Run paused before execution.",
            escalation_reason="paused_by_user",
        )
        publish_runtime_event(
            db,
            event_name="run.blocked",
            run_id=run.id,
            actor=actor,
            correlation_id=run.correlation_id,
            data={"summary": run.summary, "escalation_reason": run.escalation_reason},
        )
    else:
        run.summary = "Pause requested. Run will block at the next safe checkpoint."
        db.flush()
    return run


def continue_blocked_run(
    db: Session,
    run_id: uuid.UUID,
    *,
    actor: str = "conversation",
) -> models.Run:
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    controls = _runtime_controls(run)
    controls["pause_requested"] = False
    controls["pause_requested_by"] = actor
    _set_runtime_controls(run, controls)
    transition_run_status(db, run, models.RunStatus.QUEUED, summary="Run re-queued from conversation hold.")
    run.completed_at = None
    run.started_at = None
    run.worker_id = None
    run.locked_by = None
    run.locked_at = None
    run.lease_expires_at = None
    run.heartbeat_at = None
    run.failure_reason = None
    run.escalation_reason = None
    run.queued_at = _utcnow()
    db.flush()
    return run


def retry_runtime_run(
    db: Session,
    run_id: uuid.UUID,
    *,
    actor: str = "runtime_retry",
) -> models.Run:
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    payload = RunPayloadV1.model_validate(run.prompt_payload or {})
    cloned = payload.model_copy(update={"run_id": str(uuid.uuid4())})
    return submit_runtime_run(db, cloned, actor=actor)


def fail_run(
    db: Session,
    run_id: uuid.UUID,
    *,
    summary: Optional[str] = None,
    failure_reason: Optional[str] = None,
    escalation_reason: Optional[str] = None,
) -> models.Run:
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    target_status = models.RunStatus.BLOCKED if escalation_reason else models.RunStatus.FAILED
    transition_run_status(
        db,
        run,
        target_status,
        summary=summary,
        failure_reason=failure_reason,
        escalation_reason=escalation_reason,
    )
    if run.worker_id:
        mark_worker_idle(db, run.worker_id)
    publish_runtime_event(
        db,
        event_name="run.blocked" if target_status == models.RunStatus.BLOCKED else "run.failed",
        run_id=run.id,
        actor=run.worker_id or "runtime",
        correlation_id=run.correlation_id,
        data={
            "summary": summary,
            "failure_reason": failure_reason,
            "escalation_reason": escalation_reason,
        },
    )
    return run


def handle_stale_heartbeats(
    db: Session,
    *,
    now: Optional[datetime] = None,
    stale_after_seconds: int = 30,
) -> list[models.Run]:
    current = now or datetime.utcnow()
    threshold = current - timedelta(seconds=stale_after_seconds)
    runs = db.query(models.Run).filter(
        models.Run.status == models.RunStatus.RUNNING,
        models.Run.heartbeat_at.isnot(None),
        models.Run.heartbeat_at < threshold,
    ).all()
    updated: list[models.Run] = []
    for run in runs:
        policy = dict(run.execution_policy or {})
        max_retries = int(policy.get("max_retries") or 0)
        current_attempt = max(int(run.attempt_count or 0), 1)
        require_review = bool(policy.get("require_human_review_on_failure"))
        failure_summary = run.summary or "Worker heartbeat timed out."
        run.failure_reason = "worker_unresponsive"
        publish_runtime_event(
            db,
            event_name="run.failed",
            run_id=run.id,
            actor=run.worker_id or "runtime",
            correlation_id=run.correlation_id,
            data={
                "failure_reason": "worker_unresponsive",
                "retry_eligible": current_attempt <= max_retries,
            },
        )
        if run.worker_id:
            mark_worker_idle(db, run.worker_id)
        if current_attempt <= max_retries:
            run.status = models.RunStatus.QUEUED
            run.summary = failure_summary
            run.queued_at = current
            run.heartbeat_at = None
            run.locked_by = None
            run.locked_at = None
            run.lease_expires_at = None
            updated.append(run)
            continue
        if require_review:
            run.status = models.RunStatus.BLOCKED
            run.completed_at = current
            run.summary = failure_summary
            run.escalation_reason = "human_review_required"
            publish_runtime_event(
                db,
                event_name="run.blocked",
                run_id=run.id,
                actor="runtime",
                correlation_id=run.correlation_id,
                data={"failure_reason": "worker_unresponsive"},
            )
        else:
            run.status = models.RunStatus.FAILED
            run.completed_at = current
            run.summary = failure_summary
        updated.append(run)
    db.flush()
    return updated


def snapshot_run(db: Session, run_id: uuid.UUID) -> WorkerRunSnapshot:
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    return WorkerRunSnapshot(
        run_id=str(run.id),
        status=run.status.value,
        worker_id=run.worker_id,
        heartbeat_at=run.heartbeat_at,
        summary=run.summary,
        failure_reason=run.failure_reason,
        escalation_reason=run.escalation_reason,
    )


def collect_run_artifact_descriptors(db: Session, run_id: uuid.UUID) -> list[WorkerArtifactDescriptor]:
    rows = db.query(models.Artifact).filter(models.Artifact.run_id == run_id).order_by(models.Artifact.created_at.asc()).all()
    return [
        WorkerArtifactDescriptor(
            artifact_type=row.kind,
            uri=str(row.storage_path or ""),
            label=row.name,
            metadata_json=dict(row.extra_metadata or {}),
        )
        for row in rows
    ]


def read_run_artifact_content(db: Session, run_id: uuid.UUID, artifact_id: uuid.UUID) -> Dict[str, Any]:
    row = db.query(models.Artifact).filter(models.Artifact.id == artifact_id, models.Artifact.run_id == run_id).one()
    uri = str(row.storage_path or "")
    artifact_store = get_runtime_artifact_store()
    payload = artifact_store.retrieve_bytes(artifact_id=artifact_id)
    if payload is None:
        # Legacy fallback for artifacts persisted before runtime backend abstraction.
        root = _runtime_artifact_root() / str(run_id) / "artifacts"
        file_name = uri.split(f"artifact://runs/{run_id}/", 1)[1] if uri.startswith(f"artifact://runs/{run_id}/") else ""
        path = (root / file_name).resolve() if file_name else None
        if path is None or not path.exists():
            raise FileNotFoundError(uri)
        payload = path.read_bytes()
    return {
        "artifact_id": str(row.id),
        "artifact_type": row.kind,
        "label": row.name,
        "uri": uri,
        "content_type": row.content_type,
        "content": payload.decode("utf-8", errors="replace"),
    }


def execute_assigned_run(db: Session, run_id: uuid.UUID, *, executor=None, validation_runner=None) -> models.Run:
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    driver = get_worker_driver(run.worker_type or "")
    if driver is None or not hasattr(driver, "execute_assigned_run"):
        raise RuntimeError(f"no execution worker registered for worker_type={run.worker_type!r}")
    return driver.execute_assigned_run(
        db,
        run.id,
        run.worker_id,
        executor=executor,
        validation_runner=validation_runner,
    )


def process_runtime_cycle(
    db: Session,
    *,
    worker_id: Optional[str] = None,
    executor=None,
    validation_runner=None,
    now: Optional[datetime] = None,
    stale_after_seconds: int = 30,
    heartbeat_timeout_seconds: int = DEFAULT_HEALTH_WINDOW_SECONDS,
) -> Dict[str, Any]:
    current = now or datetime.utcnow()
    if worker_id:
        heartbeat_registered_worker(db, worker_id)
    stale_runs = handle_stale_heartbeats(db, now=current, stale_after_seconds=stale_after_seconds)
    dispatched = dispatch_queued_run(
        db,
        now=current,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
    )
    executed = None
    if dispatched is not None:
        executed = execute_assigned_run(
            db,
            dispatched.id,
            executor=executor,
            validation_runner=validation_runner,
        )
    db.commit()
    return {
        "stale_run_ids": [str(run.id) for run in stale_runs],
        "dispatched_run_id": str(dispatched.id) if dispatched is not None else None,
        "executed_run_id": str(executed.id) if executed is not None else None,
    }
