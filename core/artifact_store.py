"""LocalFS Artifact Store for Xyn Seed v0.0"""
import os
import hashlib
import aiofiles
from pathlib import Path
from typing import BinaryIO, Optional
from uuid import UUID

DEFAULT_ARTIFACT_ROOT = ".xyn/artifacts"


class LocalFSArtifactStore:
    """Local filesystem-based artifact storage."""

    def __init__(self, base_path: str = DEFAULT_ARTIFACT_ROOT):
        """Initialize the artifact store.

        Args:
            base_path: Base directory for artifact storage
        """
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _get_artifact_path(self, artifact_id: UUID) -> Path:
        """Get the filesystem path for an artifact.

        Uses a two-level directory structure based on artifact ID:
        artifacts/ab/cd/abcd1234-5678-90ab-cdef-1234567890ab

        Args:
            artifact_id: The artifact UUID

        Returns:
            Path object for the artifact file
        """
        artifact_str = str(artifact_id)
        # Use first 2 and next 2 chars for directory structure
        level1 = artifact_str[:2]
        level2 = artifact_str[2:4]

        artifact_dir = self.base_path / level1 / level2
        artifact_dir.mkdir(parents=True, exist_ok=True)

        return artifact_dir / artifact_str

    async def store(
        self,
        artifact_id: UUID,
        content: bytes,
        compute_sha256: bool = True
    ) -> tuple[str, Optional[str]]:
        """Store artifact content to filesystem.

        Args:
            artifact_id: The artifact UUID
            content: The artifact content as bytes
            compute_sha256: Whether to compute SHA256 hash

        Returns:
            Tuple of (storage_path, sha256_hash)
        """
        artifact_path = self._get_artifact_path(artifact_id)

        # Compute SHA256 if requested
        sha256_hash = None
        if compute_sha256:
            sha256_hash = hashlib.sha256(content).hexdigest()

        # Write content to file
        async with aiofiles.open(artifact_path, 'wb') as f:
            await f.write(content)

        return str(artifact_path.relative_to(self.base_path)), sha256_hash

    async def store_stream(
        self,
        artifact_id: UUID,
        stream: BinaryIO,
        compute_sha256: bool = True
    ) -> tuple[str, Optional[str], int]:
        """Store artifact content from a stream.

        Args:
            artifact_id: The artifact UUID
            stream: Binary stream to read from
            compute_sha256: Whether to compute SHA256 hash

        Returns:
            Tuple of (storage_path, sha256_hash, byte_length)
        """
        artifact_path = self._get_artifact_path(artifact_id)

        sha256_hasher = hashlib.sha256() if compute_sha256 else None
        byte_length = 0

        # Stream content to file
        async with aiofiles.open(artifact_path, 'wb') as f:
            while True:
                chunk = stream.read(8192)  # 8KB chunks
                if not chunk:
                    break

                await f.write(chunk)
                byte_length += len(chunk)

                if sha256_hasher:
                    sha256_hasher.update(chunk)

        sha256_hash = sha256_hasher.hexdigest() if sha256_hasher else None
        return (
            str(artifact_path.relative_to(self.base_path)),
            sha256_hash,
            byte_length
        )

    async def retrieve(self, artifact_id: UUID) -> Optional[bytes]:
        """Retrieve artifact content from filesystem.

        Args:
            artifact_id: The artifact UUID

        Returns:
            Artifact content as bytes, or None if not found
        """
        artifact_path = self._get_artifact_path(artifact_id)

        if not artifact_path.exists():
            return None

        async with aiofiles.open(artifact_path, 'rb') as f:
            return await f.read()

    def get_path(self, artifact_id: UUID) -> Optional[Path]:
        """Get the filesystem path for an artifact if it exists.

        Args:
            artifact_id: The artifact UUID

        Returns:
            Path object if artifact exists, None otherwise
        """
        artifact_path = self._get_artifact_path(artifact_id)
        return artifact_path if artifact_path.exists() else None

    async def delete(self, artifact_id: UUID) -> bool:
        """Delete an artifact from filesystem.

        Args:
            artifact_id: The artifact UUID

        Returns:
            True if deleted, False if not found
        """
        artifact_path = self._get_artifact_path(artifact_id)

        if not artifact_path.exists():
            return False

        artifact_path.unlink()
        return True

    def get_storage_info(self) -> dict:
        """Get information about the artifact store.

        Returns:
            Dictionary with storage statistics
        """
        total_size = 0
        total_count = 0

        for path in self.base_path.rglob("*"):
            if path.is_file():
                total_size += path.stat().st_size
                total_count += 1

        return {
            "base_path": str(self.base_path),
            "total_artifacts": total_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2)
        }
