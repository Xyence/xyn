"""Pydantic schemas (DTOs) for Xyn Seed v0.0 API"""
from datetime import datetime
from typing import Optional, Dict, Any, List
from uuid import UUID
from pydantic import BaseModel, Field
from core.artifact_provenance import extract_provenance_metadata, merge_provenance_metadata


# Health Schemas
class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    version: str
    uptime_seconds: int
    now: datetime


# Event Schemas
class EventResource(BaseModel):
    """Event resource reference."""
    type: str
    id: str


class Event(BaseModel):
    """Event response model."""
    event_id: UUID = Field(alias="id")
    event_name: str
    occurred_at: datetime
    env_id: str
    actor: str
    correlation_id: Optional[str] = None
    run_id: Optional[UUID] = None
    step_id: Optional[UUID] = None
    resource: Optional[EventResource] = None
    data: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_orm_model(cls, event):
        """Convert ORM model to schema."""
        resource = None
        if event.resource_type and event.resource_id:
            resource = EventResource(type=event.resource_type, id=event.resource_id)

        return cls(
            id=event.id,
            event_name=event.event_name,
            occurred_at=event.occurred_at,
            env_id=event.env_id,
            actor=event.actor,
            correlation_id=event.correlation_id,
            run_id=event.run_id,
            step_id=event.step_id,
            resource=resource,
            data=event.data or {}
        )


class EventListResponse(BaseModel):
    """Event list response with pagination."""
    items: List[Event]
    next_cursor: Optional[str] = None


class EmitEventRequest(BaseModel):
    """Request to emit a new event."""
    event_name: str
    resource: Optional[EventResource] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    run_id: Optional[UUID] = None
    step_id: Optional[UUID] = None


# Run Schemas
class RunCreateRequest(BaseModel):
    """Request to create a new run."""
    name: str
    blueprint_ref: Optional[str] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    priority: int = 0


class Run(BaseModel):
    """Run response model."""
    run_id: UUID = Field(alias="id")
    name: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    heartbeat_at: Optional[datetime] = None
    actor: str
    correlation_id: str
    work_item_id: Optional[str] = None
    worker_type: Optional[str] = None
    worker_id: Optional[str] = None
    prompt_payload: Dict[str, Any] = Field(default_factory=dict)
    execution_policy: Dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = 0
    summary: Optional[str] = None
    escalation_reason: Optional[str] = None
    failure_reason: Optional[str] = None
    inputs: Dict[str, Any] = Field(default_factory=dict)
    outputs: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_orm_model(cls, run):
        """Convert ORM model to schema."""
        return cls(
            id=run.id,
            name=run.name,
            status=run.status.value,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            heartbeat_at=getattr(run, "heartbeat_at", None),
            actor=run.actor,
            correlation_id=run.correlation_id,
            work_item_id=getattr(run, "work_item_id", None),
            worker_type=getattr(run, "worker_type", None),
            worker_id=getattr(run, "worker_id", None),
            prompt_payload=getattr(run, "prompt_payload", {}) or {},
            execution_policy=getattr(run, "execution_policy", {}) or {},
            attempt_count=getattr(run, "attempt_count", getattr(run, "attempt", 0)) or 0,
            summary=getattr(run, "summary", None),
            escalation_reason=getattr(run, "escalation_reason", None),
            failure_reason=getattr(run, "failure_reason", None),
            inputs=run.inputs or {},
            outputs=run.outputs,
            error=run.error
        )


class RunListResponse(BaseModel):
    """Run list response with pagination."""
    items: List[Run]
    next_cursor: Optional[str] = None


# Step Schemas
class Step(BaseModel):
    """Step response model."""
    step_id: UUID = Field(alias="id")
    run_id: UUID
    name: str
    step_key: Optional[str] = None
    label: Optional[str] = None
    status: str
    summary: Optional[str] = None
    sequence_no: int
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    logs_artifact_id: Optional[UUID] = None
    inputs: Optional[Dict[str, Any]] = None
    outputs: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_orm_model(cls, step):
        """Convert ORM model to schema."""
        return cls(
            id=step.id,
            run_id=step.run_id,
            name=step.name,
            step_key=getattr(step, "step_key", None),
            label=getattr(step, "label", None),
            status=step.status.value,
            summary=getattr(step, "summary", None),
            sequence_no=getattr(step, "sequence_no", step.idx),
            started_at=step.started_at,
            completed_at=step.completed_at,
            logs_artifact_id=step.logs_artifact_id,
            inputs=step.inputs,
            outputs=step.outputs,
            error=step.error
        )


