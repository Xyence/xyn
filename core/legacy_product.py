"""Legacy product routes shim.

Imported only when XYN_SEED_ENABLE_LEGACY_PRODUCT=true.
"""

from __future__ import annotations

import asyncio
import logging
from fastapi import FastAPI

from core.api import artifacts, debug, domain, drafts, events, health, ops, packs, releases, runs
from core.blueprints.registry import list_blueprints
from core.middleware import CorrelationIdMiddleware
from core.ui import ui_artifacts, ui_domain, ui_events, ui_runs

logger = logging.getLogger(__name__)


def register_legacy_product_routes(app: FastAPI) -> asyncio.Task | None:
    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(health.router, prefix="/api/v1", tags=["Health"])
    app.include_router(events.router, prefix="/api/v1", tags=["Events"])
    app.include_router(runs.router, prefix="/api/v1", tags=["Runs"])
    app.include_router(artifacts.router, prefix="/api/v1", tags=["Artifacts"])
    app.include_router(drafts.router, prefix="/api/v1", tags=["Drafts"])
    app.include_router(packs.router, prefix="/api/v1", tags=["Packs"])
    app.include_router(debug.router, prefix="/api/v1", tags=["Debug"])
    app.include_router(domain.router, prefix="/api/v1", tags=["Domain"])
    app.include_router(ops.router, prefix="/api/v1", tags=["Operations"])
    app.include_router(releases.router, prefix="/api/v1", tags=["Releases"])

    app.include_router(ui_events.router, prefix="/ui", tags=["UI - Events"])
    app.include_router(ui_runs.router, prefix="/ui", tags=["UI - Runs"])
    app.include_router(ui_artifacts.router, prefix="/ui", tags=["UI - Artifacts"])
    app.include_router(ui_domain.router, prefix="/ui", tags=["UI - Domain"])

    from core.blueprints import core_migrations_apply_v1, pack_install, pack_upgrade, test_orchestrator  # noqa: F401

    registered = list_blueprints()
    logger.info("legacy mode registered %d blueprints: %s", len(registered), ", ".join(registered))

    try:
        from core.releases.reconciler import reconcile_loop

        return asyncio.create_task(reconcile_loop())
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        logger.warning("legacy reconciler loop failed to start: %s", exc)
        return None


__all__ = ["register_legacy_product_routes"]
