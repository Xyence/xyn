from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from core.db_tenancy import DatabaseAllocation, allocate_database


class _FakeCursor:
    def __init__(self, executed: list[tuple[object, object]]):
        self.executed = executed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.executed.append((statement, params))


class _FakeConn:
    def __init__(self, executed: list[tuple[object, object]]):
        self.executed = executed
        self.autocommit = False
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.executed)

    def close(self):
        self.closed = True


class DbTenancyTests(unittest.TestCase):
    def test_allocate_database_local_mode_is_noop(self):
        with mock.patch.dict(os.environ, {"XYN_DB_MODE": "local"}, clear=False):
            allocation = allocate_database(
                environment_id=uuid.uuid4(),
                sibling_id=uuid.uuid4(),
                workspace_id=uuid.uuid4(),
                sibling_name="dev-shell",
            )
        self.assertIsInstance(allocation, DatabaseAllocation)
        self.assertEqual(allocation.mode, "local")
        self.assertEqual(allocation.database_url, "")
        self.assertEqual(allocation.runtime_env(), {})

    @mock.patch("core.db_tenancy.psycopg2.connect")
    def test_allocate_database_external_shared_rds_creates_scoped_database_and_user(self, connect_mock: mock.Mock):
        executed: list[tuple[object, object]] = []
        conn = _FakeConn(executed)
        connect_mock.return_value = conn
        env_id = uuid.uuid4()
        sibling_id = uuid.uuid4()
        workspace_id = uuid.uuid4()
        with mock.patch.dict(
            os.environ,
            {
                "XYN_DB_MODE": "external",
                "XYN_DB_TENANCY_MODE": "shared_rds_db_per_sibling",
                "XYN_DB_BOOTSTRAP_DATABASE_URL": "postgresql://admin:supersecret@db.example.internal:5432/postgres?sslmode=require",
            },
            clear=False,
        ):
            allocation = allocate_database(
                environment_id=env_id,
                sibling_id=sibling_id,
                workspace_id=workspace_id,
                sibling_name="sibling-a",
            )
        connect_mock.assert_called_once()
        self.assertEqual(allocation.mode, "external")
        self.assertEqual(allocation.tenancy_mode, "shared_rds_db_per_sibling")
        self.assertIn("db.example.internal", allocation.database_url)
        self.assertIn("sslmode=require", allocation.database_url)
        self.assertNotIn("admin:supersecret", allocation.database_url)
        self.assertTrue(allocation.database_name.startswith("xyn_e_"))
        self.assertTrue(allocation.database_user.startswith("xyn_u_"))
        self.assertGreaterEqual(len(executed), 4)
        public = allocation.to_public_dict()
        self.assertNotIn("database_url", public)
        self.assertNotIn("password", str(public).lower())

    def test_allocate_database_external_requires_bootstrap_url(self):
        with mock.patch.dict(
            os.environ,
            {"XYN_DB_MODE": "external", "XYN_DB_TENANCY_MODE": "shared_rds_db_per_sibling", "XYN_DB_BOOTSTRAP_DATABASE_URL": ""},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                allocate_database(
                    environment_id=uuid.uuid4(),
                    sibling_id=uuid.uuid4(),
                    workspace_id=uuid.uuid4(),
                    sibling_name="sibling-a",
                )

    @mock.patch("core.db_tenancy.psycopg2.connect", side_effect=RuntimeError("cannot connect"))
    def test_allocate_database_external_connection_error_is_sanitized(self, _connect_mock: mock.Mock):
        with mock.patch.dict(
            os.environ,
            {
                "XYN_DB_MODE": "external",
                "XYN_DB_TENANCY_MODE": "shared_rds_db_per_sibling",
                "XYN_DB_BOOTSTRAP_DATABASE_URL": "postgresql://admin:topsecret@db.example.internal:5432/postgres",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as exc_info:
                allocate_database(
                    environment_id=uuid.uuid4(),
                    sibling_id=uuid.uuid4(),
                    workspace_id=uuid.uuid4(),
                    sibling_name="sibling-a",
                )
        text = str(exc_info.exception)
        self.assertIn("Failed to connect bootstrap database", text)
        self.assertNotIn("topsecret", text)


if __name__ == "__main__":
    unittest.main()
