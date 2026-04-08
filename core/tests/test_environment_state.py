from __future__ import annotations

import unittest
import uuid

from core.environment_state import (
    create_or_update_activation,
    ensure_default_environment,
    upsert_sibling_from_provision_output,
)
from core.models import Activation, Environment, Sibling


class _FakeQuery:
    def __init__(self, db: "_FakeDb", model):
        self._db = db
        self._model = model

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def first(self):
        rows = self._db._rows.get(self._model, [])
        return rows[0] if rows else None


class _FakeDb:
    def __init__(self):
        self._rows: dict[type, list[object]] = {}

    def query(self, model):
        return _FakeQuery(self, model)

    def add(self, obj):
        rows = self._rows.setdefault(type(obj), [])
        if obj not in rows:
            rows.append(obj)

    def flush(self):
        return None


class EnvironmentStateTests(unittest.TestCase):
    def test_ensure_default_environment(self):
        db = _FakeDb()
        workspace_id = uuid.uuid4()

        created = ensure_default_environment(db, workspace_id=workspace_id, workspace_slug="development")
        self.assertEqual(created.workspace_id, workspace_id)
        self.assertEqual(created.slug, "development")
        self.assertEqual(created.kind, "dev")
        self.assertEqual(created.status, "active")

        again = ensure_default_environment(db, workspace_id=workspace_id, workspace_slug="development")
        self.assertEqual(created.id, again.id)
        self.assertEqual(len(db._rows.get(Environment, [])), 1)

    def test_upsert_sibling_from_provision_output(self):
        db = _FakeDb()
        env_id = uuid.uuid4()
        workspace_id = uuid.uuid4()
        instance_id = str(uuid.uuid4())
        output = {
            "deployment_id": "dep-1",
            "compose_project": "xyn-sibling-a",
            "ui_url": "http://sib.localhost",
            "api_url": "http://api.sib.localhost",
            "runtime_target": {
                "runtime_base_url": "http://runtime.sibling:8080",
                "public_app_url": "http://public.sibling",
            },
            "runtime_registration": {"instance": {"id": instance_id}},
            "installed_artifact": {
                "artifact_slug": "app.net-inventory",
                "artifact_revision_id": "rev-1",
                "artifact_version": "0.0.1-dev",
            },
        }

        created = upsert_sibling_from_provision_output(
            db,
            environment_id=env_id,
            workspace_id=workspace_id,
            sibling_name="sibling-a",
            provision_output=output,
            status="ready",
        )
        self.assertEqual(created.environment_id, env_id)
        self.assertEqual(created.workspace_id, workspace_id)
        self.assertEqual(created.compose_project, "xyn-sibling-a")
        self.assertEqual(created.workspace_app_instance_id, instance_id)
        self.assertEqual(created.installed_artifact_slug, "app.net-inventory")
        self.assertEqual(created.runtime_base_url, "http://runtime.sibling:8080")

        updated_output = dict(output)
        updated_output["ui_url"] = "http://sib2.localhost"
        updated = upsert_sibling_from_provision_output(
            db,
            environment_id=env_id,
            workspace_id=workspace_id,
            sibling_name="sibling-a",
            provision_output=updated_output,
            status="active",
        )
        self.assertEqual(updated.id, created.id)
        self.assertEqual(updated.status, "active")
        self.assertEqual(updated.ui_url, "http://sib2.localhost")
        self.assertEqual(len(db._rows.get(Sibling, [])), 1)

    def test_activation_happy_path_transitions(self):
        db = _FakeDb()
        env_id = uuid.uuid4()
        workspace_id = uuid.uuid4()
        sibling_id = uuid.uuid4()

        activation = create_or_update_activation(
            db,
            environment_id=env_id,
            workspace_id=workspace_id,
            artifact_slug="app.net-inventory",
            status="provisioning",
            artifact_revision_id="rev-1",
            artifact_version="0.0.1-dev",
        )
        self.assertEqual(activation.status, "provisioning")

        activation = create_or_update_activation(
            db,
            environment_id=env_id,
            workspace_id=workspace_id,
            activation_id=activation.id,
            sibling_id=sibling_id,
            artifact_slug="app.net-inventory",
            artifact_revision_id="rev-1",
            artifact_version="0.0.1-dev",
            workspace_app_instance_id="instance-1",
            status="runtime_registered",
        )
        self.assertEqual(activation.status, "runtime_registered")
        self.assertEqual(activation.sibling_id, sibling_id)
        self.assertEqual(activation.workspace_app_instance_id, "instance-1")

        activation = create_or_update_activation(
            db,
            environment_id=env_id,
            workspace_id=workspace_id,
            activation_id=activation.id,
            artifact_slug="app.net-inventory",
            status="smoke_passed",
        )
        self.assertEqual(activation.status, "smoke_passed")
        self.assertIsNotNone(activation.activated_at)
        self.assertEqual(len(db._rows.get(Activation, [])), 1)


if __name__ == "__main__":
    unittest.main()
