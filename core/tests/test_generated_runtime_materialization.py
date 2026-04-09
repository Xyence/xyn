from __future__ import annotations

import tempfile
import unittest
import uuid
import json
import zipfile
from pathlib import Path
from unittest import mock

from core.app_jobs import _build_app_spec, _build_policy_bundle, _materialize_net_inventory_compose, _package_generated_app, _prefer_local_platform_images_for_smoke
from core.provisioning_local import (
    ProvisionLocalRequest,
    _bootstrap_remote_default_agent,
    _compose_yaml,
    _compose_down_cmd,
    _ensure_remote_workspace,
    _resolve_images_for_provision,
)


class GeneratedRuntimeMaterializationTests(unittest.TestCase):
    def test_bootstrap_remote_default_agent_serializes_datetime_payload(self):
        with mock.patch(
            "core.provisioning_local._run",
            return_value=(
                0,
                '{"status":"ok","last_bootstrap_at":"2026-04-07 14:00:00+00:00"}\n',
                "",
            ),
        ) as run_mock:
            payload = _bootstrap_remote_default_agent(api_container_name="xyn-local-api")

        self.assertIsInstance(payload, dict)
        self.assertEqual(payload.get("status"), "ok")
        run_args = run_mock.call_args[0][0]
        self.assertIn("manage.py", run_args)
        self.assertIn("json.dumps(payload, default=str)", run_args[-1])

    def test_generated_package_includes_policy_bundle_artifact(self):
        workspace_id = uuid.uuid4()
        app_spec = _build_app_spec(
            workspace_id=workspace_id,
            title="Team Lunch Poll",
            raw_prompt=(
                'Build a simple internal web app called "Team Lunch Poll". Purpose: Let a small team propose lunch options. '
                "Requirements: Core entities: 1. Poll - title - poll_date - status (draft, open, closed, selected) "
                "2. Lunch Option - poll - name - restaurant - notes - active (yes/no) "
                "3. Vote - poll - lunch option - voter_name - created_at "
                "Behavior: - When a Lunch Option is selected for a poll, the poll status should become selected automatically. "
                "Behavior: - Only one Lunch Option can be selected for a poll. "
                "Behavior: - A poll in selected status must have exactly one selected Lunch Option. "
                "Views / usability: - View a poll with its options and vote counts. "
                "Validation / rules: - Prevent voting on polls that are not open."
            ),
        )
        policy_bundle = _build_policy_bundle(
            workspace_id=workspace_id,
            app_spec=app_spec,
            raw_prompt="Validation / rules: - Prevent voting on polls that are not open.",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("core.app_jobs._generated_artifacts_root", return_value=Path(tmpdir)):
                packaged = _package_generated_app(
                    workspace_id=workspace_id,
                    source_job_id="job-1",
                    app_spec=app_spec,
                    policy_bundle=policy_bundle,
                    runtime_config={},
                )
            with zipfile.ZipFile(packaged["artifact_package_path"], "r") as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                surfaces = json.loads(
                    archive.read("artifacts/application/app.team-lunch-poll/0.0.1-dev/surfaces.json").decode("utf-8")
                )

        refs = {(row["type"], row["slug"]) for row in manifest["artifacts"]}
        self.assertIn(("application", "app.team-lunch-poll"), refs)
        self.assertIn(("policy_bundle", "policy.team-lunch-poll"), refs)
        self.assertEqual(packaged["policy_bundle_slug"], "policy.team-lunch-poll")
        self.assertTrue(isinstance(surfaces, list))

    def test_generated_package_surfaces_are_installer_compatible_and_manifest_nav_is_present(self):
        workspace_id = uuid.uuid4()
        app_spec = {
            "schema_version": "xyn.appspec.v0",
            "app_slug": "deal-finder",
            "title": "Deal Finder",
            "workspace_id": str(workspace_id),
            "entities": ["campaigns", "signals", "sources"],
            "entity_contracts": [
                {
                    "key": "campaigns",
                    "singular_label": "campaign",
                    "plural_label": "campaigns",
                    "collection_path": "/campaigns",
                    "item_path_template": "/campaigns/{id}",
                    "operations": {
                        "list": {"declared": True, "method": "GET", "path": "/campaigns"},
                        "get": {"declared": True, "method": "GET", "path": "/campaigns/{id}"},
                        "create": {"declared": True, "method": "POST", "path": "/campaigns"},
                    },
                    "fields": [
                        {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
                        {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
                        {"name": "name", "type": "string", "required": True, "readable": True, "writable": True, "identity": True},
                    ],
                    "presentation": {"default_list_fields": ["name"], "default_detail_fields": ["id", "name"], "title_field": "name"},
                    "validation": {"required_on_create": ["workspace_id", "name"], "allowed_on_update": ["name"]},
                    "relationships": [],
                }
            ],
            "workflow_definitions": [{"workflow_key": "campaign-workflow", "description": "campaign map selection"}],
            "platform_primitive_composition": [{"workflow_key": "campaign-workflow", "primitives": ["campaigns"]}],
            "ui_surfaces": "campaign list view; campaign detail view",
            "services": [],
            "reports": [],
            "requires_primitives": ["location"],
        }
        policy_bundle = {"workspace_id": str(workspace_id), "title": "Deal Finder Policies", "policy_families": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("core.app_jobs._generated_artifacts_root", return_value=Path(tmpdir)):
                packaged = _package_generated_app(
                    workspace_id=workspace_id,
                    source_job_id="job-1",
                    app_spec=app_spec,
                    policy_bundle=policy_bundle,
                    runtime_config={},
                )
            with zipfile.ZipFile(packaged["artifact_package_path"], "r") as archive:
                manifest_payload = json.loads(
                    archive.read("artifacts/application/app.deal-finder/0.0.1-dev/artifact.json").decode("utf-8")
                )
                surfaces = json.loads(
                    archive.read("artifacts/application/app.deal-finder/0.0.1-dev/surfaces.json").decode("utf-8")
                )

        self.assertTrue(surfaces)
        allowed_surface_kinds = {"config", "editor", "dashboard", "visualizer", "docs"}
        allowed_renderer_types = {"ui_component_ref", "generic_editor", "generic_dashboard", "workflow_visualizer", "article_editor"}
        for row in surfaces:
            self.assertIn(row.get("surface_kind"), allowed_surface_kinds)
            self.assertIn((row.get("renderer") or {}).get("type"), allowed_renderer_types)
        self.assertTrue(all(str(row.get("nav_visibility") or "") in {"always", "hidden"} for row in surfaces))
        self.assertTrue(any(str(row.get("route") or "").endswith("/:id") for row in surfaces))
        surface_routes = {str(row.get("route") or "") for row in surfaces}
        self.assertEqual(
            surface_routes,
            {
                "/app/campaigns",
                "/app/campaigns/new",
                "/app/campaigns/:id",
            },
        )
        campaigns_create = next((row for row in surfaces if row.get("route") == "/app/campaigns/new"), {})
        campaigns_detail = next((row for row in surfaces if row.get("route") == "/app/campaigns/:id"), {})
        self.assertEqual(
            ((campaigns_create.get("renderer") or {}).get("payload") or {}).get("shell_renderer_key"),
            "campaign_map_workflow",
        )
        self.assertEqual(
            ((campaigns_detail.get("renderer") or {}).get("payload") or {}).get("campaign_id_param"),
            "id",
        )
        nav = ((manifest_payload.get("surfaces") or {}).get("nav")) if isinstance(manifest_payload.get("surfaces"), dict) else []
        self.assertTrue(isinstance(nav, list) and nav)
        nav_paths = {str(row.get("path") or "") for row in nav if isinstance(row, dict)}
        self.assertIn("/app/campaigns", nav_paths)
        self.assertIn("/app/campaigns/new", nav_paths)

    def test_compose_injects_manifest_entity_contracts(self):
        app_spec = {
            "app_slug": "net-inventory",
            "title": "Network Inventory App",
            "workspace_id": "workspace-1",
            "entities": ["devices", "locations"],
            "reports": ["devices_by_status"],
            "services": [
                {"name": "net-inventory-api", "image": "net-inventory-api:local", "ports": [{"host": 0, "container": 8080, "protocol": "tcp"}]},
                {"name": "net-inventory-db", "image": "postgres:16-alpine"},
            ],
            "requires_primitives": ["location"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            compose_path = _materialize_net_inventory_compose(
                app_spec=app_spec,
                deployment_dir=Path(tmpdir),
                compose_project="xyn-app-net-inventory",
            )
            text = compose_path.read_text(encoding="utf-8")

        self.assertIn("GENERATED_ENTITY_CONTRACTS_JSON", text)
        self.assertIn("GENERATED_ENTITY_CONTRACTS_ALLOW_DEFAULTS", text)
        self.assertIn("GENERATED_WORKFLOW_DEFINITIONS_JSON", text)
        self.assertIn("GENERATED_PLATFORM_PRIMITIVE_COMPOSITION_JSON", text)
        self.assertIn("GENERATED_REQUIRES_PRIMITIVES_JSON", text)
        self.assertIn("GENERATED_UI_SURFACES_TEXT", text)
        self.assertIn("SHELL_BASE_URL", text)
        self.assertIn('"key":"devices"', text)
        self.assertIn('"key":"locations"', text)

    def test_compose_injects_policy_bundle_for_generated_runtime_enforcement(self):
        app_spec = _build_app_spec(
            workspace_id=uuid.uuid4(),
            title="Team Lunch Poll",
            raw_prompt=(
                'Build a simple internal web app called "Team Lunch Poll". Purpose: Let a small team propose lunch options. '
                "Requirements: Core entities: 1. Poll - title - poll_date - status (draft, open, closed, selected) "
                "2. Lunch Option - poll - name - restaurant - notes - active (yes/no) "
                "3. Vote - poll - lunch option - voter_name - created_at "
                "Behavior: - When a Lunch Option is selected for a poll, the poll status should become selected automatically. "
                "Behavior: - Only one Lunch Option can be selected for a poll. "
                "Behavior: - A poll in selected status must have exactly one selected Lunch Option. "
                "Views / usability: - View a poll with its options and vote counts. "
                "Validation / rules: - Prevent voting on polls that are not open."
            ),
        )
        policy_bundle = _build_policy_bundle(
            workspace_id=uuid.uuid4(),
            app_spec=app_spec,
            raw_prompt=(
                "Behavior: - When a Lunch Option is selected for a poll, the poll status should become selected automatically. "
                "Behavior: - Only one Lunch Option can be selected for a poll. "
                "Behavior: - A poll in selected status must have exactly one selected Lunch Option. "
                "Views / usability: - View a poll with its options and vote counts. "
                "Validation / rules: - Prevent voting on polls that are not open."
            ),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            compose_path = _materialize_net_inventory_compose(
                app_spec=app_spec,
                policy_bundle=policy_bundle,
                deployment_dir=Path(tmpdir),
                compose_project="xyn-app-team-lunch-poll",
            )
            text = compose_path.read_text(encoding="utf-8")

        self.assertIn("GENERATED_POLICY_BUNDLE_JSON", text)
        self.assertIn("parent_status_gate", text)
        self.assertIn("at_most_one_matching_child_per_parent", text)
        self.assertIn("at_least_one_matching_child_per_parent", text)
        self.assertIn("related_count", text)
        self.assertIn("post_write_related_update", text)

    def test_compose_injects_generic_contracts_for_unknown_entities(self):
        app_spec = {
            "app_slug": "deal-finder",
            "title": "Real Estate Deal Finder",
            "workspace_id": "workspace-1",
            "entities": ["campaigns", "properties", "signals", "sources", "watches"],
            "reports": [],
            "services": [
                {"name": "deal-finder-api", "image": "deal-finder-api:local", "ports": [{"host": 0, "container": 8080, "protocol": "tcp"}]},
                {"name": "deal-finder-db", "image": "postgres:16-alpine"},
            ],
            "requires_primitives": ["location"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            compose_path = _materialize_net_inventory_compose(
                app_spec=app_spec,
                deployment_dir=Path(tmpdir),
                compose_project="xyn-app-deal-finder",
            )
            text = compose_path.read_text(encoding="utf-8")
        self.assertIn('"key":"campaigns"', text)
        self.assertIn('"key":"properties"', text)
        self.assertIn('"key":"signals"', text)
        self.assertIn('"key":"sources"', text)
        self.assertIn('"key":"watches"', text)

    def test_workspace_seed_creates_missing_workspace(self):
        class _FakeResponse:
            def __init__(self, status: int, body: str = "", headers: dict[str, str] | None = None):
                self.status = status
                self._body = body.encode("utf-8")
                self.headers = headers or {}

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        opener = mock.Mock()
        opener.open.side_effect = [
            _FakeResponse(302, headers={"Set-Cookie": "sessionid=abc123; Path=/"}),
            _FakeResponse(200, body='{"workspaces":[{"id":"default-1","slug":"default"}]}'),
            _FakeResponse(201, body='{"workspace":{"id":"w-1","slug":"epicb-lab"}}'),
        ]
        with mock.patch("core.provisioning_local.urllib.request.build_opener", return_value=opener):
            result = _ensure_remote_workspace(
                api_url="http://api.example.test",
                workspace_slug="epicb-lab",
                workspace_title="Epicb Lab",
            )

        self.assertEqual(result["status"], "created")
        self.assertEqual(result["workspace_slug"], "epicb-lab")
        self.assertEqual(opener.open.call_count, 3)

    @mock.patch("core.provisioning_local.SessionLocal")
    @mock.patch("core.provisioning_local.resolve_registry_images")
    def test_provision_prefers_artifact_registry_by_default(self, resolve_registry_images, session_local):
        session_local.return_value = mock.Mock()
        resolve_registry_images.return_value = {
            "registry": {"endpoint": "public.ecr.aws/i0h0h0n4/xyn/artifacts"},
            "images": {
                "ui_image": "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:dev",
                "api_image": "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:dev",
                "channel": "dev",
            },
            "registry_slug": "default-registry",
            "registry_source": "default-registry",
            "operations": ["Using ArtifactRegistry: default-registry"],
        }
        with mock.patch("core.provisioning_local._docker_image_exists", return_value=True):
            result = _resolve_images_for_provision(ProvisionLocalRequest(name="smoke"))

        self.assertEqual(result["mode"], "artifact_registry")
        self.assertEqual(result["api_image"], "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:dev")
        self.assertEqual(result["ui_image"], "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:dev")
        resolve_registry_images.assert_called_once()

    @mock.patch("core.provisioning_local.SessionLocal")
    @mock.patch("core.provisioning_local.resolve_registry_images")
    def test_provision_applies_per_image_explicit_overrides(self, resolve_registry_images, session_local):
        session_local.return_value = mock.Mock()
        resolve_registry_images.return_value = {
            "registry": {"endpoint": "public.ecr.aws/i0h0h0n4/xyn/artifacts"},
            "images": {
                "ui_image": "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:develop",
                "api_image": "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:83915e4",
                "channel": "develop",
            },
            "registry_slug": "default-registry",
            "registry_source": "default-registry",
            "operations": [],
        }
        with mock.patch("core.provisioning_local._docker_image_exists", return_value=True):
            result = _resolve_images_for_provision(
                ProvisionLocalRequest(
                    name="smoke",
                    api_image="public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:83915e4",
                )
            )

        self.assertEqual(result["api_image"], "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:83915e4")
        self.assertEqual(result["ui_image"], "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:develop")
        self.assertEqual(result["mode"], "artifact_registry_with_explicit_overrides")
        _args, kwargs = resolve_registry_images.call_args
        self.assertEqual(
            kwargs.get("explicit_api_image"),
            "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:83915e4",
        )

    @mock.patch("core.provisioning_local._docker_image_exists", return_value=True)
    @mock.patch("core.provisioning_local._running_container_image_ref")
    def test_provision_prefers_prebuilt_local_tags_before_running_container_refs(self, running_container_image_ref, _docker_image_exists):
        running_container_image_ref.side_effect = [
            "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:dev",
            "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:dev",
        ]

        result = _resolve_images_for_provision(ProvisionLocalRequest(name="smoke", prefer_local_images=True))

        self.assertEqual(result["mode"], "prebuilt_local_images")
        self.assertEqual(result["api_image"], "xyn-api")
        self.assertEqual(result["ui_image"], "xyn-ui")

    @mock.patch("core.provisioning_local._docker_image_exists", return_value=True)
    @mock.patch("core.provisioning_local._running_container_image_ref")
    def test_provision_prefers_local_workspace_build_when_requested(self, running_container_image_ref, _docker_image_exists):
        running_container_image_ref.side_effect = [
            "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-api:dev",
            "public.ecr.aws/i0h0h0n4/xyn/artifacts/xyn-ui:dev",
        ]

        def _run(cmd, *args, **kwargs):
            context = cmd[-1]
            if context in {"/tmp/src/xyn-platform/services/xyn-api", "/tmp/src/xyn-platform/apps/xyn-ui"}:
                return (0, "", "")
            return (1, "", f"missing context: {context}")

        with mock.patch("core.provisioning_local._run", side_effect=_run):
            with mock.patch.dict("os.environ", {"XYN_HOST_SRC_ROOT": "/tmp/src"}, clear=False):
                result = _resolve_images_for_provision(
                    ProvisionLocalRequest(name="smoke", prefer_local_images=True, prefer_local_sources=True)
                )

        self.assertEqual(result["mode"], "local_workspace")
        self.assertEqual(result["api_image"], "xyn-api")
        self.assertEqual(result["ui_image"], "xyn-ui")
        self.assertEqual(result["registry_source"], "local_workspace")
        self.assertIn("Built local image xyn-api from /tmp/src/xyn-platform/services/xyn-api", result["operations"])
        self.assertIn("Built local image xyn-ui from /tmp/src/xyn-platform/apps/xyn-ui", result["operations"])

    def test_provision_can_opt_into_local_images(self):
        def _run(cmd, *args, **kwargs):
            context = cmd[-1]
            if context in {"/tmp/src/xyn-platform/services/xyn-api", "/tmp/src/xyn-platform/apps/xyn-ui"}:
                return (0, "", "")
            return (1, "", f"missing context: {context}")

        with mock.patch("core.provisioning_local._running_container_image_ref", return_value=""):
            with mock.patch("core.provisioning_local._run", side_effect=_run):
                with mock.patch.dict("os.environ", {"XYN_HOST_SRC_ROOT": "/tmp/src"}, clear=False):
                    result = _resolve_images_for_provision(ProvisionLocalRequest(name="smoke", prefer_local_images=True))

        self.assertEqual(result["mode"], "local_build")
        self.assertEqual(result["api_image"], "xyn-api")
        self.assertEqual(result["ui_image"], "xyn-ui")
        self.assertIn("Built local image xyn-api from /tmp/src/xyn-platform/services/xyn-api", result["operations"])
        self.assertIn("Built local image xyn-ui from /tmp/src/xyn-platform/apps/xyn-ui", result["operations"])

    @mock.patch("core.provisioning_local._docker_image_exists", return_value=True)
    def test_provision_falls_back_to_prebuilt_local_images_when_sources_are_missing(self, _docker_image_exists):
        with mock.patch("core.provisioning_local._running_container_image_ref", return_value=""):
            with mock.patch.dict("os.environ", {"XYN_HOST_SRC_ROOT": "/tmp/src"}, clear=False):
                with mock.patch("core.provisioning_local._run", return_value=(1, "", "missing context")):
                    result = _resolve_images_for_provision(ProvisionLocalRequest(name="smoke", prefer_local_images=True))

        self.assertEqual(result["mode"], "prebuilt_local_images")
        self.assertEqual(result["api_image"], "xyn-api")
        self.assertEqual(result["ui_image"], "xyn-ui")

    def test_app_smoke_prefers_local_platform_images_by_default(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            self.assertTrue(_prefer_local_platform_images_for_smoke())

    def test_app_smoke_can_opt_out_of_local_platform_images(self):
        with mock.patch.dict("os.environ", {"XYN_APP_SMOKE_PREFER_LOCAL_IMAGES": "false"}, clear=False):
            self.assertFalse(_prefer_local_platform_images_for_smoke())

    def test_compose_down_cmd_is_non_destructive_by_default(self):
        cmd = _compose_down_cmd(project="xyn-local", compose_path=Path("/tmp/compose.yaml"))
        self.assertEqual(
            cmd,
            ["docker", "compose", "-p", "xyn-local", "-f", "/tmp/compose.yaml", "down", "--remove-orphans"],
        )

    def test_compose_down_cmd_includes_volumes_only_for_explicit_reset(self):
        cmd = _compose_down_cmd(project="xyn-local", compose_path=Path("/tmp/compose.yaml"), reset_state=True)
        self.assertEqual(
            cmd,
            ["docker", "compose", "-p", "xyn-local", "-f", "/tmp/compose.yaml", "down", "--remove-orphans", "--volumes"],
        )

    def test_local_compose_backend_includes_repo_mounts_and_runtime_repo_map(self):
        compose_text = _compose_yaml(
            "xyn-local",
            ui_image="xyn-ui:latest",
            api_image="xyn-api:latest",
            ui_host="localhost",
            api_host="api.localhost",
            auth_mode="token",
        )
        self.assertIn(
            "XYN_RUNTIME_REPO_MAP: '${XYN_RUNTIME_REPO_MAP:-{\"xyn\":[\"/workspace/xyn\"],\"xyn-platform\":[\"/workspace/xyn-platform\"]}}'",
            compose_text,
        )
        self.assertIn("XYN_OIDC_CLIENT_SECRET: ${XYN_OIDC_CLIENT_SECRET:-}", compose_text)
        self.assertIn("${XYN_HOST_SRC_ROOT:-${PWD}/..}/xyn:/workspace/xyn", compose_text)
        self.assertIn(
            "${XYN_PLATFORM_HOST_SRC_PATH:-${XYN_HOST_SRC_ROOT:-${PWD}/..}/xyn-platform}:/workspace/xyn-platform",
            compose_text,
        )
        self.assertIn("XYN_AUTH_MODE: token", compose_text)

    def test_compose_uses_external_db_without_local_postgres_when_db_mode_external(self):
        with mock.patch.dict("os.environ", {"XYN_DB_MODE": "external"}, clear=False):
            compose_text = _compose_yaml(
                "xyn-local",
                ui_image="xyn-ui:latest",
                api_image="xyn-api:latest",
                ui_host="xyn.xyence.io",
                api_host="xyn.xyence.io",
                auth_mode="oidc",
            )
        self.assertNotIn("image: postgres:16-alpine", compose_text)
        self.assertNotIn("postgres_data:/var/lib/postgresql/data", compose_text)
        self.assertIn("DATABASE_URL: ${DATABASE_URL:-}", compose_text)
        self.assertNotIn("POSTGRES_HOST: postgres", compose_text)


if __name__ == "__main__":
    unittest.main()
