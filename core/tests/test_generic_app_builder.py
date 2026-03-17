from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from core.app_jobs import (
    _build_app_spec,
    _build_policy_bundle,
    _ensure_parent_status_gate_prerequisites,
    _materialize_net_inventory_compose,
)


TEAM_LUNCH_POLL_PROMPT = (
    'Build a simple internal web app called "Team Lunch Poll". Purpose: Let a small team propose lunch options, vote '
    "on them, and mark one option as the selected choice for the day. Requirements: Core entities: "
    "1. Poll - title - poll_date - status (draft, open, closed, selected) "
    "2. Lunch Option - poll - name - restaurant - notes - active (yes/no) "
    "3. Vote - poll - lunch option - voter_name - created_at "
    "Behavior: - Users can create a poll, add lunch options, and cast votes. "
    "- When a Lunch Option is selected for a poll, the poll status should become selected automatically. "
    "- Only one Lunch Option can be selected for a poll. "
    "- A poll in selected status must have exactly one selected Lunch Option. "
    "Views / usability: - List all polls - View a poll with its options and vote counts. "
    "Validation / rules: - Prevent voting on polls that are not open."
)


class GenericAppBuilderTests(unittest.TestCase):
    def test_team_lunch_poll_prompt_generates_non_inventory_semantics(self):
        workspace_id = uuid.uuid4()

        spec = _build_app_spec(
            workspace_id=workspace_id,
            title="Team Lunch Poll",
            raw_prompt=TEAM_LUNCH_POLL_PROMPT,
        )

        self.assertEqual(spec["app_slug"], "team-lunch-poll")
        self.assertEqual(spec["title"], "Team Lunch Poll")
        self.assertEqual(spec["services"][0]["name"], "team-lunch-poll-api")
        self.assertEqual(spec["services"][1]["name"], "team-lunch-poll-db")
        self.assertNotIn("devices", spec.get("entities") or [])
        self.assertNotIn("locations", spec.get("entities") or [])
        self.assertNotIn("devices_by_status", spec.get("reports") or [])
        self.assertTrue(isinstance(spec.get("entity_contracts"), list) and spec["entity_contracts"])
        contracts = {row["key"]: row for row in spec["entity_contracts"]}
        self.assertEqual(set(contracts), {"polls", "lunch_options", "votes"})
        self.assertIn("poll_id", {field["name"] for field in contracts["lunch_options"]["fields"]})
        self.assertIn("lunch_option_id", {field["name"] for field in contracts["votes"]["fields"]})
        lunch_option_fields = {field["name"]: field for field in contracts["lunch_options"]["fields"]}
        self.assertEqual(lunch_option_fields["active"]["options"], ["yes", "no"])
        self.assertEqual(lunch_option_fields["selected"]["options"], ["yes", "no"])
        vote_fields = [field["name"] for field in contracts["votes"]["fields"]]
        self.assertEqual(vote_fields.count("created_at"), 1)

    def test_generic_builder_does_not_silently_fall_back_to_inventory(self):
        with self.assertRaisesRegex(RuntimeError, "must not silently fall back to inventory semantics"):
            _build_app_spec(
                workspace_id=uuid.uuid4(),
                title="Mystery App",
                raw_prompt="Build a useful internal application for my team.",
            )

    def test_compose_uses_generated_service_identity_for_non_inventory_apps(self):
        spec = _build_app_spec(
            workspace_id=uuid.uuid4(),
            title="Team Lunch Poll",
            raw_prompt=TEAM_LUNCH_POLL_PROMPT,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            compose_path = _materialize_net_inventory_compose(
                app_spec=spec,
                deployment_dir=Path(tmpdir),
                compose_project="xyn-app-team-lunch-poll",
            )
            text = compose_path.read_text(encoding="utf-8")

        self.assertIn("team-lunch-poll-api", text)
        self.assertIn("team-lunch-poll-db", text)
        self.assertIn('SERVICE_NAME: "team-lunch-poll-api"', text)
        self.assertIn('APP_TITLE: "Team Lunch Poll"', text)
        self.assertNotIn("net-inventory-db:", text)

    def test_generated_apps_build_policy_bundle_by_default(self):
        workspace_id = uuid.uuid4()
        spec = _build_app_spec(
            workspace_id=workspace_id,
            title="Team Lunch Poll",
            raw_prompt=TEAM_LUNCH_POLL_PROMPT,
        )

        bundle = _build_policy_bundle(
            workspace_id=workspace_id,
            app_spec=spec,
            raw_prompt=TEAM_LUNCH_POLL_PROMPT,
        )

        self.assertEqual(bundle["schema_version"], "xyn.policy_bundle.v0")
        self.assertEqual(bundle["bundle_id"], "policy.team-lunch-poll")
        self.assertEqual(bundle["scope"]["artifact_slug"], "app.team-lunch-poll")
        self.assertIn("validation_policies", bundle["policies"])
        self.assertIn("transition_policies", bundle["policies"])
        self.assertIn("render_policy_bundle", bundle["explanation"]["future_capabilities"])
        documented = sum(len(bundle["policies"][key]) for key in bundle["policies"])
        self.assertGreater(documented, 0)
        compiled = [
            entry
            for family in bundle["policies"].values()
            for entry in family
            if isinstance(entry, dict) and entry.get("enforcement_stage") == "runtime_enforced"
        ]
        self.assertGreaterEqual(len(compiled), 3)
        runtime_rules = {
            str((entry.get("parameters") or {}).get("runtime_rule") or "")
            for entry in compiled
            if isinstance(entry.get("parameters"), dict)
        }
        self.assertIn("parent_status_gate", runtime_rules)
        self.assertIn("match_related_field", runtime_rules)
        self.assertIn("field_transition_guard", runtime_rules)
        self.assertIn("at_most_one_matching_child_per_parent", runtime_rules)
        self.assertIn("at_least_one_matching_child_per_parent", runtime_rules)
        self.assertIn("related_count", runtime_rules)
        self.assertIn("post_write_related_update", runtime_rules)
        gated_invariants = [
            entry
            for entry in compiled
            if str((entry.get("parameters") or {}).get("runtime_rule") or "") == "at_least_one_matching_child_per_parent"
        ]
        self.assertTrue(gated_invariants)
        first_gate = gated_invariants[0]["parameters"]
        self.assertEqual(first_gate.get("parent_state_field"), "status")
        self.assertEqual(first_gate.get("parent_state_value"), "selected")

    def test_policy_aware_smoke_primes_parent_status_before_child_create(self):
        workspace_id = str(uuid.uuid4())
        spec = _build_app_spec(
            workspace_id=uuid.uuid4(),
            title="Team Lunch Poll",
            raw_prompt=TEAM_LUNCH_POLL_PROMPT,
        )
        policy_bundle = _build_policy_bundle(
            workspace_id=uuid.uuid4(),
            app_spec=spec,
            raw_prompt=TEAM_LUNCH_POLL_PROMPT,
        )
        contracts = {row["key"]: row for row in spec["entity_contracts"]}
        poll_id = str(uuid.uuid4())
        created_records = {
            "polls": {
                "id": poll_id,
                "workspace_id": workspace_id,
                "status": "draft",
            }
        }
        with mock.patch(
            "core.app_jobs._container_http_json",
            return_value=(200, {"id": poll_id, "workspace_id": workspace_id, "status": "open"}, ""),
        ) as container_http_json:
            _ensure_parent_status_gate_prerequisites(
                container_name="xyn-app-team-lunch-poll-api",
                port=8080,
                workspace_id=workspace_id,
                contract=contracts["votes"],
                entity_contracts=spec["entity_contracts"],
                created_records=created_records,
                policy_bundle=policy_bundle,
            )

        self.assertEqual(created_records["polls"]["status"], "open")
        container_http_json.assert_called_once()
        args, kwargs = container_http_json.call_args
        self.assertEqual(kwargs["payload"], {"status": "open"})
        self.assertIn("/polls/", args[2])


if __name__ == "__main__":
    unittest.main()
