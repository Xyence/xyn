from __future__ import annotations

import os
import unittest
from unittest import mock

from sqlalchemy.exc import OperationalError

from core.tests import db_requirements


class _Row:
    def __init__(self, value):
        self._value = value

    def first(self):
        return (self._value,)


class _Session:
    def __init__(self, *, available: bool, tables: dict[str, bool] | None = None):
        self.available = available
        self.tables = tables or {}
        self.closed = False

    def execute(self, stmt, params=None):
        if not self.available:
            raise OperationalError("SELECT 1", {}, Exception("connect failed"))
        sql = str(stmt)
        if "to_regclass" in sql:
            name = str((params or {}).get("name") or "")
            return _Row(name if self.tables.get(name, False) else None)
        return _Row(1)

    def close(self):
        self.closed = True


class DBRequirementsTests(unittest.TestCase):
    def test_policy_auto_optional_locally(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            with mock.patch.dict(os.environ, {"CI": "", "XYN_DB_TEST_POLICY": "", "XYN_REQUIRE_DB_TESTS": ""}, clear=False):
                self.assertEqual(db_requirements.db_test_policy(), "optional")

    def test_policy_auto_required_in_ci(self):
        with mock.patch.dict(os.environ, {"CI": "true", "XYN_DB_TEST_POLICY": "", "XYN_REQUIRE_DB_TESTS": ""}, clear=False):
            self.assertEqual(db_requirements.db_test_policy(), "required")

    def test_optional_policy_skips_when_db_unavailable(self):
        with mock.patch.dict(os.environ, {"XYN_DB_TEST_POLICY": "optional", "CI": ""}, clear=False):
            with self.assertRaises(unittest.SkipTest):
                db_requirements.require_db_or_skip(
                    self,
                    session_factory=lambda: _Session(available=False),
                    retries=1,
                    sleep_seconds=0.0,
                )

    def test_required_policy_fails_when_db_unavailable(self):
        with mock.patch.dict(os.environ, {"XYN_DB_TEST_POLICY": "required"}, clear=False):
            with self.assertRaises(AssertionError):
                db_requirements.require_db_or_skip(
                    self,
                    session_factory=lambda: _Session(available=False),
                    retries=1,
                    sleep_seconds=0.0,
                )

    def test_required_tables_checked(self):
        with mock.patch.dict(os.environ, {"XYN_DB_TEST_POLICY": "required"}, clear=False):
            db_requirements.require_db_or_skip(
                self,
                session_factory=lambda: _Session(available=True, tables={"workspaces": True, "artifacts": True}),
                required_tables=("workspaces", "artifacts"),
                retries=1,
                sleep_seconds=0.0,
            )


if __name__ == "__main__":
    unittest.main()
