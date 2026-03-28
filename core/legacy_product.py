"""Legacy product routes shim.

Imported only when ``XYN_SEED_ENABLE_LEGACY_PRODUCT=true``.

DEBT-04 / DEMO-04 hardening:
- Legacy API routes may still be enabled for internal compatibility workflows.
- Legacy server-rendered UI routes under ``/ui/*`` are now separately gated by
  ``XYN_ENABLE_LEGACY_UI_ROUTES`` and default to disabled.
- When disabled, direct navigation to ``/ui/*`` is redirected to the modern
  workbench path to avoid demo/user-path leakage into legacy surfaces.
"""

from __future__ import annotations

import asyncio
import logging
import os
from fastapi import FastAPI
from fastapi import APIRouter
from fastapi.responses import RedirectResponse

logger = logging.getLogger(__name__)


def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _legacy_blueprints_enabled() -> bool:
    return _as_bool(os.getenv("XYN_ENABLE_BLUEPRINTS_LEGACY", "false"))


def _legacy_ui_routes_enabled() -> bool:
    return _as_bool(os.getenv("XYN_ENABLE_LEGACY_UI_ROUTES", "false"))


def _legacy_ui_redirect_target() -> str:
    return str(os.getenv("XYN_LEGACY_UI_REDIRECT_PATH", "/workbench")).strip() or "/workbench"


def register_legacy_ui_routes(app: FastAPI) -> None:
    if _legacy_ui_routes_enabled():
        from core.ui import ui_artifacts, ui_domain, ui_events, ui_runs

        app.include_router(ui_events.router, prefix="/ui", tags=["UI - Events"])
        app.include_router(ui_runs.router, prefix="/ui", tags=["UI - Runs"])
        app.include_router(ui_artifacts.router, prefix="/ui", tags=["UI - Artifacts"])
        app.include_router(ui_domain.router, prefix="/ui", tags=["UI - Domain"])
        logger.info("legacy UI routes are ENABLED (XYN_ENABLE_LEGACY_UI_ROUTES=true)")
        return

    guard_router = APIRouter()

    @guard_router.get("/ui", include_in_schema=False)
    @guard_router.get("/ui/{legacy_path:path}", include_in_schema=False)
    async def _redirect_legacy_ui(legacy_path: str = ""):
        target = _legacy_ui_redirect_target()
        return RedirectResponse(url=target, status_code=307)

    app.include_router(guard_router)
    logger.info(
        "legacy UI routes are DISABLED; /ui/* redirects to %s "
        "(set XYN_ENABLE_LEGACY_UI_ROUTES=true to opt in)",
        _legacy_ui_redirect_target(),
    )


def register_legacy_product_routes(app: FastAPI) -> asyncio.Task | None:
    from core.middleware import CorrelationIdMiddleware
    from core.api import artifacts, debug, domain, events, health, ops, packs, releases, runs

    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(health.router, prefix="/api/v1", tags=["Health"])
    app.include_router(events.router, prefix="/api/v1", tags=["Events"])
    app.include_router(runs.router, prefix="/api/v1", tags=["Runs"])
    app.include_router(artifacts.router, prefix="/api/v1", tags=["Artifacts"])
    app.include_router(packs.router, prefix="/api/v1", tags=["Packs"])
    app.include_router(debug.router, prefix="/api/v1", tags=["Debug"])
    app.include_router(domain.router, prefix="/api/v1", tags=["Domain"])
    app.include_router(ops.router, prefix="/api/v1", tags=["Operations"])
    app.include_router(releases.router, prefix="/api/v1", tags=["Releases"])

    register_legacy_ui_routes(app)

    if _legacy_blueprints_enabled():
        from core.blueprints import core_migrations_apply_v1, pack_install, pack_upgrade, test_orchestrator  # noqa: F401
        from core.blueprints.registry import list_blueprints

        registered = list_blueprints()
        logger.info("legacy mode registered %d blueprints: %s", len(registered), ", ".join(registered))
    else:
        logger.info("legacy mode started with blueprints disabled (XYN_ENABLE_BLUEPRINTS_LEGACY=false)")

    try:
        from core.releases.reconciler import reconcile_loop

        return asyncio.create_task(reconcile_loop())
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        logger.warning("legacy reconciler loop failed to start: %s", exc)
        return None


__all__ = ["register_legacy_product_routes", "register_legacy_ui_routes"]
