from __future__ import annotations

import unittest

from core.appspec.entity_inference import (
    _build_entity_contracts_from_prompt,
    _extract_app_name_from_prompt,
    _infer_entities_from_app_spec,
    _infer_entities_from_prompt,
    _infer_requested_visuals_from_prompt,
)


TEAM_LUNCH_POLL_PROMPT = (
    'Build a simple internal web app called "Team Lunch Poll". Purpose: Let a small team propose lunch options, vote '
    'on them, and mark one option as the selected choice for the day. Requirements: Core entities: '
    '1. Poll - title - poll_date - status (draft, open, closed, selected) '
    '2. Lunch Option - poll - name - restaurant - notes - active (yes/no) '
    '3. Vote - poll - lunch option - voter_name - created_at '
    'Behavior: - Users can create a poll, add lunch options, and cast votes. '
    'Behavior: - When a Lunch Option is selected for a poll, the poll status should become selected automatically. '
    'Validation / rules: - Prevent voting on polls that are not open.'
)


class AppSpecEntityInferenceTests(unittest.TestCase):
    def test_extract_app_name_from_prompt(self):
        name = _extract_app_name_from_prompt(TEAM_LUNCH_POLL_PROMPT, fallback="Fallback")
        self.assertEqual(name, "Team Lunch Poll")

    def test_build_entity_contracts_from_prompt(self):
        contracts = _build_entity_contracts_from_prompt(TEAM_LUNCH_POLL_PROMPT)
        keys = {row["key"] for row in contracts}
        self.assertEqual(keys, {"polls", "lunch_options", "votes"})
        lunch_options = next(row for row in contracts if row["key"] == "lunch_options")
        field_names = {field["name"] for field in lunch_options["fields"]}
        self.assertIn("poll_id", field_names)
        self.assertIn("selected", field_names)

    def test_infer_entities_and_visuals_from_prompt(self):
        entities = _infer_entities_from_prompt(TEAM_LUNCH_POLL_PROMPT)
        self.assertIn("polls", entities)
        self.assertIn("votes", entities)

        visuals = _infer_requested_visuals_from_prompt("Show a chart report for interfaces by status")
        self.assertEqual(visuals, ["interfaces_by_status_chart"])

    def test_infer_entities_from_app_spec_fallbacks(self):
        app_spec = {
            "services": [{"name": "net-inventory-api"}],
            "reports": ["interfaces_by_status"],
            "source_prompt": "Build a network inventory app with devices and locations",
        }
        entities = _infer_entities_from_app_spec(app_spec)
        self.assertIn("devices", entities)
        self.assertIn("locations", entities)
        self.assertIn("interfaces", entities)


if __name__ == "__main__":
    unittest.main()
