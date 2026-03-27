from __future__ import annotations

from typing import Any, Callable

from core.execution_notes import create_execution_note, update_execution_note


CreateNoteFn = Callable[..., Any]
UpdateNoteFn = Callable[..., Any]


def begin_stage_note(
    db: Any,
    *,
    workspace_id: Any,
    prompt_or_request: str,
    findings: list[str],
    root_cause: str,
    proposed_fix: str,
    implementation_summary: str,
    validation_summary: list[str],
    debt_recorded: list[str],
    related_artifact_ids: list[str],
    status: str,
    extra_metadata: dict[str, Any] | None = None,
    create_note: CreateNoteFn = create_execution_note,
) -> Any:
    return create_note(
        db,
        workspace_id=workspace_id,
        prompt_or_request=prompt_or_request,
        findings=findings,
        root_cause=root_cause,
        proposed_fix=proposed_fix,
        implementation_summary=implementation_summary,
        validation_summary=validation_summary,
        debt_recorded=debt_recorded,
        related_artifact_ids=related_artifact_ids,
        status=status,
        extra_metadata=extra_metadata or {},
    )


def record_stage_metadata(
    db: Any,
    *,
    artifact_id: Any,
    implementation_summary: str | None = None,
    validation_summary: list[str] | None = None,
    append_validation: list[str] | None = None,
    related_artifact_ids: list[str] | None = None,
    extra_metadata_updates: dict[str, Any] | None = None,
    status: str | None = None,
    update_note: UpdateNoteFn = update_execution_note,
) -> Any:
    kwargs: dict[str, Any] = {"artifact_id": artifact_id}
    if implementation_summary is not None:
        kwargs["implementation_summary"] = implementation_summary
    if validation_summary is not None:
        kwargs["validation_summary"] = validation_summary
    if append_validation is not None:
        kwargs["append_validation"] = append_validation
    if related_artifact_ids is not None:
        kwargs["related_artifact_ids"] = related_artifact_ids
    if extra_metadata_updates is not None:
        kwargs["extra_metadata_updates"] = extra_metadata_updates
    if status is not None:
        kwargs["status"] = status
    return update_note(db, **kwargs)


def finalize_stage_note(
    db: Any,
    *,
    artifact_id: Any,
    implementation_summary: str | None = None,
    validation_summary: list[str] | None = None,
    append_validation: list[str] | None = None,
    related_artifact_ids: list[str] | None = None,
    extra_metadata_updates: dict[str, Any] | None = None,
    status: str | None = None,
    update_note: UpdateNoteFn = update_execution_note,
) -> Any:
    return record_stage_metadata(
        db,
        artifact_id=artifact_id,
        implementation_summary=implementation_summary,
        validation_summary=validation_summary,
        append_validation=append_validation,
        related_artifact_ids=related_artifact_ids,
        extra_metadata_updates=extra_metadata_updates,
        status=status,
        update_note=update_note,
    )


def record_stage_failure(
    db: Any,
    *,
    artifact_id: Any,
    job_type: str,
    error: Exception | str,
    update_note: UpdateNoteFn = update_execution_note,
) -> Any:
    return update_note(
        db,
        artifact_id=artifact_id,
        implementation_summary=f"Execution stopped during job type={job_type}.",
        append_validation=[f"Failure during {job_type}: {error}"],
        status="failed",
    )


def resolve_execution_note_artifact_id(*payloads: Any) -> str:
    for payload in payloads:
        if isinstance(payload, dict):
            resolved = str(payload.get("execution_note_artifact_id") or "").strip()
            if resolved:
                return resolved
    return ""

