import asyncio
import io
import json
import os
import uuid
import unittest
from datetime import datetime
from pathlib import Path

from fastapi import UploadFile

from core import models
from core.api import artifacts as artifacts_api
from core.artifact_store import get_runtime_artifact_store
from core.database import SessionLocal
from core.log_capture import StepLogCapture
from core.runtime_contract import RunPayloadV1
from core.runtime_execution import capture_run_artifact, create_runtime_run, read_run_artifact_content, report_run_step


@unittest.skipUnless(
    os.getenv("XYN_RUNTIME_ARTIFACT_PROVIDER", "").strip().lower() == "s3"
    and os.getenv("XYN_RUNTIME_ARTIFACT_S3_BUCKET", "").strip() != "",
    "S3 runtime artifact integration is disabled (set XYN_RUNTIME_ARTIFACT_PROVIDER=s3 and XYN_RUNTIME_ARTIFACT_S3_BUCKET).",
)
class RuntimeS3MinioIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "run_payload_v1.json"
        cls.fixture = json.loads(fixture_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.db = SessionLocal()
        self.run_ids: list[uuid.UUID] = []
        self.step_ids: list[uuid.UUID] = []
        self.artifact_ids: list[uuid.UUID] = []
        self.store = get_runtime_artifact_store(reset=True)

    def tearDown(self):
        if self.step_ids:
            self.db.query(models.Event).filter(models.Event.step_id.in_(self.step_ids)).delete(synchronize_session=False)
        if self.run_ids:
            self.db.query(models.Event).filter(models.Event.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Artifact).filter(models.Artifact.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Step).filter(models.Step.run_id.in_(self.run_ids)).delete(synchronize_session=False)
            self.db.query(models.Run).filter(models.Run.id.in_(self.run_ids)).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _payload(self) -> RunPayloadV1:
        payload = dict(self.fixture)
        payload["run_id"] = str(uuid.uuid4())
        payload["work_item_id"] = f"wi-{uuid.uuid4()}"
        payload["target"] = dict(payload["target"])
        payload["context"] = dict(payload["context"])
        payload["policy"] = dict(payload["policy"])
        return RunPayloadV1.model_validate(payload)

    def _assert_object_exists(self, storage_key: str):
        if self.store.provider != "s3":
            self.fail(f"Expected s3 store, received provider={self.store.provider}")
        # Direct object lookup proves bytes were persisted in object storage.
        response = self.store._client.get_object(Bucket=self.store.bucket, Key=storage_key)  # type: ignore[attr-defined]
        body = response.get("Body")
        self.assertIsNotNone(body)
        self.assertGreaterEqual(len(body.read()), 0)

    def test_runtime_artifact_flows_round_trip_through_s3_backend(self):
        # 1) generic artifact API write/read
        upload = UploadFile(filename="integration.txt", file=io.BytesIO(b"s3-api-flow"))
        schema = asyncio.run(
            artifacts_api.create_artifact(
                name="integration-api-artifact",
                kind="file",
                content_type="text/plain",
                file=upload,
                db=self.db,
            )
        )
        artifact_id = uuid.UUID(str(schema.artifact_id))
        self.artifact_ids.append(artifact_id)
        row = self.db.query(models.Artifact).filter(models.Artifact.id == artifact_id).one()
        storage_key = str(row.storage_path or "")
        self.assertTrue(storage_key)
        self._assert_object_exists(storage_key)
        download = asyncio.run(artifacts_api.download_artifact(artifact_id=artifact_id, db=self.db))
        self.assertEqual(getattr(download, "body", b""), b"s3-api-flow")

        # 2) step log artifact capture
        run = create_runtime_run(self.db, self._payload())
        self.run_ids.append(run.id)
        run.status = models.RunStatus.RUNNING
        run.started_at = datetime.utcnow()
        self.db.flush()
        step = report_run_step(
            self.db,
            run_id=run.id,
            step_key="integration-step",
            label="Integration Step",
            status="running",
            sequence_no=1,
            summary="running",
        )
        self.step_ids.append(step.id)
        capture = StepLogCapture(step.id, run.id, self.db, self.store, run.correlation_id)
        capture.write("integration log line")
        log_artifact_id = asyncio.run(capture.finalize())
        self.assertIsNotNone(log_artifact_id)
        self.artifact_ids.append(log_artifact_id)
        log_row = self.db.query(models.Artifact).filter(models.Artifact.id == log_artifact_id).one()
        self._assert_object_exists(str(log_row.storage_path or ""))

        # 3) runtime execution artifact write/read
        runtime_artifact = capture_run_artifact(
            self.db,
            run_id=run.id,
            artifact_type="summary",
            label="runtime-summary",
            content={"status": "ok"},
        )
        self.artifact_ids.append(runtime_artifact.id)
        runtime_meta = dict(runtime_artifact.extra_metadata or {})
        runtime_storage_key = str(runtime_meta.get("storage_key") or "")
        self.assertTrue(runtime_storage_key)
        self._assert_object_exists(runtime_storage_key)
        runtime_content = read_run_artifact_content(self.db, run.id, runtime_artifact.id)
        self.assertIn("ok", str(runtime_content.get("content") or ""))

        self.db.commit()


if __name__ == "__main__":
    unittest.main()
