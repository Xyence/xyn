from __future__ import annotations

import unittest
import uuid
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core.app_jobs import _build_app_spec, _build_app_spec_with_diagnostics, _build_generated_artifact_manifest, _handle_generate_app_spec
from core.database import SessionLocal
from core.models import Artifact, Workspace
from core.appspec import canonicalize as appspec_canonicalize
from core.appspec import consistency as appspec_consistency
from core.appspec import semantic_extractor
from core.tests.db_requirements import require_db_or_skip


class AppSpecHybridInferenceTests(unittest.TestCase):
    def test_handle_generate_app_spec_persists_and_exposes_internal_diagnostics_readback(self):
        cases = [
            {
                "name": "structured",
                "prompt": (
                    "Build a simple app called Team Lunch Poll. Requirements: Core entities: "
                    "1. Poll - title - status (draft, open, closed) "
                    "2. Vote - poll - voter_name "
                    "Behavior: - Users can vote. "
                    "Views / usability: - List polls. "
                    "Validation / rules: - Only open polls accept votes."
                ),
                "expected_route": "A",
                "expect_warning": False,
            },
            {
                "name": "free_form",
                "prompt": "Create a personal notes and documents tracker for myself.",
                "expected_route": "C",
                "expect_warning": False,
            },
            {
                "name": "consistency_warning",
                "prompt": "Build campaign operations tooling with status chart reporting.",
                "expected_route": "C",
                "expect_warning": True,
            },
        ]
        for case in cases:
            with self.subTest(case["name"]):
                workspace_id = uuid.uuid4()
                job = SimpleNamespace(
                    id=uuid.uuid4(),
                    workspace_id=workspace_id,
                    type="generate_app_spec",
                    input_json={
                        "title": f"Diagnostic {case['name']}",
                        "content_json": {"raw_prompt": case["prompt"]},
                    },
                )
                fake_note = SimpleNamespace(id=uuid.uuid4())
                fake_workspace = SimpleNamespace(slug="development")
                fake_db = mock.MagicMock()
                fake_db.query.return_value.filter.return_value.first.return_value = fake_workspace

                persisted_rows: list[dict[str, object]] = []
                execution_note_updates: list[dict[str, object]] = []

                def _persist_capture(db, workspace_id, name, kind, payload, metadata):
                    persisted_rows.append(
                        {
                            "workspace_id": workspace_id,
                            "name": name,
                            "kind": kind,
                            "payload": payload,
                            "metadata": metadata if isinstance(metadata, dict) else {},
                        }
                    )
                    return f"{kind}-artifact-{len(persisted_rows)}"

                def _update_capture(db, artifact_id, **kwargs):
                    execution_note_updates.append(kwargs)
                    return fake_note

                with mock.patch("core.app_jobs.create_execution_note", return_value=fake_note):
                    with mock.patch("core.app_jobs._persist_json_artifact", side_effect=_persist_capture):
                        with mock.patch(
                            "core.app_jobs._package_generated_app",
                            return_value={
                                "artifact_slug": f"app.{case['name']}",
                                "artifact_version": "0.0.1-dev",
                                "artifact_package_path": "/tmp/pkg.zip",
                            },
                        ):
                            with mock.patch("core.app_jobs._import_generated_artifact_package", return_value={}):
                                with mock.patch("core.app_jobs.update_execution_note", side_effect=_update_capture):
                                    if case["expect_warning"]:
                                        with mock.patch(
                                            "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
                                            return_value=(
                                                {
                                                    "entities": ["campaigns"],
                                                    "entity_contracts": [],
                                                    "requested_visuals": ["interfaces_by_status_chart"],
                                                },
                                                {"llm_used": False, "fallback_used": False, "repair_used": False},
                                            ),
                                        ):
                                            output, _ = _handle_generate_app_spec(fake_db, job, [])
                                    elif case["expected_route"] in {"B", "C"}:
                                        with mock.patch(
                                            "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
                                            return_value=(
                                                {
                                                    "entities": ["notes", "documents"],
                                                    "entity_contracts": [],
                                                    "requested_visuals": [],
                                                },
                                                {"llm_used": True, "fallback_used": False, "repair_used": False},
                                            ),
                                        ):
                                            output, _ = _handle_generate_app_spec(fake_db, job, [])
                                    else:
                                        output, _ = _handle_generate_app_spec(fake_db, job, [])

                self.assertTrue(persisted_rows)
                app_spec_row = next(row for row in persisted_rows if str(row.get("kind")) == "app_spec")
                policy_row = next(row for row in persisted_rows if str(row.get("kind")) == "policy_bundle")
                app_spec_payload = app_spec_row.get("payload") if isinstance(app_spec_row.get("payload"), dict) else {}
                app_spec_metadata = app_spec_row.get("metadata") if isinstance(app_spec_row.get("metadata"), dict) else {}
                policy_metadata = policy_row.get("metadata") if isinstance(policy_row.get("metadata"), dict) else {}

                diagnostics = output.get("inference_diagnostics") if isinstance(output.get("inference_diagnostics"), dict) else {}
                self.assertTrue(diagnostics)
                self.assertEqual(diagnostics.get("route"), case["expected_route"])
                self.assertIn("structure_score", diagnostics)
                self.assertIn("llm_used", diagnostics)
                self.assertIn("fallback_or_repair_used", diagnostics)
                self.assertIn("appspec_semantic_capability_state", diagnostics)
                self.assertIn("semantic_limited_mode", diagnostics)
                self.assertIn("semantic_limited_mode_reason", diagnostics)
                self.assertTrue(isinstance(diagnostics.get("consistency_warnings"), list))
                self.assertTrue(isinstance(diagnostics.get("consistency_errors"), list))

                self.assertEqual(app_spec_metadata.get("inference_diagnostics"), diagnostics)
                self.assertNotIn("inference_diagnostics", app_spec_payload)
                self.assertNotIn("inference_diagnostics", output.get("app_spec") or {})

                self.assertTrue(execution_note_updates)
                note_metadata = execution_note_updates[-1].get("extra_metadata_updates")
                self.assertTrue(isinstance(note_metadata, dict))
                self.assertEqual(note_metadata.get("inference_diagnostics"), diagnostics)

                # Backward-compatibility read: metadata may legitimately omit diagnostics.
                self.assertNotIn("inference_diagnostics", policy_metadata)
                self.assertIsNone(policy_metadata.get("inference_diagnostics"))

                warnings = diagnostics.get("consistency_warnings") if isinstance(diagnostics.get("consistency_warnings"), list) else []
                has_non_limited_warnings = any("semantic planning agent was unavailable" not in str(row).lower() for row in warnings)
                self.assertEqual(has_non_limited_warnings, case["expect_warning"])

    def test_golden_corpus_routes_and_no_diagnostics_leak(self):
        corpus = [
            {
                "name": "structured_route_a",
                "prompt": (
                    "Build a simple app called Team Lunch Poll. Requirements: Core entities: "
                    "1. Poll - title - status (draft, open, closed) "
                    "2. Vote - poll - voter_name "
                    "Behavior: - Users can vote. "
                    "Views / usability: - List polls. "
                    "Validation / rules: - Only open polls accept votes."
                ),
                "route": "A",
            },
            {
                "name": "semi_structured_route_b",
                "prompt": "Core entities: 1. Campaign - name. Workflow: campaign workflow.",
                "route": "B",
            },
            {
                "name": "free_form_route_c",
                "prompt": "Build a useful internal customer ticket tracker app for my team.",
                "route": "C",
            },
            {
                "name": "non_network_free_form",
                "prompt": "Create a personal notes and documents tracker.",
                "route": "C",
            },
        ]
        for case in corpus:
            with self.subTest(case["name"]):
                if case["route"] in {"B", "C"}:
                    with mock.patch(
                        "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
                        return_value=(
                            {
                                "entities": ["tickets", "notes"],
                                "entity_contracts": [],
                                "requested_visuals": [],
                            },
                            {"llm_used": True, "fallback_used": False, "repair_used": False},
                        ),
                    ):
                        spec, diagnostics = _build_app_spec_with_diagnostics(
                            workspace_id=uuid.uuid4(),
                            title="Golden",
                            raw_prompt=case["prompt"],
                        )
                else:
                    spec, diagnostics = _build_app_spec_with_diagnostics(
                        workspace_id=uuid.uuid4(),
                        title="Golden",
                        raw_prompt=case["prompt"],
                    )
                self.assertEqual(diagnostics.get("route"), case["route"])
                self.assertIn("structure_score", diagnostics)
                self.assertIn("llm_used", diagnostics)
                self.assertIn("fallback_or_repair_used", diagnostics)
                self.assertIn("appspec_semantic_capability_state", diagnostics)
                self.assertIn("semantic_limited_mode", diagnostics)
                self.assertIn("semantic_limited_mode_reason", diagnostics)
                self.assertIn("consistency_warnings", diagnostics)
                self.assertIn("consistency_errors", diagnostics)
                self.assertNotIn("inference_diagnostics", spec)

    def test_route_a_reports_deterministic_only_capability_when_llm_unavailable(self):
        prompt = (
            "Build a simple app called Team Lunch Poll. Requirements: Core entities: "
            "1. Poll - title - status (draft, open, closed) "
            "2. Vote - poll - voter_name "
            "Behavior: - Users can vote."
        )
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "0"}, clear=False):
            spec, diagnostics = _build_app_spec_with_diagnostics(
                workspace_id=uuid.uuid4(),
                title="Team Lunch Poll",
                raw_prompt=prompt,
            )
        self.assertEqual(diagnostics.get("route"), "A")
        self.assertEqual(diagnostics.get("appspec_semantic_capability_state"), "deterministic_only")
        self.assertFalse(diagnostics.get("semantic_limited_mode"))
        self.assertEqual(diagnostics.get("semantic_limited_mode_reason"), "")
        self.assertNotIn("inference_diagnostics", spec)

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
            "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
            return_value=(
                {
                    "entities": ["campaigns"],
                    "entity_contracts": [],
                    "requested_visuals": ["devices_by_status_chart"],
                },
                {"llm_used": False, "fallback_used": False, "repair_used": False},
            ),
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
            "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
            return_value=(
                {
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
                {"llm_used": False, "fallback_used": False, "repair_used": False},
            ),
        ):
            spec = _build_app_spec(workspace_id=uuid.uuid4(), title="Voting Tool", raw_prompt=prompt)
        contracts = {str(row.get("key") or ""): row for row in (spec.get("entity_contracts") or []) if isinstance(row, dict)}
        self.assertEqual(set(contracts), {"polls", "votes"})
        poll_fields = {str(field.get("name") or "") for field in contracts["polls"].get("fields", []) if isinstance(field, dict)}
        self.assertIn("title", poll_fields)
        self.assertNotIn("broken_field", poll_fields)
        self.assertIn("notes", set(spec.get("entities") or []))

    def test_route_c_blocks_when_semantic_agent_disabled(self):
        prompt = "Create a personal notes and documents tracker for myself."
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "0"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "Semantic planning blocked"):
                _build_app_spec(workspace_id=uuid.uuid4(), title="KB", raw_prompt=prompt)

    def test_route_c_blocks_when_codex_unavailable(self):
        prompt = "Create a personal notes and documents tracker for myself."
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "1"}, clear=False):
            with mock.patch("core.appspec.semantic_extractor._semantic_codex_available", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "Semantic planning blocked"):
                    _build_app_spec_with_diagnostics(
                        workspace_id=uuid.uuid4(),
                        title="KB",
                        raw_prompt=prompt,
                    )

    def test_route_c_semantic_path_available_uses_hybrid_llm_available_state(self):
        prompt = "Create a personal notes and documents tracker for myself."
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "1"}, clear=False):
            with mock.patch("core.appspec.semantic_extractor._semantic_codex_available", return_value=True):
                with mock.patch(
                    "core.appspec.semantic_extractor._invoke_semantic_planning_agent",
                    return_value={
                        "entities": ["notes", "documents"],
                        "entity_contracts": [],
                        "requested_visuals": [],
                    },
                ):
                    spec, diagnostics = _build_app_spec_with_diagnostics(
                        workspace_id=uuid.uuid4(),
                        title="KB",
                        raw_prompt=prompt,
                    )
        self.assertEqual(diagnostics.get("route"), "C")
        self.assertEqual(diagnostics.get("appspec_semantic_capability_state"), "hybrid_llm_available")
        self.assertFalse(diagnostics.get("semantic_limited_mode"))
        self.assertEqual(diagnostics.get("semantic_limited_mode_reason"), "")
        self.assertTrue(diagnostics.get("llm_used"))
        self.assertIn("notes", set(spec.get("entities") or []))

    def test_force_llm_errors_when_semantic_capability_unavailable(self):
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "0"}, clear=False):
            with self.assertRaises(RuntimeError):
                semantic_extractor.extract_semantic_inference(
                    "Create notes tracker",
                    force_llm=True,
                )

    def test_invalid_semantic_payload_raises_validation_error(self):
        with mock.patch.dict("os.environ", {"XYN_APPSPEC_ENABLE_LLM_FALLBACK": "1"}, clear=False):
            with mock.patch(
                "core.appspec.semantic_extractor._invoke_semantic_planning_agent",
                return_value={"entities": "not-a-list", "entity_contracts": "bad", "requested_visuals": 12},
            ):
                with self.assertRaises(semantic_extractor.SemanticPlanningResponseValidationError):
                    semantic_extractor.extract_semantic_inference(
                        "Track customer tickets with notes",
                        prefer_llm=True,
                    )

    def test_semantic_entities_do_not_mutate_deterministic_contract_rows(self):
        prompt = (
            "Core entities: "
            "1. Campaign - name - status "
            "2. Signal - campaign - severity "
        )
        with mock.patch(
            "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
            return_value=(
                {
                    "entities": ["api"],
                    "entity_contracts": [{"key": "campaigns", "fields": [{"name": "should_not_apply", "type": "string"}]}],
                    "requested_visuals": [],
                },
                {"llm_used": False, "fallback_used": False, "repair_used": False},
            ),
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

    def test_golden_conflict_and_visual_warning_cases(self):
        prompt = "Core entities: 1. Campaign - name."
        with mock.patch(
            "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
            return_value=(
                {
                    "entities": ["campaigns"],
                    "entity_contracts": [{"key": "campaigns", "fields": [{"name": "different"}]}],
                    "requested_visuals": ["interfaces_by_status_chart"],
                },
                {"llm_used": False, "fallback_used": False, "repair_used": False},
            ),
        ):
            spec, diagnostics = _build_app_spec_with_diagnostics(
                workspace_id=uuid.uuid4(),
                title="Conflict",
                raw_prompt=prompt,
            )
        self.assertTrue(isinstance(spec.get("entity_contracts"), list) and spec.get("entity_contracts"))
        warnings = diagnostics.get("consistency_warnings") if isinstance(diagnostics.get("consistency_warnings"), list) else []
        self.assertTrue(any("interfaces_by_status_chart" in str(row) for row in warnings))

    def test_contract_structure_validation_warnings_and_errors_surface_in_diagnostics(self):
        prompt = "Build a customer support tracker for my team."
        with mock.patch(
            "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
            return_value=(
                {
                    "entities": ["tickets"],
                    "entity_contracts": [
                        {"fields": [{"name": "broken", "type": "string"}]},
                        {
                            "key": "tickets",
                            "fields": [
                                {"name": "title", "type": "string"},
                                {"name": "title", "type": "enum"},
                            ],
                            "validation": "invalid-shape",
                        },
                    ],
                    "requested_visuals": [],
                },
                {"llm_used": False, "fallback_used": False, "repair_used": False},
            ),
        ):
            spec, diagnostics = _build_app_spec_with_diagnostics(
                workspace_id=uuid.uuid4(),
                title="Support Tracker",
                raw_prompt=prompt,
            )
        self.assertIn("tickets", set(spec.get("entities") or []))
        contracts = {
            str(row.get("key") or ""): row
            for row in (spec.get("entity_contracts") or [])
            if isinstance(row, dict) and str(row.get("key") or "").strip()
        }
        self.assertIn("tickets", contracts)
        self.assertEqual(
            contracts["tickets"].get("fields"),
            [{"name": "title", "type": "string"}],
        )
        warnings = diagnostics.get("consistency_warnings") if isinstance(diagnostics.get("consistency_warnings"), list) else []
        errors = diagnostics.get("consistency_errors") if isinstance(diagnostics.get("consistency_errors"), list) else []
        self.assertTrue(any("contradictory duplicate field" in str(row).lower() for row in warnings))
        self.assertTrue(any("non-object 'validation'" in str(row).lower() for row in warnings))
        self.assertEqual(errors, [])

    def test_handle_generate_app_spec_emits_internal_diagnostics_metadata(self):
        workspace_id = uuid.uuid4()
        job = SimpleNamespace(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            type="generate_app_spec",
            input_json={
                "title": "Diagnostic App",
                "content_json": {"raw_prompt": "Core entities: 1. Campaign - name"},
            },
        )
        fake_note = SimpleNamespace(id=uuid.uuid4())
        fake_workspace = SimpleNamespace(slug="development")
        app_spec = {
            "schema_version": "xyn.appspec.v0",
            "app_slug": "diagnostic-app",
            "title": "Diagnostic App",
            "workspace_id": str(workspace_id),
            "services": [],
            "data": {"postgres": {}},
            "reports": [],
            "entities": ["campaigns"],
            "phase_1_scope": ["campaigns"],
            "requested_visuals": [],
        }
        policy_bundle = {"schema_version": "xyn.policy_bundle.v0", "bundle_id": "policy.diagnostic-app"}
        diagnostics = {
            "structure_score": 0.5,
            "route": "B",
            "llm_used": False,
            "consistency_warnings": [],
            "consistency_errors": [],
            "fallback_or_repair_used": False,
        }
        fake_db = mock.MagicMock()
        fake_db.query.return_value.filter.return_value.first.return_value = fake_workspace
        with mock.patch("core.app_jobs.create_execution_note", return_value=fake_note):
            with mock.patch("core.app_jobs._build_app_spec_with_diagnostics", return_value=(app_spec, diagnostics)):
                with mock.patch("core.app_jobs.validate", return_value=None):
                    with mock.patch("core.app_jobs._build_policy_bundle", return_value=policy_bundle):
                        with mock.patch("core.app_jobs._persist_json_artifact", side_effect=["appspec-art", "policy-art"]) as persist:
                            with mock.patch(
                                "core.app_jobs._package_generated_app",
                                return_value={"artifact_slug": "app.diagnostic-app", "artifact_version": "0.0.1-dev", "artifact_package_path": "/tmp/pkg.zip"},
                            ):
                                with mock.patch("core.app_jobs._import_generated_artifact_package", return_value={}):
                                    with mock.patch("core.app_jobs.update_execution_note", return_value=fake_note):
                                        output, _ = _handle_generate_app_spec(fake_db, job, [])
        self.assertEqual(output.get("inference_diagnostics"), diagnostics)
        self.assertNotIn("inference_diagnostics", output.get("app_spec") or {})
        first_call = persist.call_args_list[0]
        metadata = first_call.kwargs.get("metadata") if isinstance(first_call.kwargs.get("metadata"), dict) else {}
        self.assertEqual(metadata.get("inference_diagnostics"), diagnostics)

    def test_generated_artifact_manifest_backward_compatible_without_diagnostics(self):
        manifest = _build_generated_artifact_manifest(
            app_spec={
                "schema_version": "xyn.appspec.v0",
                "app_slug": "compat-check",
                "title": "Compat Check",
                "workspace_id": str(uuid.uuid4()),
                "entities": ["campaigns"],
                "reports": [],
                "requested_visuals": [],
            },
            runtime_config={},
        )
        self.assertEqual(str(((manifest.get("artifact") or {}).get("slug"))), "app.compat-check")


if __name__ == "__main__":
    unittest.main()


class AppSpecHybridInferencePersistenceIntegrationTests(unittest.TestCase):
    def test_db_backed_diagnostics_persistence_and_readback(self):
        require_db_or_skip(
            self,
            session_factory=SessionLocal,
            required_tables=("workspaces", "artifacts"),
        )

        cases = [
            {
                "name": "free_form_semantic",
                "prompt": "Create an internal customer ticket tracker with notes.",
                "expect_warning": False,
                "expect_limited_mode_warning": True,
            },
            {
                "name": "consistency_warning",
                "prompt": "Build campaign operations tooling with interfaces by status charts.",
                "expect_warning": False,
                "expect_limited_mode_warning": True,
            },
        ]

        for case in cases:
            with self.subTest(case["name"]):
                db = SessionLocal()
                created_artifact_ids: list[uuid.UUID] = []
                workspace = Workspace(
                    slug=f"diag-int-{uuid.uuid4().hex[:8]}",
                    title="Diagnostics Integration",
                )
                db.add(workspace)
                db.commit()
                db.refresh(workspace)
                try:
                    job = SimpleNamespace(
                        id=uuid.uuid4(),
                        workspace_id=workspace.id,
                        type="generate_app_spec",
                        input_json={
                            "title": f"DB Integration {case['name']}",
                            "content_json": {"raw_prompt": case["prompt"]},
                        },
                    )
                    with mock.patch(
                        "core.app_jobs._package_generated_app",
                        return_value={
                            "artifact_slug": f"app.{case['name']}",
                            "artifact_version": "0.0.1-dev",
                            "artifact_package_path": "/tmp/pkg.zip",
                        },
                    ):
                        with mock.patch("core.app_jobs._import_generated_artifact_package", return_value={}):
                            with mock.patch(
                                "core.appspec.semantic_extractor.extract_semantic_inference_with_diagnostics",
                                return_value=(
                                    {
                                        "entities": ["tickets", "notes"],
                                        "entity_contracts": [],
                                        "requested_visuals": ["interfaces_by_status_chart"]
                                        if case["name"] == "consistency_warning"
                                        else [],
                                    },
                                    {"llm_used": True, "fallback_used": False, "repair_used": False},
                                ),
                            ):
                                output, _ = _handle_generate_app_spec(db, job, [])
                    db.commit()

                    app_spec_artifact_id = uuid.UUID(str(output["app_spec_artifact_id"]))
                    policy_bundle_artifact_id = uuid.UUID(str(output["policy_bundle_artifact_id"]))
                    execution_note_artifact_id = uuid.UUID(str(output["execution_note_artifact_id"]))
                    created_artifact_ids.extend(
                        [app_spec_artifact_id, policy_bundle_artifact_id, execution_note_artifact_id]
                    )

                    read_db = SessionLocal()
                    try:
                        app_spec_row = read_db.query(Artifact).filter(Artifact.id == app_spec_artifact_id).first()
                        policy_row = read_db.query(Artifact).filter(Artifact.id == policy_bundle_artifact_id).first()
                        note_row = read_db.query(Artifact).filter(Artifact.id == execution_note_artifact_id).first()

                        self.assertIsNotNone(app_spec_row)
                        self.assertIsNotNone(policy_row)
                        self.assertIsNotNone(note_row)

                        diagnostics = output.get("inference_diagnostics")
                        self.assertTrue(isinstance(diagnostics, dict) and diagnostics)
                        self.assertNotIn("inference_diagnostics", output.get("app_spec") or {})
                        self.assertEqual((app_spec_row.metadata_json or {}).get("inference_diagnostics"), diagnostics)
                        self.assertEqual((note_row.metadata_json or {}).get("inference_diagnostics"), diagnostics)

                        warnings = (
                            diagnostics.get("consistency_warnings")
                            if isinstance(diagnostics.get("consistency_warnings"), list)
                            else []
                        )
                        limited_mode_warnings = [
                            row for row in warnings if "semantic planning agent was unavailable" in str(row).lower()
                        ]
                        has_non_limited_warnings = any(
                            "semantic planning agent was unavailable" not in str(row).lower() for row in warnings
                        )
                        self.assertEqual(has_non_limited_warnings, case["expect_warning"])
                        self.assertEqual(bool(limited_mode_warnings), case["expect_limited_mode_warning"])

                        # Backward-compatible older-style read path: diagnostics may be absent.
                        self.assertTrue(isinstance(policy_row.metadata_json, dict))
                        self.assertIsNone((policy_row.metadata_json or {}).get("inference_diagnostics"))
                    finally:
                        read_db.close()
                finally:
                    if created_artifact_ids:
                        db.query(Artifact).filter(Artifact.id.in_(created_artifact_ids)).delete(
                            synchronize_session=False
                        )
                    db.query(Workspace).filter(Workspace.id == workspace.id).delete(
                        synchronize_session=False
                    )
                    db.commit()
                    db.close()

    def test_execution_note_payload_and_metadata_share_inference_diagnostics(self):
        require_db_or_skip(
            self,
            session_factory=SessionLocal,
            required_tables=("workspaces", "artifacts"),
        )

        db = SessionLocal()
        created_artifact_ids: list[uuid.UUID] = []
        workspace = Workspace(
            slug=f"diag-note-{uuid.uuid4().hex[:8]}",
            title="Diagnostics Note Integration",
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        try:
            prompt = "Create a personal notes and documents tracker for myself."
            job = SimpleNamespace(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                type="generate_app_spec",
                input_json={
                    "title": "DB Note Consistency",
                    "content_json": {"raw_prompt": prompt},
                },
            )
            with mock.patch(
                "core.app_jobs._package_generated_app",
                return_value={
                    "artifact_slug": "app.diag-note",
                    "artifact_version": "0.0.1-dev",
                    "artifact_package_path": "/tmp/pkg.zip",
                },
            ):
                with mock.patch("core.app_jobs._import_generated_artifact_package", return_value={}):
                    output, _ = _handle_generate_app_spec(db, job, [])
            db.commit()

            app_spec_artifact_id = uuid.UUID(str(output["app_spec_artifact_id"]))
            policy_bundle_artifact_id = uuid.UUID(str(output["policy_bundle_artifact_id"]))
            execution_note_artifact_id = uuid.UUID(str(output["execution_note_artifact_id"]))
            created_artifact_ids.extend(
                [app_spec_artifact_id, policy_bundle_artifact_id, execution_note_artifact_id]
            )

            read_db = SessionLocal()
            try:
                note_row = read_db.query(Artifact).filter(Artifact.id == execution_note_artifact_id).first()
                app_spec_row = read_db.query(Artifact).filter(Artifact.id == app_spec_artifact_id).first()
                policy_row = read_db.query(Artifact).filter(Artifact.id == policy_bundle_artifact_id).first()
                self.assertIsNotNone(note_row)
                self.assertIsNotNone(app_spec_row)
                self.assertIsNotNone(policy_row)

                diagnostics = output.get("inference_diagnostics")
                self.assertTrue(isinstance(diagnostics, dict) and diagnostics)
                self.assertNotIn("inference_diagnostics", output.get("app_spec") or {})

                note_meta = dict(note_row.metadata_json or {})
                self.assertEqual(note_meta.get("inference_diagnostics"), diagnostics)

                note_path = Path(str(note_row.storage_path or ""))
                self.assertTrue(note_path.exists())
                note_payload = json.loads(note_path.read_text(encoding="utf-8"))
                payload_extra = note_payload.get("extra_metadata") if isinstance(note_payload.get("extra_metadata"), dict) else {}
                self.assertEqual(payload_extra.get("inference_diagnostics"), diagnostics)

                # Backward compatibility: policy artifact metadata need not carry diagnostics.
                self.assertIsNone((policy_row.metadata_json or {}).get("inference_diagnostics"))
                # AppSpec payload contract remains unchanged.
                app_spec_payload = json.loads(Path(str(app_spec_row.storage_path)).read_text(encoding="utf-8"))
                self.assertNotIn("inference_diagnostics", app_spec_payload)
            finally:
                read_db.close()
        finally:
            if created_artifact_ids:
                db.query(Artifact).filter(Artifact.id.in_(created_artifact_ids)).delete(
                    synchronize_session=False
                )
            db.query(Workspace).filter(Workspace.id == workspace.id).delete(
                synchronize_session=False
            )
            db.commit()
            db.close()
