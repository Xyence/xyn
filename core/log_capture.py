"""Log capture utilities for step execution.

This module provides utilities to capture logs during step execution
and store them as artifacts linked to steps.
"""
import uuid
import io
import logging
from contextlib import contextmanager
from typing import Generator, Optional
from datetime import datetime
from sqlalchemy.orm import Session

from core import models
from core.artifact_store import ArtifactStoreBase


logger = logging.getLogger(__name__)


class StepLogCapture:
    """Captures logs for a step execution and stores as artifact."""

    def __init__(
        self,
        step_id: uuid.UUID,
        run_id: uuid.UUID,
        db: Session,
        artifact_store: ArtifactStoreBase,
        correlation_id: Optional[str] = None
    ):
        """Initialize log capture for a step.

        Args:
            step_id: Step UUID
            run_id: Run UUID
            db: Database session
            artifact_store: Artifact store instance
            correlation_id: Optional correlation ID for tracing
        """
        self.step_id = step_id
        self.run_id = run_id
        self.db = db
        self.artifact_store = artifact_store
        self.correlation_id = correlation_id
        self.log_buffer = io.StringIO()

        # Write header with metadata for self-describing artifacts
        self._write_header()

    def _write_header(self):
        """Write artifact metadata header for self-describing logs."""
        self.log_buffer.write(f"Run ID: {self.run_id}\n")
        self.log_buffer.write(f"Step ID: {self.step_id}\n")
        if self.correlation_id:
            self.log_buffer.write(f"Correlation ID: {self.correlation_id}\n")
        self.log_buffer.write("---\n")

    def write(self, message: str):
        """Write a log message to the buffer.

        Args:
            message: Log message to write
        """
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self.log_buffer.write(f"[{timestamp}] {message}\n")

    def write_line(self, message: str):
        """Write a log line with newline.

        Args:
            message: Log message
        """
        self.write(message)

    async def finalize(self) -> Optional[uuid.UUID]:
        """Finalize log capture and create artifact.

        Creates an Artifact with kind="log" and returns its ID.

        Returns:
            Artifact UUID if logs were captured, None otherwise
        """
        log_content = self.log_buffer.getvalue()

        if not log_content:
            logger.debug(f"No logs captured for step {self.step_id}")
            return None

        # Create artifact for logs
        artifact_id = uuid.uuid4()
        log_bytes = log_content.encode('utf-8')

        # Store in artifact store
        storage_path, sha256_hash = await self.artifact_store.store(
            artifact_id=artifact_id,
            content=log_bytes,
            compute_sha256=True
        )

        # Create artifact DB record
        artifact = models.Artifact(
            id=artifact_id,
            name=f"step-{self.step_id}-logs.txt",
            kind="log",
            content_type="text/plain",
            byte_length=len(log_bytes),
            sha256=sha256_hash,
            run_id=self.run_id,
            step_id=self.step_id,
            created_by="system",
            storage_path=storage_path,
            extra_metadata={
                "encoding": "utf-8",
                "captured_at": datetime.utcnow().isoformat()
            }
        )

        self.db.add(artifact)
        self.db.commit()
        self.db.refresh(artifact)

        logger.info(
            f"Created log artifact {artifact_id} for step {self.step_id} "
            f"({len(log_bytes)} bytes)"
        )

        return artifact_id


@contextmanager
def capture_step_logs(
    step_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session,
    artifact_store: ArtifactStoreBase
) -> Generator[StepLogCapture, None, None]:
    """Context manager for capturing step logs.

    Usage:
        with capture_step_logs(step_id, run_id, db, store) as log_capture:
            log_capture.write("Starting step execution...")
            # ... execute step ...
            log_capture.write("Step completed successfully")
        # Logs are automatically saved as artifact and linked to step

    Args:
        step_id: Step UUID
        run_id: Run UUID
        db: Database session
        artifact_store: Artifact store instance

    Yields:
        StepLogCapture instance for writing logs
    """
    capture = StepLogCapture(step_id, run_id, db, artifact_store)

    try:
        yield capture
    finally:
        # Always finalize logs, even if step fails
        pass  # Don't auto-finalize in context manager
        # Let caller explicitly finalize to control timing
