"""Seed kernel app: kernel-only routes + dynamic artifact role loading."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from importlib import import_module

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core import __version__
from core.ai_bootstrap import ensure_default_agent_via_api
from core.app_jobs import AppJobWorkerHandle, start_app_job_worker, stop_app_job_worker
from core.artifact_registry import ensure_seed_default_registry
from core.api.artifacts import router as artifacts_router
from core.api.drafts import router as drafts_router
from core.api.events import router as events_router
from core.api.jobs import router as jobs_router
from core.api.locations import router as locations_router
from core.api.ops import router as ops_router
from core.api.context_packs import router as context_packs_router
from core.api.lifecycle import router as lifecycle_router
from core.api.palette import router as palette_router
from core.api.primitives import router as primitives_router
from core.api.runs import router as runs_router
from core.api.workspaces import router as workspaces_router
from core.api.artifact_refresh import router as artifact_refresh_router
from core.palette_commands import ensure_default_palette_commands
from core.provisioning_local import router as provisioning_router
from core.database import init_db
from core.database import SessionLocal
from core.env_config import export_runtime_env, load_seed_config
from core.kernel_loader import load_workspace_artifacts_into_app
from core.context_packs import ensure_runtime_context_pack_artifacts
from core.api.artifact_registries import router as artifact_registry_router
from core.workspaces import ensure_default_workspace
from core.runtime_loop import RuntimeWorkerLoopHandle, start_runtime_worker_loop, stop_runtime_worker_loop


class CorrelationIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "correlation_id"):
            record.correlation_id = "-"
        return True


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - [%(correlation_id)s] - %(message)s",
)
logging.getLogger().addFilter(CorrelationIdFilter())
_record_factory = logging.getLogRecordFactory()


def _log_record_factory(*args, **kwargs):
    record = _record_factory(*args, **kwargs)
    if not hasattr(record, "correlation_id"):
        record.correlation_id = "-"
    return record


logging.setLogRecordFactory(_log_record_factory)
logger = logging.getLogger(__name__)
_STARTUP_TS = time.time()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config = load_seed_config()
    os.environ.update(export_runtime_env(config))
    logger.info(
        "seed bootstrap config env=%s auth=%s ai_provider=%s ai_model=%s ai_enabled=%s",
        config.env,
        config.auth_mode,
        config.ai_provider,
        config.ai_model,
        config.ai_enabled,
    )
    logger.info("starting xyn-seed kernel v%s", __version__)
    init_db()
    db = SessionLocal()
    try:
        ensure_default_workspace(db)
        ensure_seed_default_registry(db)
        ensure_runtime_context_pack_artifacts(db)
        ensure_default_palette_commands(db)
    finally:
        db.close()
    ensure_default_agent_via_api()
    app_job_worker: AppJobWorkerHandle | None = None
    runtime_worker_loop: RuntimeWorkerLoopHandle | None = None
    if os.getenv("XYN_APP_JOB_WORKER_ENABLED", "true").strip().lower() in {"1", "true", "yes"}:
        app_job_worker = start_app_job_worker()
        logger.info("app-intent job worker started")
    if os.getenv("XYN_RUNTIME_WORKER_ENABLED", "true").strip().lower() in {"1", "true", "yes"}:
        runtime_worker_loop = start_runtime_worker_loop()
        logger.info("runtime worker loop started worker_id=%s", runtime_worker_loop.worker_id)

    loaded = await load_workspace_artifacts_into_app(app)
    logger.info("kernel loaded %d artifact(s)", len(loaded))

    reconciler_task = None
    if os.getenv("XYN_SEED_ENABLE_LEGACY_PRODUCT", "false").lower() in {"1", "true", "yes"}:
        legacy = import_module("core.legacy_product")
        reconciler_task = legacy.register_legacy_product_routes(app)
        logger.warning("legacy product routes are ENABLED")

    yield

    if reconciler_task:
        reconciler_task.cancel()
        try:
            await reconciler_task
        except Exception:
            pass
    stop_app_job_worker(app_job_worker)
    stop_runtime_worker_loop(runtime_worker_loop)
    logger.info("shutting down xyn-seed kernel")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Xyn Seed Kernel",
        description="Kernel-only bootstrap that loads workspace-installed artifacts.",
        version=__version__,
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def root():
        return {
            "service": "xyn-seed-kernel",
            "version": __version__,
            "legacy_enabled": os.getenv("XYN_SEED_ENABLE_LEGACY_PRODUCT", "false").lower() in {"1", "true", "yes"},
            "loaded_artifacts": getattr(app.state, "kernel_loaded_artifacts", []),
        }

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": int(time.time() - _STARTUP_TS),
        }

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.include_router(provisioning_router)
    app.include_router(artifact_registry_router)
    app.include_router(artifacts_router, prefix="/api/v1", tags=["Artifacts"])
    app.include_router(drafts_router, prefix="/api/v1", tags=["Drafts"])
    app.include_router(events_router, prefix="/api/v1", tags=["Events"])
    app.include_router(jobs_router, prefix="/api/v1", tags=["Jobs"])
    app.include_router(locations_router, prefix="/api/v1", tags=["Locations"])
    app.include_router(ops_router, prefix="/api/v1", tags=["Ops"])
    app.include_router(context_packs_router, prefix="/api/v1", tags=["Context Packs"])
    app.include_router(lifecycle_router, prefix="/api/v1", tags=["Lifecycle"])
    app.include_router(workspaces_router, prefix="/api/v1", tags=["Workspaces"])
    app.include_router(primitives_router, prefix="/api/v1", tags=["Primitives"])
    app.include_router(palette_router, prefix="/api/v1", tags=["Palette"])
    app.include_router(runs_router, prefix="/api/v1", tags=["Runs"])
    app.include_router(artifact_refresh_router, prefix="/api/v1", tags=["Artifacts"])

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("core.kernel_app:app", host="0.0.0.0", port=8000, reload=True)
