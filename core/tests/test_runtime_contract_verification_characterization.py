from __future__ import annotations

import unittest
from unittest import mock

from core.app_jobs import (
    _ensure_parent_status_gate_prerequisites,
    _exercise_runtime_contracts,
)


class RuntimeContractVerificationCharacterizationTests(unittest.TestCase):
    def test_exercise_runtime_contracts_success_path_sequence(self):
        workspace_id = "ws-1"
        contracts = [
            {
                "key": "tickets",
                "singular_label": "ticket",
                "collection_path": "/tickets",
                "item_path_template": "/tickets/{id}",
                "fields": [
                    {"name": "workspace_id", "writable": True, "required": True},
                    {"name": "name", "writable": True, "required": True, "type": "string"},
                    {"name": "status", "writable": True, "type": "string", "options": ["new", "open"]},
                ],
                "validation": {
                    "required_on_create": ["workspace_id", "name"],
                    "allowed_on_update": ["status"],
                },
                "relationships": [],
            }
        ]
        calls: list[tuple[str, str]] = []

        def _container_http_json_stub(_container, method, path, *, port, payload=None):
            calls.append((method, path))
            if method == "POST" and path == "/tickets":
                return 201, {"id": "t1", "workspace_id": workspace_id, "status": "new"}, "created"
            if method == "GET" and path == f"/tickets?workspace_id={workspace_id}":
                return 200, {"items": [{"id": "t1"}]}, "ok"
            if method == "GET" and path == f"/tickets/t1?workspace_id={workspace_id}":
                return 200, {"id": "t1", "workspace_id": workspace_id, "status": "new"}, "ok"
            if method == "PATCH" and path == f"/tickets/t1?workspace_id={workspace_id}":
                return 200, {"id": "t1", "workspace_id": workspace_id, "status": "open"}, "ok"
            return 500, {}, "unexpected"

        with mock.patch("core.app_jobs._container_http_json", side_effect=_container_http_json_stub):
            results = _exercise_runtime_contracts(
                container_name="runtime-api",
                port=8080,
                workspace_id=workspace_id,
                entity_contracts=contracts,
                policy_bundle={},
            )

        self.assertIn("tickets", results)
        self.assertEqual(results["tickets"]["create"]["code"], 201)
        self.assertEqual(results["tickets"]["list"]["code"], 200)
        self.assertEqual(results["tickets"]["get"]["code"], 200)
        self.assertEqual(results["tickets"]["update"]["code"], 200)
        self.assertEqual(
            calls,
            [
                ("POST", "/tickets"),
                ("GET", f"/tickets?workspace_id={workspace_id}"),
                ("GET", f"/tickets/t1?workspace_id={workspace_id}"),
                ("PATCH", f"/tickets/t1?workspace_id={workspace_id}"),
            ],
        )

    def test_parent_status_gate_transition_path_behavior(self):
        workspace_id = "ws-1"
        parent_contract = {
            "key": "polls",
            "item_path_template": "/polls/{id}",
        }
        child_contract = {
            "key": "votes",
        }
        created_records = {"polls": {"id": "p1", "status": "draft"}}
        policy_bundle = {
            "policies": {
                "validation_policies": [
                    {
                        "parameters": {
                            "runtime_rule": "parent_status_gate",
                            "entity_key": "votes",
                            "on_operations": ["create"],
                            "parent_entity": "polls",
                            "parent_status_field": "status",
                            "allowed_parent_statuses": ["selected"],
                        }
                    }
                ],
                "transition_policies": [
                    {
                        "parameters": {
                            "runtime_rule": "field_transition_guard",
                            "entity_key": "polls",
                            "field_name": "status",
                            "allowed_transitions": {"draft": ["open"], "open": ["selected"]},
                        }
                    }
                ],
            }
        }
        payloads: list[dict[str, str]] = []

        def _container_http_json_stub(_container, method, path, *, port, payload=None):
            if method == "PATCH" and path == f"/polls/p1?workspace_id={workspace_id}":
                payloads.append(dict(payload or {}))
                status = str((payload or {}).get("status") or "")
                return 200, {"id": "p1", "status": status}, "ok"
            return 500, {}, "unexpected"

        with mock.patch("core.app_jobs._container_http_json", side_effect=_container_http_json_stub):
            _ensure_parent_status_gate_prerequisites(
                container_name="runtime-api",
                port=8080,
                workspace_id=workspace_id,
                contract=child_contract,
                entity_contracts=[parent_contract, child_contract],
                created_records=created_records,
                policy_bundle=policy_bundle,
            )

        self.assertEqual(payloads, [{"status": "open"}, {"status": "selected"}])
        self.assertEqual(created_records["polls"]["status"], "selected")

    def test_exercise_runtime_contracts_fail_fast_post_error_message(self):
        workspace_id = "ws-1"
        contracts = [{"key": "tickets", "collection_path": "/tickets", "fields": [], "validation": {}, "relationships": []}]

        with mock.patch("core.app_jobs._container_http_json", return_value=(500, {}, "boom")):
            with self.assertRaisesRegex(RuntimeError, r"POST /tickets failed \(500\): boom"):
                _exercise_runtime_contracts(
                    container_name="runtime-api",
                    port=8080,
                    workspace_id=workspace_id,
                    entity_contracts=contracts,
                    policy_bundle={},
                )

    def test_exercise_runtime_contracts_unresolved_dependency_message(self):
        contracts = [
            {
                "key": "child",
                "collection_path": "/child",
                "fields": [],
                "validation": {},
                "relationships": [{"target_entity": "missing-parent"}],
            }
        ]

        with self.assertRaisesRegex(RuntimeError, r"Could not resolve seed order for generated entity contracts: \['child'\]"):
            _exercise_runtime_contracts(
                container_name="runtime-api",
                port=8080,
                workspace_id="ws-1",
                entity_contracts=contracts,
                policy_bundle={},
            )