# Artifact Schemas
class ArtifactCreateRequest(BaseModel):
    """Request to create an artifact."""
    name: str
    kind: str  # log, report, bundle, file
    content_type: str
    byte_length: Optional[int] = None
    run_id: Optional[UUID] = None
    step_id: Optional[UUID] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Artifact(BaseModel):
    """Artifact response model."""
    artifact_id: UUID = Field(alias="id")
    workspace_id: Optional[UUID] = None
    name: str
    kind: str
    artifact_type: str
    uri: Optional[str] = None
    label: Optional[str] = None
    storage_scope: str
    sync_state: str
    content_type: str
    byte_length: Optional[int] = None
    created_at: datetime
    created_by: str
    run_id: Optional[UUID] = None
    step_id: Optional[UUID] = None
    sha256: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_orm_model(cls, artifact):
        """Convert ORM model to schema."""
        metadata = merge_provenance_metadata(artifact.extra_metadata or {})
        provenance = extract_provenance_metadata(metadata)
        return cls(
            id=artifact.id,
            workspace_id=artifact.workspace_id,
            name=artifact.name,
            kind=artifact.kind,
            artifact_type=getattr(artifact, "artifact_type", artifact.kind),
            uri=getattr(artifact, "uri", getattr(artifact, "storage_path", None)),
            label=getattr(artifact, "label", artifact.name),
            storage_scope=getattr(artifact, "storage_scope", "instance-local"),
            sync_state=getattr(artifact, "sync_state", "local"),
            content_type=artifact.content_type,
            byte_length=artifact.byte_length,
            created_at=artifact.created_at,
            created_by=artifact.created_by,
            run_id=artifact.run_id,
            step_id=artifact.step_id,
            sha256=artifact.sha256,
            metadata=metadata,
            provenance=provenance,
        )


class ArtifactListResponse(BaseModel):
    """Artifact list response with pagination."""
    items: List[Artifact]
    next_cursor: Optional[str] = None


# Draft Schemas
class Draft(BaseModel):
    """Draft response model."""
    draft_id: UUID = Field(alias="id")
    workspace_id: UUID
    type: str
    title: str
    status: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    content_json: Dict[str, Any] = Field(default_factory=dict)

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_orm_model(cls, draft):
        """Convert ORM model to schema."""
        return cls(
            id=draft.id,
            workspace_id=draft.workspace_id,
            type=draft.type,
            title=draft.title,
            status=draft.status,
            created_by=draft.created_by,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
            content_json=draft.content_json or {}
        )


# Error Schema
class ErrorDetail(BaseModel):
    """Error detail."""
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: ErrorDetail
    correlation_id: Optional[str] = None


# Pack Schemas
class Pack(BaseModel):
    """Pack response model."""
    pack_id: UUID = Field(alias="id")
    pack_ref: str
    name: str
    version: str
    description: Optional[str] = None
    schema_name: Optional[str] = None
    manifest: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_orm_model(cls, pack):
        """Convert ORM model to schema."""
        return cls(
            id=pack.id,
            pack_ref=pack.pack_ref,
            name=pack.name,
            version=pack.version,
            description=pack.description,
            schema_name=pack.schema_name,
            manifest=pack.manifest or {},
            created_at=pack.created_at,
            updated_at=pack.updated_at
        )


