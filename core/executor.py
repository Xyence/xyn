"""Simple execution engine for Xyn Seed v0.0

This is a minimal run/step executor that demonstrates the execution model.
Real implementation will be expanded in v1.
"""
import uuid
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session

from core import models, schemas
from core.artifact_store import ArtifactStoreBase, get_runtime_artifact_store
from core.log_capture import StepLogCapture

logger = logging.getLogger(__name__)


class SimpleExecutor:
    """Simple executor for runs and steps.

    v0.0 provides basic execution mechanics without full blueprint compilation.
    This demonstrates the run/step lifecycle for smoke testing.
    """

    def __init__(self, db: Session, artifact_store: Optional[ArtifactStoreBase] = None):
        """Initialize executor with database session.

        Args:
            db: Database session
            artifact_store: Artifact store instance (creates default if not provided)
        """
        self.db = db
        self.artifact_store = artifact_store or get_runtime_artifact_store()

    async def create_run(
        self,
        name: str,
        inputs: Dict[str, Any],
        actor: str = "system",
        blueprint_id: Optional[uuid.UUID] = None,
        correlation_id: Optional[str] = None
    ) -> models.Run:
        """Create a new run.

        Args:
            name: Run name
            inputs: Input parameters
            actor: Actor creating the run
            blueprint_id: Optional blueprint reference
            correlation_id: Optional correlation ID (generates new one if not provided)

        Returns:
            Created Run model
        """
        if correlation_id is None:
            correlation_id = str(uuid.uuid4())

        run = models.Run(
            name=name,
            blueprint_id=blueprint_id,
            status=models.RunStatus.CREATED,
            actor=actor,
            correlation_id=correlation_id,
            inputs=inputs,
            created_at=datetime.utcnow()
        )

        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)

        # Emit run.created event
        event = models.Event(
            event_name="xyn.run.created",
            occurred_at=datetime.utcnow(),
            env_id="local-dev",
            actor=actor,
            correlation_id=correlation_id,
            run_id=run.id,
            resource_type="run",
            resource_id=str(run.id),
            data={"name": name}
        )
        self.db.add(event)
        self.db.commit()

        logger.info(
            f"Created run {run.id}: {name}",
            extra={"correlation_id": correlation_id}
        )
        return run

    async def start_run(self, run_id: uuid.UUID) -> models.Run:
        """Start a run.

        Args:
            run_id: Run ID

        Returns:
            Updated Run model
        """
        run = self.db.query(models.Run).filter(models.Run.id == run_id).first()
        if not run:
            raise ValueError(f"Run {run_id} not found")

        run.status = models.RunStatus.RUNNING
        run.started_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(run)

        # Emit run.started event
        event = models.Event(
            event_name="xyn.run.started",
            occurred_at=datetime.utcnow(),
            env_id="local-dev",
            actor=run.actor,
            correlation_id=run.correlation_id,
            run_id=run.id,
            resource_type="run",
            resource_id=str(run.id),
            data={}
        )
        self.db.add(event)
        self.db.commit()

        logger.info(
            f"Started run {run.id}",
            extra={"correlation_id": run.correlation_id}
        )
        return run

    async def complete_run(
        self,
        run_id: uuid.UUID,
        outputs: Optional[Dict[str, Any]] = None,
        error: Optional[Dict[str, Any]] = None
    ) -> models.Run:
        """Complete a run.

        Args:
            run_id: Run ID
            outputs: Output data (if successful)
            error: Error data (if failed)

        Returns:
            Updated Run model
        """
        run = self.db.query(models.Run).filter(models.Run.id == run_id).first()
        if not run:
            raise ValueError(f"Run {run_id} not found")

        if error:
            run.status = models.RunStatus.FAILED
            run.error = error
            event_name = "xyn.run.failed"
        else:
            run.status = models.RunStatus.COMPLETED
            run.outputs = outputs
            event_name = "xyn.run.completed"

        run.completed_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(run)

        # Emit completion event
        event = models.Event(
            event_name=event_name,
            occurred_at=datetime.utcnow(),
            env_id="local-dev",
            actor=run.actor,
            correlation_id=run.correlation_id,
            run_id=run.id,
            resource_type="run",
            resource_id=str(run.id),
            data={"status": run.status.value}
        )
        self.db.add(event)
        self.db.commit()

        logger.info(
            f"Completed run {run.id} with status {run.status.value}",
            extra={"correlation_id": run.correlation_id}
        )
        return run

    async def create_step(
        self,
        run_id: uuid.UUID,
        name: str,
        kind: str,
        idx: int,
        inputs: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None
    ) -> models.Step:
        """Create a new step in a run.

        Args:
            run_id: Parent run ID
            name: Step name
            kind: Step kind (agent_task, action_task, etc.)
            idx: Step index
            inputs: Input parameters
            correlation_id: Optional correlation ID

        Returns:
            Created Step model
        """
        # Get run to extract correlation_id if not provided
        run = self.db.query(models.Run).filter(models.Run.id == run_id).first()
        if not run:
            raise ValueError(f"Run {run_id} not found")

        if correlation_id is None:
            correlation_id = run.correlation_id

        step = models.Step(
            run_id=run_id,
            name=name,
            kind=kind,
            idx=idx,
            status=models.StepStatus.CREATED,
            inputs=inputs,
            created_at=datetime.utcnow()
        )

        self.db.add(step)
        self.db.commit()
        self.db.refresh(step)

        # Emit step.created event
        event = models.Event(
            event_name="xyn.step.created",
            occurred_at=datetime.utcnow(),
            env_id="local-dev",
            actor=run.actor,
            correlation_id=correlation_id,
            run_id=run_id,
            step_id=step.id,
            resource_type="step",
            resource_id=str(step.id),
            data={"name": name, "kind": kind, "idx": idx}
        )
        self.db.add(event)
        self.db.commit()

        logger.info(
            f"Created step {step.id} in run {run_id}: {name}",
            extra={"correlation_id": correlation_id}
        )
        return step

    async def execute_simple_run(
        self,
        name: str,
        inputs: Dict[str, Any],
        simulate_failure: bool = False,
        correlation_id: Optional[str] = None
    ) -> models.Run:
        """Execute a simple demonstration run with steps.

        This creates a run with a few demo steps to showcase the execution model.

        Args:
            name: Run name
            inputs: Input parameters
            simulate_failure: Whether to simulate a failure
            correlation_id: Optional correlation ID for request tracing

        Returns:
            Completed Run model
        """
        # Create run
        run = await self.create_run(name=name, inputs=inputs, correlation_id=correlation_id)

        # Start run
        run = await self.start_run(run.id)

        # Create and execute demo steps
        step1 = await self.create_step(
            run_id=run.id,
            name="Initialize",
            kind="transform",
            idx=0,
            inputs=inputs
        )

        # Execute step1 with log capture
        step1.status = models.StepStatus.RUNNING
        step1.started_at = datetime.utcnow()
        self.db.commit()

        # Emit step.started event
        event = models.Event(
            event_name="xyn.step.started",
            occurred_at=datetime.utcnow(),
            env_id="local-dev",
            actor=run.actor,
            correlation_id=run.correlation_id,
            run_id=run.id,
            step_id=step1.id,
            resource_type="step",
            resource_id=str(step1.id),
            data={}
        )
        self.db.add(event)
        self.db.commit()

        # Capture logs for this step
        log_capture = StepLogCapture(step1.id, run.id, self.db, self.artifact_store, run.correlation_id)
        log_capture.write("Step execution started")
        log_capture.write(f"Step name: {step1.name}")
        log_capture.write(f"Step kind: {step1.kind}")
        log_capture.write(f"Inputs: {inputs}")
        log_capture.write("Performing initialization tasks...")
        log_capture.write("Initialization completed successfully")
        log_capture.write("Step execution completed")

        # Finalize logs and create artifact
        logs_artifact_id = await log_capture.finalize()

        step1.status = models.StepStatus.COMPLETED
        step1.completed_at = datetime.utcnow()
        step1.outputs = {"initialized": True}
        step1.logs_artifact_id = logs_artifact_id
        self.db.commit()

        # Emit step.completed event
        event = models.Event(
            event_name="xyn.step.completed",
            occurred_at=datetime.utcnow(),
            env_id="local-dev",
            actor=run.actor,
            correlation_id=run.correlation_id,
            run_id=run.id,
            step_id=step1.id,
            resource_type="step",
            resource_id=str(step1.id),
            data={"status": "completed"}
        )
        self.db.add(event)
        self.db.commit()

        if simulate_failure:
            # Create a failing step
            step2 = await self.create_step(
                run_id=run.id,
                name="Process (will fail)",
                kind="action_task",
                idx=1,
                inputs={"data": inputs}
            )

            step2.status = models.StepStatus.RUNNING
            step2.started_at = datetime.utcnow()
            self.db.commit()

            # Emit step.started event
            event = models.Event(
                event_name="xyn.step.started",
                occurred_at=datetime.utcnow(),
                env_id="local-dev",
                actor=run.actor,
                correlation_id=run.correlation_id,
                run_id=run.id,
                step_id=step2.id,
                resource_type="step",
                resource_id=str(step2.id),
                data={}
            )
            self.db.add(event)
            self.db.commit()

            # Capture logs for failing step
            log_capture2 = StepLogCapture(step2.id, run.id, self.db, self.artifact_store, run.correlation_id)
            log_capture2.write("Step execution started")
            log_capture2.write(f"Step name: {step2.name}")
            log_capture2.write(f"Processing data: {inputs}")
            log_capture2.write("WARNING: Simulating failure condition")
            log_capture2.write("ERROR: Demo failure triggered")
            log_capture2.write("Stack trace: [simulated]")
            log_capture2.write("Step execution failed")

            logs_artifact_id2 = await log_capture2.finalize()

            step2.status = models.StepStatus.FAILED
            step2.completed_at = datetime.utcnow()
            step2.error = {"code": "DEMO_FAILURE", "message": "Simulated failure for testing"}
            step2.logs_artifact_id = logs_artifact_id2
            self.db.commit()

            # Emit step.failed event
            event = models.Event(
                event_name="xyn.step.failed",
                occurred_at=datetime.utcnow(),
                env_id="local-dev",
                actor=run.actor,
                correlation_id=run.correlation_id,
                run_id=run.id,
                step_id=step2.id,
                resource_type="step",
                resource_id=str(step2.id),
                data={"error": step2.error}
            )
            self.db.add(event)
            self.db.commit()

            # Complete run with error
            run = await self.complete_run(
                run_id=run.id,
                error={"message": "Run failed at step: Process (will fail)"}
            )
        else:
            # Create a successful step
            step2 = await self.create_step(
                run_id=run.id,
                name="Process",
                kind="action_task",
                idx=1,
                inputs={"data": inputs}
            )

            step2.status = models.StepStatus.RUNNING
            step2.started_at = datetime.utcnow()
            self.db.commit()

            # Emit step.started event
            event = models.Event(
                event_name="xyn.step.started",
                occurred_at=datetime.utcnow(),
                env_id="local-dev",
                actor=run.actor,
                correlation_id=run.correlation_id,
                run_id=run.id,
                step_id=step2.id,
                resource_type="step",
                resource_id=str(step2.id),
                data={}
            )
            self.db.add(event)
            self.db.commit()

            # Capture logs for successful step
            log_capture2 = StepLogCapture(step2.id, run.id, self.db, self.artifact_store, run.correlation_id)
            log_capture2.write("Step execution started")
            log_capture2.write(f"Step name: {step2.name}")
            log_capture2.write(f"Processing data: {inputs}")
            log_capture2.write("Validating input data...")
            log_capture2.write("Input validation passed")
            log_capture2.write("Executing processing logic...")
            log_capture2.write("Processing completed successfully")
            log_capture2.write("Preparing output data...")
            log_capture2.write("Step execution completed successfully")

            logs_artifact_id2 = await log_capture2.finalize()

            step2.status = models.StepStatus.COMPLETED
            step2.completed_at = datetime.utcnow()
            step2.outputs = {"result": "success", "processed": True}
            step2.logs_artifact_id = logs_artifact_id2
            self.db.commit()

            # Emit step.completed event
            event = models.Event(
                event_name="xyn.step.completed",
                occurred_at=datetime.utcnow(),
                env_id="local-dev",
                actor=run.actor,
                correlation_id=run.correlation_id,
                run_id=run.id,
                step_id=step2.id,
                resource_type="step",
                resource_id=str(step2.id),
                data={"status": "completed"}
            )
            self.db.add(event)
            self.db.commit()

            # Complete run successfully
            run = await self.complete_run(
                run_id=run.id,
                outputs={"final_result": "Run completed successfully"}
            )

        return run
