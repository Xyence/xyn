import os
import tempfile
import unittest
import uuid
from pathlib import Path

from core.artifact_store import (
    LocalFSArtifactStore,
    S3ArtifactStore,
    create_runtime_artifact_store,
    resolve_runtime_artifact_storage_config,
    runtime_artifact_storage_status,
)


class _FakeS3Body:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _FakeS3Client:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket: str, Key: str):
        payload = self.objects.get((Bucket, Key))
        if payload is None:
            raise KeyError(Key)
        return {"Body": _FakeS3Body(payload)}

    def delete_object(self, *, Bucket: str, Key: str):
        self.objects.pop((Bucket, Key), None)


class ArtifactStoreTests(unittest.TestCase):
    def test_local_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFSArtifactStore(base_path=tmp)
            artifact_id = uuid.uuid4()
            payload = b"artifact-payload"
            storage_key, sha = store.store_bytes(artifact_id=artifact_id, content=payload, compute_sha256=True)
            self.assertTrue(storage_key.endswith(str(artifact_id)))
            self.assertIsNotNone(sha)
            self.assertEqual(store.retrieve_bytes(artifact_id=artifact_id), payload)
            local_path = store.get_local_path(artifact_id=artifact_id)
            self.assertIsNotNone(local_path)
            self.assertTrue(Path(str(local_path)).exists())

    def test_create_runtime_store_defaults_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_backup = dict(os.environ)
            try:
                os.environ["XYN_RUNTIME_ARTIFACT_PROVIDER"] = "local"
                os.environ["XYN_ARTIFACT_ROOT"] = tmp
                cfg = resolve_runtime_artifact_storage_config()
                store = create_runtime_artifact_store(config=cfg)
                self.assertEqual(store.provider, "local")
                status = runtime_artifact_storage_status(config=cfg)
                self.assertEqual(status["provider"], "local")
                self.assertFalse(status["remote_durability_active"])
            finally:
                os.environ.clear()
                os.environ.update(env_backup)

    def test_create_runtime_store_rejects_incomplete_s3_config(self):
        env_backup = dict(os.environ)
        try:
            os.environ["XYN_RUNTIME_ARTIFACT_PROVIDER"] = "s3"
            os.environ.pop("XYN_RUNTIME_ARTIFACT_S3_BUCKET", None)
            cfg = resolve_runtime_artifact_storage_config()
            with self.assertRaises(ValueError):
                create_runtime_artifact_store(config=cfg)
        finally:
            os.environ.clear()
            os.environ.update(env_backup)

    def test_s3_store_round_trip_with_fake_client(self):
        fake_client = _FakeS3Client()
        store = S3ArtifactStore(bucket="xyn-test", prefix="runtime", client=fake_client)
        artifact_id = uuid.uuid4()
        payload = b"hello-s3"
        storage_key, sha = store.store_bytes(artifact_id=artifact_id, content=payload, compute_sha256=True)
        self.assertTrue(storage_key.startswith("runtime/"))
        self.assertIsNotNone(sha)
        self.assertEqual(store.retrieve_bytes(artifact_id=artifact_id), payload)
        self.assertTrue(store.delete_artifact(artifact_id=artifact_id))
        self.assertIsNone(store.retrieve_bytes(artifact_id=artifact_id))


if __name__ == "__main__":
    unittest.main()
