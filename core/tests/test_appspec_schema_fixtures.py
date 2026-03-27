from __future__ import annotations

import json
import unittest
from pathlib import Path

from jsonschema import ValidationError, validate

from core.appspec.contract_validation import validate_and_normalize_entity_contracts


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "appspec_schema"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "appspec_v0.schema.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class AppSpecSchemaFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = _load_json(SCHEMA_PATH)

    def test_valid_fixtures_pass_schema(self):
        fixture_names = [
            "valid_minimal.json",
            "valid_with_entity_contracts.json",
            "valid_hybrid_style.json",
            "valid_with_unknown_keys_tolerated.json",
        ]
        for name in fixture_names:
            with self.subTest(name=name):
                payload = _load_json(FIXTURES_DIR / name)
                validate(instance=payload, schema=self.schema)

    def test_invalid_fixtures_fail_schema(self):
        fixture_names = [
            "invalid_contract_shapes.json",
            "invalid_field_relationship_rows.json",
            "loose_compatible_contracts_raw.json",
        ]
        for name in fixture_names:
            with self.subTest(name=name):
                payload = _load_json(FIXTURES_DIR / name)
                with self.assertRaises(ValidationError):
                    validate(instance=payload, schema=self.schema)

    def test_loose_compatible_fixture_passes_after_internal_normalization(self):
        payload = _load_json(FIXTURES_DIR / "loose_compatible_contracts_raw.json")
        normalized_contracts = validate_and_normalize_entity_contracts(payload.get("entity_contracts")).contracts
        payload["entity_contracts"] = normalized_contracts
        validate(instance=payload, schema=self.schema)


if __name__ == "__main__":
    unittest.main()

