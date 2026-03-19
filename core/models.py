"""SQLAlchemy database models for Xyn Seed v0.0"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON, ForeignKey, Text, Enum, BigInteger, Index, UniqueConstraint, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
import enum

from core.database import Base


class RunStatus(str, enum.Enum):
    """Run execution status."""
    QUEUED = "queued"      # Waiting in queue for worker to claim
    CREATED = "created"    # Legacy - being phased out
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class StepStatus(str, enum.Enum):
    """Step execution status."""
    QUEUED = "queued"
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class DraftStatus(str, enum.Enum):
    """Draft status."""
    DRAFT = "draft"
    READY = "ready"
    SUBMITTED = "submitted"
    ARCHIVED = "archived"


class JobStatus(str, enum.Enum):
    """Job status."""
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RuntimeWorkerStatus(str, enum.Enum):
    """Registered runtime worker availability."""
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


class Event(Base):
    """Event model - immutable input records."""
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_name = Column(String(255), nullable=False, index=True)
    occurred_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
    env_id = Column(String(255), nullable=False, default="local-dev")
    actor = Column(String(255), nullable=False, default="system")
    correlation_id = Column(String(255), nullable=True, index=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True, index=True)
    step_id = Column(UUID(as_uuid=True), ForeignKey("steps.id"), nullable=True)
    resource_type = Column(String(255), nullable=True)
    resource_id = Column(String(255), nullable=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    run = relationship("Run", back_populates="events", foreign_keys=[run_id])
    step = relationship("Step", back_populates="events", foreign_keys=[step_id])


class Blueprint(Base):
    """Blueprint model - declarative workflow definitions."""
    __tablename__ = "blueprints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, unique=True, index=True)
    version = Column(String(50), nullable=False)
    trigger_event_type = Column(String(255), nullable=True, index=True)
    definition = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    runs = relationship("Run", back_populates="blueprint")


class Workspace(Base):
    """Workspace model."""
    __tablename__ = "workspaces"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    title = Column(String(255), nullable=False, default="Default Workspace")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    drafts = relationship("Draft", back_populates="workspace")
    jobs = relationship("Job", back_populates="workspace")
    locations = relationship("Location", back_populates="workspace")
    palette_commands = relationship("PaletteCommand", back_populates="workspace")


class Draft(Base):
    """Draft model - NL intent and app spec drafts."""
    __tablename__ = "drafts"
    __table_args__ = (
        Index("ix_drafts_workspace_id", "workspace_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    type = Column(String(100), nullable=False, default="app_intent")
    title = Column(String(255), nullable=False)
    content_json = Column(JSON, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default=DraftStatus.DRAFT.value, index=True)
    created_by = Column(String(255), nullable=False, default="system")
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="drafts")


class Job(Base):
    """Job model for draft submit/generation/deploy workflows."""
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_workspace_id", "workspace_id"),
        Index("ix_jobs_status", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    type = Column(String(100), nullable=False)
    status = Column(String(32), nullable=False, default=JobStatus.QUEUED.value)
    input_json = Column(JSON, nullable=False, default=dict)
    output_json = Column(JSON, nullable=True)
    logs_text = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="jobs")


class Location(Base):
    """Reusable workspace-scoped location primitive."""
    __tablename__ = "locations"
    __table_args__ = (
        Index("ix_locations_workspace_id", "workspace_id"),
        Index("ix_locations_workspace_kind", "workspace_id", "kind"),
        Index("ix_locations_workspace_parent", "workspace_id", "parent_location_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False)
    name = Column(String(255), nullable=False)
    kind = Column(String(64), nullable=False)
    parent_location_id = Column(UUID(as_uuid=True), ForeignKey("locations.id"), nullable=True)
    address_line1 = Column(String(255), nullable=True)
    address_line2 = Column(String(255), nullable=True)
    city = Column(String(255), nullable=True)
    region = Column(String(255), nullable=True)
    postal_code = Column(String(64), nullable=True)
    country = Column(String(128), nullable=True)
    notes = Column(Text, nullable=True)
    tags_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="locations")
    parent = relationship("Location", remote_side=[id], foreign_keys=[parent_location_id])


class PaletteCommand(Base):
    """Workspace/global palette command registry entries."""
    __tablename__ = "palette_commands"
    __table_args__ = (
        Index("ix_palette_commands_workspace_id", "workspace_id"),
        Index("ix_palette_commands_workspace_command", "workspace_id", "command_key"),
        CheckConstraint("handler_type <> ''", name="ck_palette_commands_handler_type_nonempty"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=True)
    command_key = Column(String(255), nullable=False)
    handler_type = Column(String(64), nullable=False, default="http_json")
    handler_config_json = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", back_populates="palette_commands")


class Run(Base):
    """Run model - execution instance of a blueprint."""
    __tablename__ = "runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    blueprint_id = Column(UUID(as_uuid=True), ForeignKey("blueprints.id"), nullable=True, index=True)
    event_id = Column(UUID(as_uuid=True), ForeignKey("events.id"), nullable=True)
    status = Column(Enum(RunStatus), nullable=False, default=RunStatus.QUEUED, index=True)
    actor = Column(String(255), nullable=False, default="system")
    correlation_id = Column(String(255), nullable=False, index=True)
    inputs = Column(JSON, nullable=False, default=dict)
    outputs = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)

    # Queue and lease management
    queued_at = Column(DateTime(timezone=True), nullable=True, index=True)
    locked_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(String(255), nullable=True)  # Worker identifier
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)  # For crash recovery
    heartbeat_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Scheduling and priority
    run_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)  # When run becomes eligible
    priority = Column(Integer, nullable=False, default=100, index=True)  # Lower = higher priority
    attempt = Column(Integer, nullable=False, default=0)  # Retry count
    max_attempts = Column(Integer, nullable=True)  # Optional retry limit
    work_item_id = Column(String(255), nullable=True, index=True)
    worker_type = Column(String(255), nullable=True, index=True)
    prompt_payload = Column(JSON, nullable=True, default=dict)
    execution_policy = Column(JSON, nullable=True, default=dict)
    summary = Column(Text, nullable=True)
    escalation_reason = Column(String(255), nullable=True)
    failure_reason = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # DAG/Orchestration (Phase 2)
    parent_run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True, index=True)

    # Relationships
    blueprint = relationship("Blueprint", back_populates="runs")
    trigger_event = relationship("Event", foreign_keys=[event_id])
    steps = relationship("Step", back_populates="run", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="run")
    events = relationship("Event", back_populates="run", foreign_keys=[Event.run_id])

    # DAG relationships
    parent = relationship("Run", remote_side=[id], foreign_keys=[parent_run_id])
    edges_as_parent = relationship("RunEdge", back_populates="parent", foreign_keys="RunEdge.parent_run_id")
    edges_as_child = relationship("RunEdge", back_populates="child", foreign_keys="RunEdge.child_run_id")

    @property
    def worker_id(self):
        return self.locked_by

    @worker_id.setter
    def worker_id(self, value):
        self.locked_by = value

    @property
    def attempt_count(self):
        return self.attempt

    @attempt_count.setter
    def attempt_count(self, value):
        self.attempt = value


class RunEdge(Base):
    """RunEdge model - parent/child relationships for DAG execution."""
    __tablename__ = "run_edges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    parent_run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    child_run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    relation = Column(String(50), nullable=False, default="child")
    child_key = Column(String(255), nullable=True)  # Idempotency key for resumable spawns
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    # Relationships
    parent = relationship("Run", back_populates="edges_as_parent", foreign_keys=[parent_run_id])
    child = relationship("Run", back_populates="edges_as_child", foreign_keys=[child_run_id])

    __table_args__ = (
        # Prevent duplicate parent->child edges
        UniqueConstraint('parent_run_id', 'child_run_id', name='uq_run_edges_parent_child'),
        # Note: Partial unique index for idempotent spawns (parent_run_id, child_key WHERE child_key IS NOT NULL)
        # is created directly in migration SQL (003_add_run_edges_for_dag.sql) since SQLAlchemy
        # doesn't support dialect-agnostic partial unique constraints in ORM models
    )


class Step(Base):
    """Step model - atomic unit in a run."""
    __tablename__ = "steps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    idx = Column(Integer, nullable=False)
    kind = Column(String(50), nullable=False)  # agent_task, action_task, gate, transform
    status = Column(Enum(StepStatus), nullable=False, default=StepStatus.CREATED, index=True)
    inputs = Column(JSON, nullable=True)
    outputs = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)
    logs_artifact_id = Column(UUID(as_uuid=True), ForeignKey("artifacts.id"), nullable=True)
    step_key = Column(String(255), nullable=True, index=True)
    label = Column(String(255), nullable=True)
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    run = relationship("Run", back_populates="steps")
    artifacts = relationship("Artifact", back_populates="step", foreign_keys="Artifact.step_id")
    logs_artifact = relationship("Artifact", foreign_keys=[logs_artifact_id], post_update=True)
    events = relationship("Event", back_populates="step", foreign_keys=[Event.step_id])

    @property
    def sequence_no(self):
        return self.idx

    @sequence_no.setter
    def sequence_no(self, value):
        self.idx = value


class Artifact(Base):
    """Artifact model - generated outputs."""
    __tablename__ = "artifacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=True)
    name = Column(String(255), nullable=False)
    kind = Column(String(50), nullable=False)  # log, report, bundle, file
    storage_scope = Column(String(32), nullable=False, default="instance-local", index=True)
    sync_state = Column(String(32), nullable=False, default="local", index=True)
    content_type = Column(String(255), nullable=False)
    byte_length = Column(BigInteger, nullable=True)
    sha256 = Column(String(64), nullable=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True, index=True)
    step_id = Column(UUID(as_uuid=True), ForeignKey("steps.id"), nullable=True, index=True)
    created_by = Column(String(255), nullable=False, default="system")
    extra_metadata = Column(JSON, nullable=False, default=dict)
    storage_path = Column(String(1024), nullable=True)  # LocalFS path
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    # Relationships
    workspace = relationship("Workspace", foreign_keys=[workspace_id])
    run = relationship("Run", back_populates="artifacts", foreign_keys=[run_id])
    step = relationship("Step", back_populates="artifacts", foreign_keys=[step_id])

    @property
    def artifact_type(self):
        return self.kind

    @artifact_type.setter
    def artifact_type(self, value):
        self.kind = value

    @property
    def uri(self):
        return self.storage_path

    @uri.setter
    def uri(self, value):
        self.storage_path = value

    @property
    def label(self):
        return self.name

    @label.setter
    def label(self, value):
        self.name = value

    @property
    def metadata_json(self):
        return self.extra_metadata

    @metadata_json.setter
    def metadata_json(self, value):
        self.extra_metadata = value


class LifecycleTransition(Base):
    """Durable lifecycle transition history for core compatibility integrations.

    Canonical platform lifecycle history now lives in xyn-platform.
    """
    __tablename__ = "lifecycle_transitions"
    __table_args__ = (
        Index("ix_lifecycle_transitions_workspace_id", "workspace_id"),
        Index("ix_lifecycle_transitions_object", "object_type", "object_id"),
        Index("ix_lifecycle_transitions_lifecycle_name", "lifecycle_name"),
        Index("ix_lifecycle_transitions_created_at", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id = Column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=True, index=True)
    lifecycle_name = Column(String(128), nullable=False)
    object_type = Column(String(128), nullable=False)
    object_id = Column(String(255), nullable=False)
    from_state = Column(String(64), nullable=True)
    to_state = Column(String(64), nullable=False)
    actor = Column(String(255), nullable=True)
    reason = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=False, default=dict)
    correlation_id = Column(String(255), nullable=True)
    run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    workspace = relationship("Workspace", foreign_keys=[workspace_id])
    run = relationship("Run", foreign_keys=[run_id])


class RuntimeWorker(Base):
    """Persistent runtime worker registrations."""
    __tablename__ = "runtime_workers"
    __table_args__ = (
        Index("ix_runtime_workers_worker_type_status", "worker_type", "status"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(String(255), nullable=False, unique=True, index=True)
    worker_type = Column(String(255), nullable=False, index=True)
    runtime_environment = Column(String(255), nullable=False)
    status = Column(Enum(RuntimeWorkerStatus), nullable=False, default=RuntimeWorkerStatus.IDLE, index=True)
    last_heartbeat = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, index=True)
    capabilities_json = Column(JSON, nullable=False, default=list)
    active_run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    active_run = relationship("Run", foreign_keys=[active_run_id])


class WorkspaceSetting(Base):
    """Workspace-level settings used by seed control-plane features."""
    __tablename__ = "workspace_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_slug = Column(String(255), nullable=False, unique=True, index=True)
    default_artifact_registry_slug = Column(String(255), nullable=False, default="default-registry")
    default_context_pack_artifact_ids_json = Column(JSON, nullable=False, default=list)
    artifact_sync_target = Column(String(512), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Secret(Base):
    """Minimal secrets table for reference-based auth configuration."""
    __tablename__ = "secrets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, unique=True, index=True)
    value = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class Node(Base):
    """Node model - deployed instance."""
    __tablename__ = "nodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False, unique=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id"), nullable=True)
    version = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False, default="active")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Self-referential relationship
    parent = relationship("Node", remote_side=[id], backref="children")


class PackStatus(str, enum.Enum):
    """Pack installation status."""
    PENDING = "pending"  # Installation record created, not yet started
    INSTALLING = "installing"
    INSTALLED = "installed"
    UPGRADING = "upgrading"
    FAILED = "failed"
    UNINSTALLING = "uninstalling"


class Pack(Base):
    """Pack registry - available packs."""
    __tablename__ = "packs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_ref = Column(String(255), nullable=False, unique=True)  # e.g., core.domain@v1
    name = Column(String(255), nullable=False)
    version = Column(String(50), nullable=False)
    description = Column(Text, nullable=True)
    schema_name = Column(String(255), nullable=True)  # Target schema if applicable
    manifest = Column(JSON, nullable=False, default=dict)  # Full pack manifest
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship to installations
    installations = relationship("PackInstallation", back_populates="pack")


class PackInstallation(Base):
    """Pack installation records - tracks installed packs per environment."""
    __tablename__ = "pack_installations"
    __table_args__ = (
        # Unique constraint for atomic duplicate prevention
        # Ensures (env_id, pack_ref) uniqueness at database level for safe concurrent operations
        UniqueConstraint("env_id", "pack_ref", name="uq_pack_installations_env_pack"),

        # Check constraint for schema_mode values
        CheckConstraint(
            "schema_mode IN ('per_pack', 'shared')",
            name="ck_pack_installations_schema_mode"
        ),

        # Explicit indexes for query performance
        Index("ix_pack_installations_pack_id", "pack_id"),
        Index("ix_pack_installations_pack_ref", "pack_ref"),
        Index("ix_pack_installations_env_id", "env_id"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pack_id = Column(UUID(as_uuid=True), ForeignKey("packs.id"), nullable=False)
    pack_ref = Column(String(255), nullable=False)  # Denormalized, server-derived from pack.pack_ref
    env_id = Column(String(255), nullable=False, default="local-dev")
    status = Column(Enum(PackStatus), nullable=False, default=PackStatus.PENDING)

    # Schema configuration
    schema_mode = Column(String(50), nullable=False, default="per_pack")  # shared / per_pack
    schema_name = Column(String(255), nullable=True)  # Actual deployed schema

    # Version and migration tracking
    installed_version = Column(String(50), nullable=True)
    migration_provider = Column(String(50), nullable=False, default="sql")  # sql, alembic, flyway
    migration_state = Column(String(255), nullable=True)  # Latest applied migration ID (e.g., "20260111_1414_init")

    # Installation metadata
    installed_at = Column(DateTime, nullable=True)  # Required when status=INSTALLED
    installed_by_run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True)
    updated_by_run_id = Column(UUID(as_uuid=True), ForeignKey("runs.id"), nullable=True)  # Last upgrade/change run

    # Error tracking
    error = Column(JSON, nullable=True)  # Error details if status=FAILED
    last_error_at = Column(DateTime, nullable=True)  # Timestamp of last error

    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    pack = relationship("Pack", back_populates="installations")
    installed_by_run = relationship("Run", foreign_keys=[installed_by_run_id])
    updated_by_run = relationship("Run", foreign_keys=[updated_by_run_id])
