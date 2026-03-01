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
from core.database import init_db
from core.kernel_loader import load_workspace_artifacts_into_app


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
    logger.info("starting xyn-seed kernel v%s", __version__)
    init_db()

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

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("core.kernel_app:app", host="0.0.0.0", port=8000, reload=True)
