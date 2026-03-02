"""Postgres-backed run queue worker
Claim and execute queued blueprint runs with crash recovery via leases.
"""
import os
import asyncio
import logging
import signal
import sys
import uuid
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import SessionLocal
from core import models

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Worker configuration
WORKER_ID = os.getenv("WORKER_ID", f"worker-{os.getpid()}")
LEASE_DURATION_SECONDS = int(os.getenv("LEASE_DURATION_SECONDS", "60"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "2"))
BATCH_SIZE = int(os.getenv("WORKER_BATCH_SIZE", "1"))
ENABLE_BLUEPRINTS_LEGACY = os.getenv("XYN_ENABLE_BLUEPRINTS_LEGACY", "false").strip().lower() in {"1", "true", "yes"}

shutdown_requested = False


def handle_shutdown(signum, frame):
    """Handle graceful shutdown on SIGTERM/SIGINT."""
    global shutdown_requested
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_requested = True


def claim_run(db: Session) -> Optional[models.Run]:
    """Claim a queued run using atomic UPDATE ... RETURNING.

    Implements lease-based crash recovery: can reclaim runs where lease expired.
    Uses single atomic statement to prevent concurrency edge cases.

    Returns:
        Run object if claimed, None if no work available
    """
    # Atomic claim: SELECT FOR UPDATE SKIP LOCKED + UPDATE in one statement
    # Production-hardened: handles NULL run_at, prioritizes reclaims to clear zombies
    claim_sql = text("""
        WITH candidate AS (
          SELECT id
          FROM runs
          WHERE
            (
              -- QUEUED runs ready to run (COALESCE handles NULL run_at defensively)
              (status = 'QUEUED'::runstatus AND COALESCE(run_at, queued_at, created_at, NOW()) <= NOW())
              OR
              -- RUNNING runs with expired leases (crash recovery)
              (status = 'RUNNING'::runstatus AND lease_expires_at IS NOT NULL AND lease_expires_at < NOW())
            )
          ORDER BY
            priority ASC,
            -- Reclaim expired leases first (clears zombies quickly)
            CASE WHEN status = 'RUNNING'::runstatus THEN 0 ELSE 1 END,
            run_at ASC NULLS LAST,
            queued_at ASC NULLS LAST,
            created_at ASC
          FOR UPDATE SKIP LOCKED
          LIMIT :batch_size
        )
        UPDATE runs r
        SET
          status = 'RUNNING'::runstatus,
          locked_at = NOW(),
          locked_by = :worker_id,
          lease_expires_at = NOW() + (:lease_seconds || ' seconds')::interval,
          started_at = COALESCE(r.started_at, NOW())
        FROM candidate
        WHERE r.id = candidate.id
        RETURNING r.id
    """)

    row = db.execute(claim_sql, {
        "batch_size": BATCH_SIZE,
        "worker_id": WORKER_ID,
        "lease_seconds": LEASE_DURATION_SECONDS,
    }).fetchone()

    if not row:
        return None

    run_id = row[0]
    db.commit()

    # Load claimed run
    run = db.query(models.Run).filter(models.Run.id == run_id).one()
    logger.info(f"Claimed run {run.id} (blueprint={run.name}, correlation_id={run.correlation_id})")
    return run


async def renew_lease(db: Session, run_id: uuid.UUID) -> bool:
    """Renew lease on a running task to prevent reclaim by other workers.

    Uses raw SQL UPDATE with no ORM objects - safe for ephemeral sessions.

    Args:
        db: Short-lived database session (created per renewal tick)
        run_id: UUID of the run to renew

    Returns:
        True if lease renewed successfully, False if worker lost ownership
    """
    try:
        # Conditional renewal: only update if we still own it
        result = db.execute(text("""
            UPDATE runs
            SET lease_expires_at = NOW() + (:lease_seconds || ' seconds')::interval
            WHERE id = :run_id
              AND status = 'RUNNING'::runstatus
              AND locked_by = :worker_id
            RETURNING id
        """), {
            "run_id": run_id,
            "worker_id": WORKER_ID,
            "lease_seconds": LEASE_DURATION_SECONDS,
        })

        row = result.fetchone()
        db.commit()

        if row:
            logger.debug(f"Renewed lease for run {run_id}")
            return True
        else:
            logger.warning(f"Lost lease ownership for run {run_id} - another worker may have reclaimed it")
            return False

    except Exception as e:
        db.rollback()
        logger.error(f"Failed to renew lease for run {run_id}: {e}")
        return False


