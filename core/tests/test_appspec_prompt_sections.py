from __future__ import annotations

import unittest

from core.appspec.prompt_sections import (
    _build_structured_plan_snapshot,
    _extract_objective_sections,
    _extract_prompt_sections,
    _extract_workflow_blocks_from_prompt,
    _pick_prompt_section,
)


STRUCTURED_PROMPT = """
Build an application named "Real Estate Deal Finder".

### 1. Application Overview
St. Louis City MVP focused on parcel-centered distress signals.

### 2. Domain Model (REQUIRED DETAIL)
- Parcel (HANDLE canonical identifier)
- Property
- Campaign
- Signal
- Data Source

### 3. Workflow Definitions (REQUIRED)
#### Campaign Workflow
- create campaign
- map rectangle selection

### 4. Platform Primitive Composition
Use existing canonical platform facilities for geospatial services and PostGIS-backed spatial handling.

### 7. UI Surface Definition
- campaign list view
- map selection view
"""


OBJECTIVE_PROMPT = (
    'Build a simple internal web app called "Team Lunch Poll". Requirements: Core entities: '
    '1. Poll - title - poll_date - status (draft, open, closed, selected) '
    '2. Lunch Option - poll - name - restaurant - notes - active (yes/no) '
    'Behavior: - Users can create a poll and vote. '
    'Views / usability: - List all polls '
    'Validation / rules: - Prevent voting on polls that are not open.'
)


class AppSpecPromptSectionsTests(unittest.TestCase):
    def test_extract_objective_sections_parses_expected_buckets(self):
        sections = _extract_objective_sections(OBJECTIVE_PROMPT)
        self.assertTrue(sections["core_entities"])
        self.assertTrue(sections["behavior"])
        self.assertTrue(sections["views"])
        self.assertTrue(sections["validation"])

    def test_extract_prompt_sections_and_pick_section(self):
        sections = _extract_prompt_sections(STRUCTURED_PROMPT)
        self.assertIn("1. application overview", sections)
        self.assertTrue(_pick_prompt_section(sections, "application overview"))

    def test_workflow_blocks_and_structured_snapshot(self):
        blocks = _extract_workflow_blocks_from_prompt(STRUCTURED_PROMPT)
        self.assertTrue(blocks)
        self.assertEqual(blocks[0]["workflow_key"], "campaign")
        self.assertIn("geospatial", blocks[0]["requires_primitives"])

        snapshot = _build_structured_plan_snapshot(STRUCTURED_PROMPT)
        self.assertTrue(snapshot["application_overview"])
        self.assertTrue(snapshot["workflow_definitions"])
        self.assertIn("map selection", snapshot["ui_surfaces"].lower())


if __name__ == "__main__":
    unittest.main()
