from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from core.artifact_provenance import (
    extract_provenance_metadata,
    merge_provenance_metadata,
    normalize_provenance_payload,
    validate_provenance_payload,
)
from core.schemas import Artifact


class ArtifactProvenanceTests(unittest.TestCase):
    def test_normalize_and_validate_canonical_provenance(self) -> None:
        payload = {
            "source": {
                "kind": "git",
                "repo_url": "https://github.com/xyn-platform",
                "repo_key": "xyn-platform",
                "commit_sha": "ABCDEF0123456789",
                "branch_hint": "develop",
                "monorepo_subpath": "services/xyn-api",
                "manifest_ref": "xyn-api/artifact.manifest.json",
            },
            "build": {
                "pipeline_provider": "github_actions",
                "run_id": "run-123",
                "image_ref": "public.ecr.aws/xyn/xyn-api:develop",
                "image_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                "built_from_commit_sha": "abcdef0123456789",
            },
        }
        normalized = normalize_provenance_payload(payload)
        self.assertEqual(normalized["source"]["kind"], "git")
        self.assertEqual(normalized["source"]["commit_sha"], "abcdef0123456789")
        self.assertEqual(normalized["build"]["pipeline_provider"], "github_actions")
        self.assertEqual(validate_provenance_payload(normalized), [])

    def test_merge_provenance_metadata_preserves_existing_fields(self) -> None:
        metadata = {
            "workspace_id": "w1",
            "source": {
                "kind": "git",
                "repo_key": "xyn-platform",
                "commit_sha": "abcdef0123456789",
            },
        }
        merged = merge_provenance_metadata(
            metadata,
            provenance={
                "build": {
                    "pipeline_provider": "github_actions",
                    "image_digest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                }
            },
        )
        provenance = merged.get("provenance") if isinstance(merged.get("provenance"), dict) else {}
        source = provenance.get("source") if isinstance(provenance.get("source"), dict) else {}
        build = provenance.get("build") if isinstance(provenance.get("build"), dict) else {}
        self.assertEqual(source.get("repo_key"), "xyn-platform")
        self.assertEqual(build.get("pipeline_provider"), "github_actions")
        self.assertEqual(merged.get("workspace_id"), "w1")

    def test_artifact_schema_exposes_normalized_provenance(self) -> None:
        row = SimpleNamespace(
            id=uuid.uuid4(),
            workspace_id=None,
            name="xyn-api",
            kind="bundle",
            artifact_type="bundle",
            uri="/tmp/fake.zip",
            label="xyn-api",
            content_type="application/zip",
            byte_length=42,
            created_at=datetime.now(timezone.utc),
            created_by="system",
            run_id=None,
            step_id=None,
            sha256=None,
            storage_scope="instance-local",
            sync_state="local",
            storage_path="/tmp/fake.zip",
            extra_metadata={
                "source": {
                    "kind": "git",
                    "repo_url": "https://github.com/xyn-platform",
                    "repo_key": "xyn-platform",
                    "commit_sha": "abcdef0123456789",
                    "monorepo_subpath": "services/xyn-api",
                }
            },
        )

        payload = Artifact.from_orm_model(row)
        self.assertEqual(payload.provenance.get("source", {}).get("repo_key"), "xyn-platform")
        self.assertEqual(payload.provenance.get("source", {}).get("monorepo_subpath"), "services/xyn-api")
        self.assertIn("provenance", payload.metadata)
        extracted = extract_provenance_metadata(payload.metadata)
        self.assertEqual(extracted.get("source", {}).get("repo_url"), "https://github.com/xyn-platform")

    def test_invalid_provenance_is_flagged(self) -> None:
        errors = validate_provenance_payload(
            {
                "source": {"kind": "git", "commit_sha": "not-a-sha"},
                "build": {"image_digest": "sha256:not-a-real-digest"},
            }
        )
        self.assertGreaterEqual(len(errors), 2)


if __name__ == "__main__":
    unittest.main()
