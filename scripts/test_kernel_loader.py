import asyncio
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.kernel_loader import load_workspace_artifacts_into_app, register_manifest_roles  # noqa: E402


BANNED_MODULES = [
    "core.ui.ui_artifacts",
    "core.ui.ui_domain",
    "core.ui.ui_events",
    "core.ui.ui_runs",
    "core.api.domain",
    "core.api.drafts",
    "core.api.packs",
    "core.api.ops",
    "core.api.releases",
]


def test_kernel_boot_does_not_import_legacy_modules():
    os.environ["XYN_SEED_ENABLE_LEGACY_PRODUCT"] = "false"

    for name in BANNED_MODULES + ["core.kernel_app"]:
        sys.modules.pop(name, None)

    importlib.import_module("core.kernel_app")

    leaked = [name for name in BANNED_MODULES if name in sys.modules]
    assert leaked == [], f"kernel imported legacy modules: {leaked}"


def test_dummy_artifact_router_loads_and_registers():
    with tempfile.TemporaryDirectory() as tmpdir:
        mod_path = Path(tmpdir) / "dummy_artifact.py"
        mod_path.write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/ping')\n"
            "async def ping():\n"
            "    return {'ok': True}\n",
            encoding="utf-8",
        )

        app = FastAPI()
        manifest = {
            "artifact": {"id": "dummy", "name": "dummy"},
            "roles": [
                {
                    "role": "api_router",
                    "entrypoint": "dummy_artifact:router",
                    "mount_path": "/dummy",
                    "pythonpath": [tmpdir],
                }
            ],
        }
        artifact_row = {"artifact_id": "dummy-1", "title": "Dummy", "enabled": True, "installed_state": "installed"}
        register_manifest_roles(app, manifest, artifact_row)

        client = TestClient(app)
        response = client.get("/dummy/ping")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


def test_root_mount_collision_fails_fast():
    with tempfile.TemporaryDirectory() as tmpdir:
        first_manifest = Path(tmpdir) / "first.manifest.json"
        second_manifest = Path(tmpdir) / "second.manifest.json"
        first_manifest.write_text(
            json.dumps(
                {
                    "artifact": {"id": "a", "name": "A"},
                    "roles": [{"role": "ui_mount", "mount_path": "/", "static_dir": tmpdir}],
                }
            ),
            encoding="utf-8",
        )
        second_manifest.write_text(
            json.dumps(
                {
                    "artifact": {"id": "b", "name": "B"},
                    "roles": [{"role": "ui_mount", "mount_path": "/", "static_dir": tmpdir}],
                }
            ),
            encoding="utf-8",
        )

        os.environ["XYN_KERNEL_BINDINGS_JSON"] = json.dumps(
            [
                {"artifact_id": "artifact-a", "manifest_ref": str(first_manifest), "enabled": True, "installed_state": "installed"},
                {"artifact_id": "artifact-b", "manifest_ref": str(second_manifest), "enabled": True, "installed_state": "installed"},
            ]
        )

        app = FastAPI()
        try:
            try:
                asyncio.run(load_workspace_artifacts_into_app(app))
                raise AssertionError("expected root mount collision to fail startup")
            except RuntimeError as exc:
                message = str(exc)
                assert "mount '/'" in message
                assert "artifact-a" in message
                assert "artifact-b" in message
        finally:
            os.environ.pop("XYN_KERNEL_BINDINGS_JSON", None)


def test_deterministic_routing_api_before_ui_root():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        module_path = tmp / "dummy_api_artifact.py"
        module_path.write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/health')\n"
            "async def health():\n"
            "    return {'service': 'api-ok'}\n",
            encoding="utf-8",
        )
        ui_dir = tmp / "ui"
        ui_dir.mkdir(parents=True, exist_ok=True)
        (ui_dir / "index.html").write_text("<html><body>ui-index</body></html>", encoding="utf-8")

        api_manifest = tmp / "api.manifest.json"
        api_manifest.write_text(
            json.dumps(
                {
                    "artifact": {"id": "api", "name": "API"},
                    "roles": [
                        {
                            "role": "api_router",
                            "entrypoint": "dummy_api_artifact:router",
                            "mount_path": "/api",
                            "pythonpath": [tmpdir],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        ui_manifest = tmp / "ui.manifest.json"
        ui_manifest.write_text(
            json.dumps(
                {
                    "artifact": {"id": "ui", "name": "UI"},
                    "roles": [{"role": "ui_mount", "mount_path": "/", "static_dir": str(ui_dir)}],
                }
            ),
            encoding="utf-8",
        )

        # Deliberately put UI first to verify kernel role ordering is deterministic.
        os.environ["XYN_KERNEL_BINDINGS_JSON"] = json.dumps(
            [
                {"artifact_id": "ui-artifact", "manifest_ref": str(ui_manifest), "enabled": True, "installed_state": "installed"},
                {"artifact_id": "api-artifact", "manifest_ref": str(api_manifest), "enabled": True, "installed_state": "installed"},
            ]
        )

        app = FastAPI()
        try:
            asyncio.run(load_workspace_artifacts_into_app(app))
            client = TestClient(app)

            api_response = client.get("/api/health")
            assert api_response.status_code == 200
            assert api_response.json() == {"service": "api-ok"}

            ui_response = client.get("/")
            assert ui_response.status_code == 200
            assert "ui-index" in ui_response.text
        finally:
            os.environ.pop("XYN_KERNEL_BINDINGS_JSON", None)


def test_hello_artifact_manifest_loads_api_and_ui_roles():
    manifest_path = ROOT.parent / "xyn-ui" / "apps" / "hello-artifact" / "artifact.manifest.json"
    assert manifest_path.exists(), f"missing manifest: {manifest_path}"

    previous_roots = os.environ.get("XYN_KERNEL_MANIFEST_ROOTS")
    os.environ["XYN_KERNEL_MANIFEST_ROOTS"] = str(ROOT.parent)
    os.environ["XYN_KERNEL_BINDINGS_JSON"] = json.dumps(
        [
            {
                "artifact_id": "hello-app-artifact",
                "title": "Hello App",
                "manifest_ref": str(manifest_path),
                "enabled": True,
                "installed_state": "installed",
            }
        ]
    )

    app = FastAPI()
    try:
        asyncio.run(load_workspace_artifacts_into_app(app))
        client = TestClient(app)

        api_response = client.get("/api/apps/hello/ping")
        assert api_response.status_code == 200
        assert api_response.json() == {"ok": True, "app": "hello"}

        ui_response = client.get("/apps/hello")
        assert ui_response.status_code in {200, 307, 308}
    finally:
        if previous_roots is None:
            os.environ.pop("XYN_KERNEL_MANIFEST_ROOTS", None)
        else:
            os.environ["XYN_KERNEL_MANIFEST_ROOTS"] = previous_roots
        os.environ.pop("XYN_KERNEL_BINDINGS_JSON", None)


if __name__ == "__main__":
    test_kernel_boot_does_not_import_legacy_modules()
    test_dummy_artifact_router_loads_and_registers()
    test_root_mount_collision_fails_fast()
    test_deterministic_routing_api_before_ui_root()
    test_hello_artifact_manifest_loads_api_and_ui_roles()
    print("ok - test_kernel_loader")
