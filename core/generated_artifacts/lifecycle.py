from __future__ import annotations

"""Generated artifact lifecycle helpers (incremental DEBT-09 hardening).

Lifecycle identity model (additive/backward compatible):
- artifact_slug: stable logical identity (existing)
- revision_id: immutable unique id per generation (new)
- version_label: human-facing lifecycle label (new; defaults to ``dev``)
- lineage_id: stable revision family id per artifact_slug (new)

Lifecycle stages:
- GENERATED: newly generated, development-stage revision
- PROMOTED: revision marked stable/released via metadata promotion
- INSTALLED: revision observed as installed in a runtime/workspace binding

Compatibility:
- Legacy package/runtime version ``0.0.1-dev`` remains supported as the
  default/fallback transport version for existing import/install paths.
- New lifecycle fields are additive metadata and must not break existing flows
  when absent.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


LEGACY_GENERATED_VERSION = "0.0.1-dev"

STAGE_GENERATED = "GENERATED"
STAGE_PROMOTED = "PROMOTED"
STAGE_INSTALLED = "INSTALLED"


@dataclass(frozen=True)
class GeneratedArtifactIdentity:
    artifact_slug: str
    revision_id: str
    version_label: str
    lineage_id: str
    lifecycle_stage: str
    legacy_version: str = LEGACY_GENERATED_VERSION

    def to_metadata(self) -> dict[str, Any]:
        return {
            "artifact_slug": self.artifact_slug,
            "revision_id": self.revision_id,
            "version_label": self.version_label,
            "lineage_id": self.lineage_id,
            "lifecycle_stage": self.lifecycle_stage,
            "legacy_version": self.legacy_version,
        }


def generate_revision_id(*, now: datetime | None = None) -> str:
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M%S")
    return f"r-{ts}-{uuid.uuid4().hex[:10]}"


def build_lineage_id(*, artifact_slug: str) -> str:
    slug = str(artifact_slug or "").strip().lower() or "app.generated"
    return f"lineage:{slug}"


def generated_identity(*, artifact_slug: str, version_label: str = "dev") -> GeneratedArtifactIdentity:
    revision_id = generate_revision_id()
    return GeneratedArtifactIdentity(
        artifact_slug=str(artifact_slug or "").strip(),
        revision_id=revision_id,
        version_label=str(version_label or "dev").strip() or "dev",
        lineage_id=build_lineage_id(artifact_slug=str(artifact_slug or "").strip()),
        lifecycle_stage=STAGE_GENERATED,
    )
