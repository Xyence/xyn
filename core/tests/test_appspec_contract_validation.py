from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path

from jsonschema import ValidationError, validate

from core.appspec.contract_validation import validate_and_normalize_entity_contracts


class AppSpecEntityContractValidationTests(unittest.TestCase):
    @staticmethod
    def _appspec_schema() -> dict:
        path = Path(__file__).resolve().parents[1] / "contracts" / "appspec_v0.schema.json"
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _minimal_appspec(*, entity_contracts):
        return {
            "schema_version": "xyn.appspec.v0",
            "app_slug": "schema-check",
            "title": "Schema Check",
            "workspace_id": str(uuid.uuid4()),
            "services": [
                {
                    "name": "schema-check-api",
                    "image": "xyn-api:latest",
                    "env": {},
                    "ports": [{"container": 8080, "host": 0, "protocol": "tcp"}],
                    "depends_on": [],
                }
            ],
            "data": {"postgres": {}},
            "reports": [],
            "entity_contracts": entity_contracts,
        }

    def test_missing_contract_key_is_reported_and_dropped(self):
        result = validate_and_normalize_entity_contracts(
            [
                {"fields": [{"name": "name", "type": "string"}]},
                {"key": "polls", "fields": [{"name": "title", "type": "string"}]},
            ]
        )
        keys = {str(row.get("key") or "") for row in result.contracts if isinstance(row, dict)}
        self.assertEqual(keys, {"polls"})
        self.assertTrue(any("missing required key" in row.lower() for row in result.errors))

    def test_malformed_fields_are_normalized_safely(self):
        result = validate_and_normalize_entity_contracts(
            [
                {
                    "key": "polls",
                    "fields": [
                        {"name": "Title", "type": "string"},
                        {"name": "", "type": "string"},
                        {"name": "status"},
                        "bad",
                    ],
                }
            ]
        )
        self.assertEqual(result.errors, [])
        fields = result.contracts[0].get("fields") if isinstance(result.contracts[0].get("fields"), list) else []
        self.assertEqual(fields, [{"name": "title", "type": "string"}])
        self.assertGreaterEqual(len(result.warnings), 3)

    def test_duplicate_and_contradictory_entries_keep_first(self):
        result = validate_and_normalize_entity_contracts(
            [
                {
                    "key": "polls",
                    "fields": [
                        {"name": "status", "type": "string"},
                        {"name": "status", "type": "enum"},
                    ],
                },
                {"key": "polls", "fields": [{"name": "title", "type": "string"}]},
            ]
        )
        self.assertEqual(len(result.contracts), 1)
        fields = result.contracts[0].get("fields") if isinstance(result.contracts[0].get("fields"), list) else []
        self.assertEqual(fields, [{"name": "status", "type": "string"}])
        self.assertTrue(any("contradictory duplicate field 'status'" in row.lower() for row in result.warnings))
        self.assertTrue(any("duplicate contract 'polls'" in row.lower() for row in result.warnings))

    def test_backward_compatible_loose_payloads_still_normalize(self):
        result = validate_and_normalize_entity_contracts(
            [
                {"key": "notes", "fields": "not-a-list", "relationships": "not-a-list", "validation": "not-a-dict"}
            ]
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(result.contracts[0]["key"], "notes")
        self.assertEqual(result.contracts[0]["fields"], [])
        self.assertEqual(result.contracts[0]["relationships"], [])
        self.assertEqual(result.contracts[0]["validation"], {})
        self.assertTrue(result.warnings)

    def test_schema_accepts_existing_style_contracts(self):
        appspec = self._minimal_appspec(
            entity_contracts=[
                {
                    "key": "polls",
                    "fields": [{"name": "title", "type": "string"}],
                }
            ]
        )
        validate(instance=appspec, schema=self._appspec_schema())

    def test_schema_rejects_clearly_malformed_contract_shapes(self):
        appspec = self._minimal_appspec(
            entity_contracts=[
                {
                    "key": 123,
                    "fields": "invalid",
                    "relationships": "invalid",
                    "operations": [],
                    "validation": [],
                }
            ]
        )
        with self.assertRaises(ValidationError):
            validate(instance=appspec, schema=self._appspec_schema())

    def test_schema_allows_older_loose_payload_after_internal_normalization(self):
        raw_contracts = [
            {"key": "notes", "fields": "not-a-list", "relationships": "not-a-list", "validation": "not-a-dict"}
        ]
        normalized = validate_and_normalize_entity_contracts(raw_contracts).contracts
        appspec = self._minimal_appspec(entity_contracts=normalized)
        validate(instance=appspec, schema=self._appspec_schema())


if __name__ == "__main__":
    unittest.main()
