from __future__ import annotations

import unittest
import uuid
from unittest import mock

from core.app_jobs import _build_app_spec
from core.appspec import canonicalize as appspec_canonicalize
from core.appspec import consistency as appspec_consistency
from core.appspec import semantic_extractor


class AppSpecHybridInferenceTests(unittest.TestCase):
    def test_duplicate_entity_normalization_in_canonicalize(self):
        interpretation = appspec_canonicalize.canonicalize_interpretation(
            route="B",
            existing_entities=[],
            summary_entities=[],
            requested_entities=["Campaigns"],
            deterministic_entities=["campaigns", "campaigns"],
            semantic_entities=["campaigns", "signals"],
            deterministic_contracts=[],
            semantic_contracts=[],
            requested_visuals=[],
            deterministic_visuals=[],
            semantic_visuals=[],
            primitive_keys=[],
        )
        keys = [row.key for row in interpretation.entities]
        self.assertEqual(keys.count("campaigns"), 1)
        self.assertIn("signals", keys)

    def test_contract_entity_consistency_adds_missing_entity(self):
        interpretation = appspec_canonicalize.canonicalize_interpretation(
            route="B",
            existing_entities=[],
            summary_entities=[],
            requested_entities=[],
            deterministic_entities=[],
            semantic_entities=[],
            deterministic_contracts=[{"key": "polls", "fields": []}],
            semantic_contracts=[],
            requested_visuals=[],
            deterministic_visuals=[],
            semantic_visuals=[],
            primitive_keys=[],
        )
        validated = appspec_consistency.validate_interpretation_consistency(interpretation)
        entity_keys = {row.key for row in validated.interpretation.entities}
        self.assertIn("polls", entity_keys)
        self.assertEqual(validated.warnings, [])

    def test_visual_entity_consistency_warns_on_missing_entity(self):
        interpretation = appspec_canonicalize.canonicalize_interpretation(
            route="B",
            existing_entities=[],
            summary_entities=[],
            requested_entities=[],
            deterministic_entities=["campaigns"],
            semantic_entities=[],
            deterministic_contracts=[],
            semantic_contracts=[],
            requested_visuals=[],
            deterministic_visuals=["interfaces_by_status_chart"],
            semantic_visuals=[],
            primitive_keys=[],
        )
        validated = appspec_consistency.validate_interpretation_consistency(interpretation)
        self.assertTrue(any("interfaces_by_status_chart" in row for row in validated.warnings))

    def test_route_a_for_structured_prompt(self):
        prompt = (
            "Build a simple app called Team Lunch Poll. Requirements: Core entities: "
            "1. Poll - title - status (draft, open, closed) "
            "2. Vote - poll - voter_name "
            "Behavior: - Users can vote. "
            "Views / usability: - List polls. "
            "Validation / rules: - Only open polls accept votes."
        )
        spec = _build_app_spec(workspace_id=uuid.uuid4(), title="Team Lunch Poll", raw_prompt=prompt)
        self.assertTrue(spec.get("entity_contracts"))

    def test_route_c_uses_semantic_entities_for_free_form_prompt(self):
        prompt = "Create a personal knowledgebase application to track personal notes and documents."
        with mock.patch(
            "core.appspec.semantic_extractor.extract_semantic_inference",
            return_value={
                "entities": ["notes", "documents"],
                "entity_contracts": [],
                "requested_visuals": [],
            },
        ):
            spec = _build_app_spec(workspace_id=uuid.uuid4(), title="Knowledgebase", raw_prompt=prompt)
        entities = set(spec.get("entities") or [])
        self.assertIn("notes", entities)
        self.assertIn("documents", entities)

    def test_route_b_augments_with_semantic_visuals(self):
        prompt = (
            "Build campaign management tooling.\n"
            "Core entities: 1. Campaign - name.\n"
            "Workflow: campaign workflow.\n"
            "Add status chart reporting."
        )
        with mock.patch(
            "core.appspec.semantic_extractor.extract_semantic_inference",
            return_value={
                "entities": ["campaigns"],
                "entity_contracts": [],
                "requested_visuals": ["devices_by_status_chart"],
            },
        ):
            spec = _build_app_spec(workspace_id=uuid.uuid4(), title="Campaign Tooling", raw_prompt=prompt)
        visuals = set(spec.get("requested_visuals") or [])
        self.assertIn("devices_by_status_chart", visuals)

    def test_route_b_keeps_deterministic_contracts_authoritative(self):
        prompt = (
            "Build voting tool. Core entities: "
            "1. Poll - title - status (draft, open, closed) "
            "2. Vote - poll - voter_name."
        )
        with mock.patch(
            "core.appspec.semantic_extractor.extract_semantic_inference",
            return_value={
                "entities": ["notes"],
                "entity_contracts": [
                    {
                        "key": "polls",
                        "fields": [{"name": "broken_field", "type": "string"}],
                    },
                    {"key": "notes", "fields": [{"name": "name", "type": "string"}]},
                ],
                "requested_visuals": [],
            },
        ):
            spec = _build_app_spec(workspace_id=uuid.uuid4(), title="Voting Tool", raw_prompt=prompt)
        contracts = {str(row.get("key") or ""): row for row in (spec.get("entity_contracts") or []) if isinstance(row, dict)}
        self.assertEqual(set(contracts), {"polls", "votes"})
        poll_fields = {str(field.get("name") or "") for field in contracts["polls"].get("fields", []) if isinstance(field, dict)}
        self.assertIn("title", poll_fields)
        self.assertNotIn("broken_field", poll_fields)
        self.assertIn("notes", set(spec.get("entities") or []))

    def test_route_c_uses_heuristic_semantic_when_llm_disabled(self):
        prompt = "Create a personal notes and documents tracker for myself."
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "0"}, clear=False):
            with mock.patch(
                "core.appspec.semantic_extractor._extract_via_codex",
                side_effect=AssertionError("LLM extractor should not run when disabled"),
            ):
                with mock.patch(
                    "core.appspec.semantic_extractor._heuristic_semantic_extract",
                    return_value={
                        "entities": ["Notes", "documents", "notes"],
                        "entity_contracts": [],
                        "requested_visuals": [],
                    },
                ) as heuristic:
                    spec = _build_app_spec(workspace_id=uuid.uuid4(), title="KB", raw_prompt=prompt)
        heuristic.assert_called_once()
        self.assertIn("notes", set(spec.get("entities") or []))
        self.assertIn("documents", set(spec.get("entities") or []))

    def test_invalid_semantic_payload_falls_back_to_constrained_output(self):
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "1"}, clear=False):
            with mock.patch(
                "core.appspec.semantic_extractor._extract_via_codex",
                return_value={"entities": "not-a-list", "entity_contracts": "bad", "requested_visuals": 12},
            ):
                result = semantic_extractor.extract_semantic_inference(
                    "Track customer tickets with notes",
                    prefer_llm=True,
                )
        self.assertTrue(isinstance(result.get("entities"), list))
        self.assertTrue(isinstance(result.get("entity_contracts"), list))
        self.assertTrue(isinstance(result.get("requested_visuals"), list))
        self.assertIn("customers", set(result.get("entities") or []))

    def test_semantic_entities_do_not_mutate_deterministic_contract_rows(self):
        prompt = (
            "Core entities: "
            "1. Campaign - name - status "
            "2. Signal - campaign - severity "
        )
        with mock.patch(
            "core.appspec.semantic_extractor.extract_semantic_inference",
            return_value={
                "entities": ["api"],
                "entity_contracts": [{"key": "campaigns", "fields": [{"name": "should_not_apply", "type": "string"}]}],
                "requested_visuals": [],
            },
        ):
            spec = _build_app_spec(workspace_id=uuid.uuid4(), title="Deal Finder", raw_prompt=prompt)
        contracts = {str(row.get("key") or ""): row for row in (spec.get("entity_contracts") or []) if isinstance(row, dict)}
        campaign_fields = {str(field.get("name") or "") for field in contracts["campaigns"].get("fields", []) if isinstance(field, dict)}
        self.assertNotIn("should_not_apply", campaign_fields)
        self.assertIn("api", set(spec.get("entities") or []))

    def test_deterministic_semantic_contract_conflict_warned(self):
        interpretation = appspec_canonicalize.canonicalize_interpretation(
            route="B",
            existing_entities=[],
            summary_entities=[],
            requested_entities=[],
            deterministic_entities=["campaigns"],
            semantic_entities=["campaigns"],
            deterministic_contracts=[{"key": "campaigns", "fields": [{"name": "name"}]}],
            semantic_contracts=[{"key": "campaigns", "fields": [{"name": "different"}]}],
            requested_visuals=[],
            deterministic_visuals=[],
            semantic_visuals=[],
            primitive_keys=[],
        )
        self.assertTrue(any("deterministic contracts are authoritative" in row.lower() for row in interpretation.warnings))

    def test_build_app_spec_shape_stable_after_canonicalization(self):
        prompt = (
            "Build a simple app called Team Lunch Poll. Requirements: Core entities: "
            "1. Poll - title - status (draft, open, closed) "
            "2. Vote - poll - voter_name "
            "Behavior: - Users can vote. "
            "Views / usability: - List polls. "
            "Validation / rules: - Only open polls accept votes."
        )
        spec = _build_app_spec(workspace_id=uuid.uuid4(), title="Team Lunch Poll", raw_prompt=prompt)
        self.assertEqual(spec.get("schema_version"), "xyn.appspec.v0")
        self.assertTrue(isinstance(spec.get("services"), list) and spec.get("services"))
        self.assertTrue(isinstance(spec.get("entities"), list) and spec.get("entities"))
        self.assertTrue(isinstance(spec.get("reports"), list))
        self.assertNotIn("inference_diagnostics", spec)


if __name__ == "__main__":
    unittest.main()