async def execute_run_worker(run: models.Run):
    """Execute a claimed run using the blueprint runner.

    Wrapper around execute_run() that handles lease renewal with ephemeral sessions.

    Args:
        run: Claimed run object (from claim session, used for ID only)
    """
    from core.exceptions import LostLeaseError

    run_id = run.id
    execution_db = SessionLocal()

    try:
        logger.info(f"Executing run {run_id}: {run.name}")

        # Import execute_run from runner
        from core.blueprints.runner import execute_run

        # Set up periodic lease renewal (uses ephemeral sessions per tick)
        renewal_task = asyncio.create_task(
            periodic_lease_renewal(run_id, LEASE_DURATION_SECONDS // 2)
        )

        try:
            # Execute using dedicated execution session with worker_id for ownership checks
            await execute_run(run_id, execution_db, worker_id=WORKER_ID)

            logger.info(f"Completed run {run_id}")

        finally:
            # Cancel lease renewal
            renewal_task.cancel()
            try:
                await renewal_task
            except asyncio.CancelledError:
                pass

    except LostLeaseError as e:
        # Worker lost lease during execution
        logger.warning(f"Lost lease for run {run_id}: {e}")

    except Exception as e:
        logger.error(f"Failed run {run_id}: {e}", exc_info=True)

    finally:
        execution_db.close()


async def periodic_lease_renewal(run_id: uuid.UUID, interval_seconds: int):
    """Periodically renew lease while run is executing.

    Uses ephemeral sessions per tick to avoid connection pool starvation
    and stale state issues. Works with run_id only (no ORM objects).

    Args:
        run_id: UUID of the run to renew
        interval_seconds: Seconds between renewal attempts

    Stops if lease ownership is lost (another worker reclaimed it).
    """
    try:
        while True:
            await asyncio.sleep(interval_seconds)

            # Use ephemeral session per tick
            renewal_db = SessionLocal()
            try:
                success = await renew_lease(renewal_db, run_id)
            finally:
                renewal_db.close()

            if not success:
                logger.error(f"Lost ownership of run {run_id} during execution - stopping renewal")
                return

    except asyncio.CancelledError:
        logger.debug(f"Lease renewal cancelled for run {run_id}")
        return


async def worker_loop():
    """Main worker loop: claim and execute runs."""
    # Register blueprints only in legacy mode.
    if ENABLE_BLUEPRINTS_LEGACY:
        from core.blueprints import pack_install, pack_upgrade, test_orchestrator, core_migrations_apply_v1  # noqa - Import to register
        from core.blueprints.registry import list_blueprints

        registered = list_blueprints()
        logger.info(f"Registered {len(registered)} blueprints: {', '.join(registered)}")
    else:
        logger.info("Blueprint execution is disabled (XYN_ENABLE_BLUEPRINTS_LEGACY=false)")

    # Start metrics collector
    from core.observability.collector import metrics_collector_loop
    metrics_interval = int(os.getenv("METRICS_COLLECTOR_INTERVAL", "5"))
    asyncio.create_task(metrics_collector_loop(interval_seconds=metrics_interval))

    logger.info(f"Worker {WORKER_ID} started")
    logger.info(f"Configuration: lease={LEASE_DURATION_SECONDS}s, poll={POLL_INTERVAL_SECONDS}s, batch={BATCH_SIZE}")

    while not shutdown_requested:
        db = SessionLocal()
        try:
            # Claim a run
            run = claim_run(db)

            if run:
                # Execute run (in new session to avoid lock conflicts)
                await execute_run_worker(run)
            else:
                # No work available, sleep before next poll
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

        except Exception as e:
            logger.error(f"Worker loop error: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        finally:
            db.close()

    logger.info(f"Worker {WORKER_ID} shutting down gracefully")


def main():
    """Entry point for worker process."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    logger.info("Starting Xyn worker process")

    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    except Exception as e:
        logger.error(f"Worker crashed: {e}", exc_info=True)
        sys.exit(1)

    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
