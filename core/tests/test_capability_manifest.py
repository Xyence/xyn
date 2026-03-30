import unittest
import uuid

from core.app_jobs import _build_app_spec, _build_generated_artifact_manifest
from core.capability_manifest import build_resolved_capability_manifest


class CapabilityManifestTests(unittest.TestCase):
    def test_resolved_manifest_separates_enabled_and_latent_capabilities(self):
        manifest = build_resolved_capability_manifest(
            {
                "app_slug": "net-inventory",
                "title": "Net Inventory",
                "workspace_id": str(uuid.uuid4()),
                "entities": ["devices", "locations"],
                "reports": ["devices_by_status"],
            }
        )
        enabled = {entry["key"] for entry in manifest["commands"]}
        latent = {entry["key"] for entry in manifest["diagnostics"]["latent_commands"]}
        self.assertEqual(
            enabled,
            {
                "show devices",
                "create device",
                "update device",
                "delete device",
                "show locations",
                "create location",
                "update location",
                "delete location",
                "show devices by status",
            },
        )
        self.assertIn("show interfaces", latent)
        self.assertIn("update interface", latent)
        self.assertIn("show interfaces by status", latent)

    def test_generated_artifact_manifest_excludes_undeclared_suggestions(self):
        workspace_id = uuid.uuid4()
        artifact_manifest = _build_generated_artifact_manifest(
            app_spec={
                "schema_version": "xyn.appspec.v0",
                "app_slug": "net-inventory",
                "title": "Net Inventory",
                "workspace_id": str(workspace_id),
                "entities": ["devices", "locations"],
                "reports": ["devices_by_status"],
            },
            runtime_config={},
        )
        prompts = {entry["prompt"] for entry in artifact_manifest["suggestions"]}
        diagnostics = artifact_manifest["resolved_capability_manifest"]["diagnostics"]
        latent_prompts = {entry["prompt"] for entry in diagnostics["latent_commands"]}
        self.assertNotIn("show interfaces", prompts)
        self.assertIn("show interfaces", latent_prompts)

    def test_resolved_manifest_includes_entity_crud_contract_for_devices_and_locations(self):
        manifest = build_resolved_capability_manifest(
            {
                "app_slug": "net-inventory",
                "title": "Net Inventory",
                "workspace_id": str(uuid.uuid4()),
                "entities": ["devices", "locations"],
                "reports": ["devices_by_status"],
            }
        )

        entities = {entry["key"]: entry for entry in manifest["entities"]}
        self.assertEqual(set(entities), {"devices", "locations"})

        devices = entities["devices"]
        self.assertEqual(devices["singular_label"], "device")
        self.assertEqual(devices["plural_label"], "devices")
        self.assertEqual(devices["collection_path"], "/devices")
        self.assertEqual(devices["item_path_template"], "/devices/{id}")
        self.assertTrue(devices["operations"]["list"]["declared"])
        self.assertTrue(devices["operations"]["get"]["declared"])
        self.assertTrue(devices["operations"]["create"]["declared"])
        self.assertTrue(devices["operations"]["update"]["declared"])
        self.assertTrue(devices["operations"]["delete"]["declared"])
        self.assertIn("name", devices["presentation"]["default_list_fields"])
        self.assertEqual(devices["presentation"]["title_field"], "name")
        device_fields = {field["name"]: field for field in devices["fields"]}
        self.assertTrue(device_fields["name"]["required"])
        self.assertTrue(device_fields["name"]["identity"])
        self.assertEqual(device_fields["location_id"]["relation"]["target_entity"], "locations")
        self.assertIn("workspace_id", devices["validation"]["required_on_create"])
        self.assertIn("status", devices["validation"]["allowed_on_update"])

        locations = entities["locations"]
        self.assertEqual(locations["collection_path"], "/locations")
        self.assertEqual(locations["item_path_template"], "/locations/{id}")
        self.assertTrue(locations["operations"]["list"]["declared"])
        self.assertTrue(locations["operations"]["get"]["declared"])
        self.assertTrue(locations["operations"]["create"]["declared"])
        location_fields = {field["name"]: field for field in locations["fields"]}
        self.assertIn("city", location_fields)
        self.assertEqual(locations["presentation"]["title_field"], "name")
        self.assertIn("country", locations["presentation"]["default_detail_fields"])

    def test_entity_contract_omits_undeclared_entities_and_only_declares_present_operations(self):
        manifest = build_resolved_capability_manifest(
            {
                "app_slug": "net-inventory",
                "title": "Net Inventory",
                "workspace_id": str(uuid.uuid4()),
                "entities": ["devices", "locations"],
                "reports": [],
            }
        )

        entities = {entry["key"]: entry for entry in manifest["entities"]}
        self.assertNotIn("interfaces", entities)
        self.assertTrue(entities["devices"]["operations"]["list"]["declared"])
        self.assertTrue(entities["devices"]["operations"]["create"]["declared"])
        self.assertTrue(entities["devices"]["operations"]["update"]["declared"])
        self.assertTrue(entities["devices"]["operations"]["delete"]["declared"])

    def test_evolution_updates_generated_manifest_with_new_capability(self):
        workspace_id = uuid.uuid4()
        current_app_spec = {
            "schema_version": "xyn.appspec.v0",
            "app_slug": "net-inventory",
            "title": "Net Inventory",
            "workspace_id": str(workspace_id),
            "entities": ["devices", "locations"],
            "reports": ["devices_by_status"],
        }
        evolved = _build_app_spec(
            workspace_id=workspace_id,
            title="Net Inventory",
            raw_prompt="Add interfaces and a chart that shows interfaces by status.",
            initial_intent={"requested_entities": ["interfaces"], "requested_visuals": ["interfaces_by_status_chart"]},
            current_app_spec=current_app_spec,
            current_app_summary={"entities": ["devices", "locations"], "reports": ["devices_by_status"]},
            revision_anchor={"anchor_type": "installed_generated_artifact"},
        )
        artifact_manifest = _build_generated_artifact_manifest(app_spec=evolved, runtime_config={})
        suggestions = {entry["prompt"] for entry in artifact_manifest["suggestions"]}
        enabled = {entry["key"] for entry in artifact_manifest["resolved_capability_manifest"]["commands"]}

        self.assertIn("show interfaces", suggestions)
        self.assertIn("update interface", suggestions)
        self.assertIn("delete interface", suggestions)
        self.assertIn("show interfaces by status", suggestions)
        self.assertIn("show interfaces", enabled)
        self.assertIn("update interface", enabled)
        self.assertIn("delete interface", enabled)
        self.assertIn("show interfaces by status", enabled)
        entities = {entry["key"]: entry for entry in artifact_manifest["resolved_capability_manifest"]["entities"]}
        self.assertIn("interfaces", entities)
        self.assertTrue(entities["interfaces"]["operations"]["list"]["declared"])
        self.assertTrue(entities["interfaces"]["operations"]["get"]["declared"])
        self.assertTrue(entities["interfaces"]["operations"]["create"]["declared"])
        self.assertTrue(entities["interfaces"]["operations"]["update"]["declared"])
        self.assertTrue(entities["interfaces"]["operations"]["delete"]["declared"])

    def test_explicit_entity_contracts_drive_generated_manifest_without_inventory_defaults(self):
        manifest = build_resolved_capability_manifest(
            {
                "app_slug": "team-lunch-poll",
                "title": "Team Lunch Poll",
                "workspace_id": str(uuid.uuid4()),
                "reports": [],
                "entity_contracts": [
                    {
                        "key": "polls",
                        "singular_label": "poll",
                        "plural_label": "polls",
                        "collection_path": "/polls",
                        "item_path_template": "/polls/{id}",
                        "operations": {
                            "list": {"declared": True, "method": "GET", "path": "/polls"},
                            "get": {"declared": True, "method": "GET", "path": "/polls/{id}"},
                            "create": {"declared": True, "method": "POST", "path": "/polls"},
                            "update": {"declared": True, "method": "PATCH", "path": "/polls/{id}"},
                            "delete": {"declared": True, "method": "DELETE", "path": "/polls/{id}"},
                        },
                        "fields": [
                            {"name": "id", "type": "uuid", "required": True, "readable": True, "writable": False, "identity": True},
                            {"name": "workspace_id", "type": "uuid", "required": True, "readable": True, "writable": True, "identity": False},
                            {"name": "title", "type": "string", "required": True, "readable": True, "writable": True, "identity": True},
                            {"name": "poll_date", "type": "string", "required": True, "readable": True, "writable": True, "identity": False},
                            {"name": "status", "type": "string", "required": True, "readable": True, "writable": True, "identity": False},
                        ],
                        "presentation": {"default_list_fields": ["title", "poll_date", "status"], "default_detail_fields": ["id", "title", "poll_date", "status"], "title_field": "title"},
                        "validation": {"required_on_create": ["workspace_id", "title", "poll_date", "status"], "allowed_on_update": ["title", "poll_date", "status"]},
                        "relationships": [],
                    }
                ],
            }
        )
        prompts = {entry["prompt"] for entry in manifest["commands"]}
        self.assertIn("show polls", prompts)
        self.assertIn("create poll", prompts)
        self.assertNotIn("show devices", prompts)
        self.assertEqual({entry["key"] for entry in manifest["entities"]}, {"polls"})

    def test_unknown_entities_from_app_spec_receive_generic_contracts(self):
        manifest = build_resolved_capability_manifest(
            {
                "app_slug": "deal-finder",
                "title": "Deal Finder",
                "workspace_id": str(uuid.uuid4()),
                "entities": ["campaigns", "properties", "signals", "sources", "watches"],
                "reports": [],
            }
        )
        entities = {entry["key"]: entry for entry in manifest["entities"]}
        self.assertEqual(set(entities), {"campaigns", "properties", "signals", "sources", "watches"})
        for key in ("campaigns", "properties", "signals", "sources", "watches"):
            contract = entities[key]
            self.assertEqual(contract["collection_path"], f"/{key}")
            self.assertEqual(contract["item_path_template"], f"/{key}/{{id}}")
            field_names = {field["name"] for field in contract["fields"]}
            self.assertIn("id", field_names)
            self.assertIn("workspace_id", field_names)
            self.assertIn("name", field_names)
            self.assertIn("created_at", field_names)
            self.assertTrue(contract["operations"]["create"]["declared"])
            self.assertTrue(contract["operations"]["update"]["declared"])
            self.assertTrue(contract["operations"]["delete"]["declared"])
        prompts = {entry["prompt"] for entry in manifest["commands"]}
        self.assertIn("show campaigns", prompts)
        self.assertIn("show properties", prompts)
        self.assertIn("show signals", prompts)

    def test_generated_artifact_manifest_includes_nav_surfaces_and_surface_definitions(self):
        workspace_id = uuid.uuid4()
        artifact_manifest = _build_generated_artifact_manifest(
            app_spec={
                "schema_version": "xyn.appspec.v0",
                "app_slug": "deal-finder",
                "title": "Deal Finder",
                "workspace_id": str(workspace_id),
                "entities": ["campaigns", "signals"],
                "reports": [],
                "workflow_definitions": [{"workflow_key": "campaign-workflow", "description": "campaign workflow"}],
                "ui_surfaces": "campaign list view; signal list view",
            },
            runtime_config={},
        )
        surfaces = artifact_manifest.get("surfaces") if isinstance(artifact_manifest.get("surfaces"), dict) else {}
        self.assertTrue(isinstance(surfaces.get("nav"), list) and surfaces.get("nav"))
        self.assertTrue(isinstance(surfaces.get("manage"), list) and surfaces.get("manage"))
        nav_paths = {str(row.get("path") or "") for row in (surfaces.get("nav") or []) if isinstance(row, dict)}
        self.assertIn("/app/campaigns/new", nav_paths)
        generated_defs = ((artifact_manifest.get("content") or {}).get("generated_surface_definitions"))
        self.assertTrue(isinstance(generated_defs, list) and generated_defs)
        routes = {str(row.get("route") or "") for row in generated_defs if isinstance(row, dict)}
        self.assertIn("/app/campaigns/new", routes)
        self.assertNotIn("/app/signals", routes)

    def test_resolved_manifest_views_only_include_modern_campaign_routes(self):
        manifest = build_resolved_capability_manifest(
            {
                "app_slug": "deal-finder",
                "title": "Deal Finder",
                "workspace_id": str(uuid.uuid4()),
                "entities": ["campaigns", "properties", "signals", "sources", "watches"],
                "reports": [],
                "workflow_definitions": [{"workflow_key": "map-selection", "description": "Map rectangle selection workflow"}],
                "ui_surfaces": "admin dashboards and map selection",
            }
        )
        view_paths = {str(row.get("path") or "") for row in (manifest.get("views") or []) if isinstance(row, dict)}
        self.assertEqual(view_paths, {"/app/campaigns", "/app/campaigns/new"})
        self.assertNotIn("/app/workbench", view_paths)
        self.assertNotIn("/app/admin", view_paths)
        self.assertNotIn("/app/map-selection", view_paths)
        self.assertNotIn("/app/properties", view_paths)


if __name__ == "__main__":
    unittest.main()
