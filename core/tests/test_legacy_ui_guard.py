from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest
from unittest import mock

from fastapi import APIRouter, FastAPI
from fastapi.responses import RedirectResponse

from core.legacy_product import register_legacy_ui_routes


class LegacyUiGuardTests(unittest.TestCase):
    def _build_app(self) -> FastAPI:
        app = FastAPI()
        register_legacy_ui_routes(app)
        return app

    def test_legacy_ui_routes_redirect_by_default(self):
        with mock.patch.dict(
            os.environ,
            {
                "XYN_ENABLE_LEGACY_UI_ROUTES": "false",
                "XYN_LEGACY_UI_REDIRECT_PATH": "/workbench",
                "XYN_ENABLE_BLUEPRINTS_LEGACY": "false",
            },
            clear=False,
        ):
            app = self._build_app()
            paths = {route.path for route in app.routes}
            self.assertIn("/ui", paths)
            self.assertIn("/ui/{legacy_path:path}", paths)
            catchall_route = next(route for route in app.routes if route.path == "/ui/{legacy_path:path}")
            response = asyncio.run(catchall_route.endpoint("runs"))
            self.assertIsInstance(response, RedirectResponse)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers.get("location"), "/workbench")

    def test_legacy_ui_redirect_target_is_configurable(self):
        with mock.patch.dict(
            os.environ,
            {
                "XYN_ENABLE_LEGACY_UI_ROUTES": "false",
                "XYN_LEGACY_UI_REDIRECT_PATH": "/w/demo/workbench",
                "XYN_ENABLE_BLUEPRINTS_LEGACY": "false",
            },
            clear=False,
        ):
            app = self._build_app()
            catchall_route = next(route for route in app.routes if route.path == "/ui/{legacy_path:path}")
            response = asyncio.run(catchall_route.endpoint("domain"))
            self.assertIsInstance(response, RedirectResponse)
            self.assertEqual(response.status_code, 307)
            self.assertEqual(response.headers.get("location"), "/w/demo/workbench")

    def test_legacy_ui_routes_can_be_opted_in_for_internal_workflows(self):
        ui_artifacts = types.ModuleType("core.ui.ui_artifacts")
        ui_domain = types.ModuleType("core.ui.ui_domain")
        ui_events = types.ModuleType("core.ui.ui_events")
        ui_runs = types.ModuleType("core.ui.ui_runs")
        for module, path in (
            (ui_artifacts, "/artifacts"),
            (ui_domain, "/domain"),
            (ui_events, "/events"),
            (ui_runs, "/runs"),
        ):
            router = APIRouter()

            @router.get(path)
            async def _handler():
                return {"ok": True}

            module.router = router

        with mock.patch.dict(
            os.environ,
            {
                "XYN_ENABLE_LEGACY_UI_ROUTES": "true",
                "XYN_ENABLE_BLUEPRINTS_LEGACY": "false",
            },
            clear=False,
        ), mock.patch.dict(
            sys.modules,
            {
                "core.ui.ui_artifacts": ui_artifacts,
                "core.ui.ui_domain": ui_domain,
                "core.ui.ui_events": ui_events,
                "core.ui.ui_runs": ui_runs,
            },
            clear=False,
        ):
            app = self._build_app()
        paths = {route.path for route in app.routes}
        self.assertIn("/ui/runs", paths)
        self.assertIn("/ui/artifacts", paths)
        self.assertIn("/ui/domain", paths)
        self.assertNotIn("/ui/{legacy_path:path}", paths)


if __name__ == "__main__":
    unittest.main()