class PackInstallation(BaseModel):
    """Pack installation response model."""
    installation_id: UUID = Field(alias="id")
    pack_id: UUID
    pack_ref: str
    env_id: str
    status: str

    # Schema configuration
    schema_mode: str
    schema_name: Optional[str] = None

    # Version and migration tracking
    installed_version: Optional[str] = None
    migration_provider: str = "sql"
    migration_state: Optional[str] = None

    # Installation metadata
    installed_at: Optional[datetime] = None
    installed_by_run_id: Optional[UUID] = None
    updated_by_run_id: Optional[UUID] = None

    # Error tracking
    error: Optional[Dict[str, Any]] = None
    last_error_at: Optional[datetime] = None

    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_orm_model(cls, installation):
        """Convert ORM model to schema."""
        return cls(
            id=installation.id,
            pack_id=installation.pack_id,
            pack_ref=installation.pack_ref,
            env_id=installation.env_id,
            status=installation.status.value if hasattr(installation.status, 'value') else installation.status,
            schema_mode=installation.schema_mode,
            schema_name=installation.schema_name,
            installed_version=installation.installed_version,
            migration_provider=installation.migration_provider,
            migration_state=installation.migration_state,
            installed_at=installation.installed_at,
            installed_by_run_id=installation.installed_by_run_id,
            updated_by_run_id=installation.updated_by_run_id,
            error=installation.error,
            last_error_at=installation.last_error_at,
            created_at=installation.created_at,
            updated_at=installation.updated_at
        )


class PackWithInstallation(BaseModel):
    """Pack with installation status."""
    pack: Pack
    installation: Optional[PackInstallation] = None


class PackListResponse(BaseModel):
    """Pack list response."""
    items: List[PackWithInstallation]
    next_cursor: Optional[str] = None


class PackInstallRequest(BaseModel):
    """Pack install/upgrade request body."""
    run_at: Optional[datetime] = Field(None, description="Schedule run for later (default: now)")
    priority: int = Field(100, description="Priority (lower = higher priority). 0-9: critical, 10-49: high, 50-100: normal, 200+: background")
    max_attempts: Optional[int] = Field(None, description="Maximum retry attempts (default: no retries)")


class PackStatusResponse(BaseModel):
    """Pack installation status response."""
    pack_ref: str
    status: str

    # Schema configuration
    schema_mode: Optional[str] = None
    schema_name: Optional[str] = None

    # Version and migration tracking
    installed_version: Optional[str] = None
    migration_provider: Optional[str] = None
    migration_state: Optional[str] = None

    # Installation metadata
    installed_at: Optional[datetime] = None
    installed_by_run_id: Optional[UUID] = None
    updated_by_run_id: Optional[UUID] = None

    # Error tracking
    error: Optional[Dict[str, Any]] = None
    last_error_at: Optional[datetime] = None


# Release Schemas (contracts-aligned)
class ReleasePlanAction(BaseModel):
    op: str
    targetType: str
    targetName: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ReleasePlanArtifacts(BaseModel):
    runtimeSpecPath: Optional[str] = None
    backendPlanPath: Optional[str] = None
    composeYamlPath: Optional[str] = None
    diffPath: Optional[str] = None
    releaseSpecPath: Optional[str] = None

    class Config:
        extra = "allow"


class ReleasePlan(BaseModel):
    planId: str
    releaseId: str
    revisionFrom: Optional[int] = None
    revisionTo: int
    summary: str
    actions: List[ReleasePlanAction]
    artifacts: ReleasePlanArtifacts
    actionKind: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)


class ReleaseObservedStatus(BaseModel):
    timestamp: datetime
    backend: Optional[str] = None
    message: Optional[str] = None


class ReleaseServiceStatus(BaseModel):
    name: str
    state: str
    health: str = "unknown"
    details: Dict[str, Any] = Field(default_factory=dict)


class ReleaseStatus(BaseModel):
    releaseId: str
    desiredRevision: int
    observed: ReleaseObservedStatus
    services: List[ReleaseServiceStatus]


class ReleaseSummary(BaseModel):
    releaseId: str
    name: str
    namespace: str
    desiredRevision: Optional[int] = None
    backend: Optional[str] = None
    updatedAt: datetime


class ReleaseDetails(BaseModel):
    releaseId: str
    name: str
    namespace: str
    revision: int
    backend: Optional[str] = None
    updatedAt: datetime
    releaseSpec: Dict[str, Any]
    artifacts: Dict[str, str]


class Operation(BaseModel):
    operationId: str
    releaseId: str
    type: str
    status: str
    createdAt: datetime
    startedAt: Optional[datetime] = None
    finishedAt: Optional[datetime] = None
    planId: Optional[str] = None
    message: str = ""
    artifacts: Dict[str, str] = Field(default_factory=dict)
