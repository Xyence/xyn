"""Runtime artifact storage backends and factory."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Optional
from uuid import UUID

import aiofiles

DEFAULT_ARTIFACT_ROOT = ".xyn/artifacts"

_RUNTIME_STORE_SINGLETON: "ArtifactStoreBase | None" = None


def _artifact_key_for_id(artifact_id: UUID) -> str:
    token = str(artifact_id)
    return f"{token[:2]}/{token[2:4]}/{token}"


def _read_env(key: str, default: str = "", aliases: tuple[str, ...] = ()) -> str:
    value = os.getenv(key)
    if value and str(value).strip():
        return str(value).strip()
    for alias in aliases:
        candidate = os.getenv(alias)
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return default


@dataclass(frozen=True)
class RuntimeArtifactStorageConfig:
    provider: str
    local_root: Path
    s3_bucket: str
    s3_region: str
    s3_prefix: str
    s3_endpoint_url: str
    s3_kms_key_id: str
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_session_token: str


def resolve_runtime_artifact_storage_config() -> RuntimeArtifactStorageConfig:
    provider = _read_env(
        "XYN_RUNTIME_ARTIFACT_PROVIDER",
        _read_env("XYN_ARTIFACT_STORE_PROVIDER", "local"),
    ).lower()
    if provider in {"filesystem", "fs"}:
        provider = "local"
    if provider not in {"local", "s3"}:
        raise ValueError(f"Unsupported runtime artifact provider '{provider}'. Expected local or s3.")

    local_root = Path(
        _read_env(
            "XYN_ARTIFACT_ROOT",
            _read_env("ARTIFACT_STORE_PATH", DEFAULT_ARTIFACT_ROOT),
            aliases=("XYN_RUNTIME_ARTIFACT_LOCAL_ROOT",),
        )
    )
    s3_bucket = _read_env("XYN_RUNTIME_ARTIFACT_S3_BUCKET", aliases=("XYN_ARTIFACT_S3_BUCKET",))
    s3_region = _read_env("XYN_RUNTIME_ARTIFACT_S3_REGION", aliases=("XYN_ARTIFACT_S3_REGION",))
    s3_prefix = _read_env(
        "XYN_RUNTIME_ARTIFACT_S3_PREFIX",
        _read_env("XYN_ARTIFACT_S3_PREFIX", "runtime-artifacts"),
    ).strip("/")
    s3_endpoint_url = _read_env("XYN_RUNTIME_ARTIFACT_S3_ENDPOINT_URL", aliases=("XYN_ARTIFACT_S3_ENDPOINT_URL",))
    s3_kms_key_id = _read_env("XYN_RUNTIME_ARTIFACT_S3_KMS_KEY_ID", aliases=("XYN_ARTIFACT_S3_KMS_KEY_ID",))
    s3_access_key_id = _read_env("XYN_RUNTIME_ARTIFACT_S3_ACCESS_KEY_ID", aliases=("AWS_ACCESS_KEY_ID",))
    s3_secret_access_key = _read_env("XYN_RUNTIME_ARTIFACT_S3_SECRET_ACCESS_KEY", aliases=("AWS_SECRET_ACCESS_KEY",))
    s3_session_token = _read_env("XYN_RUNTIME_ARTIFACT_S3_SESSION_TOKEN", aliases=("AWS_SESSION_TOKEN",))

    return RuntimeArtifactStorageConfig(
        provider=provider,
        local_root=local_root,
        s3_bucket=s3_bucket,
        s3_region=s3_region,
        s3_prefix=s3_prefix,
        s3_endpoint_url=s3_endpoint_url,
        s3_kms_key_id=s3_kms_key_id,
        s3_access_key_id=s3_access_key_id,
        s3_secret_access_key=s3_secret_access_key,
        s3_session_token=s3_session_token,
    )


class ArtifactStoreBase:
    provider = "unknown"
    mode = "unknown"

    def store_bytes(self, *, artifact_id: UUID, content: bytes, compute_sha256: bool = True) -> tuple[str, Optional[str]]:
        raise NotImplementedError

    def retrieve_bytes(self, *, artifact_id: UUID) -> Optional[bytes]:
        raise NotImplementedError

    def get_local_path(self, *, artifact_id: UUID) -> Optional[Path]:
        return None

    def delete_artifact(self, *, artifact_id: UUID) -> bool:
        raise NotImplementedError

    def get_storage_info(self) -> dict[str, Any]:
        raise NotImplementedError

    async def store(self, artifact_id: UUID, content: bytes, compute_sha256: bool = True) -> tuple[str, Optional[str]]:
        return await asyncio.to_thread(self.store_bytes, artifact_id=artifact_id, content=content, compute_sha256=compute_sha256)

    async def store_stream(self, artifact_id: UUID, stream: BinaryIO, compute_sha256: bool = True) -> tuple[str, Optional[str], int]:
        content = await asyncio.to_thread(stream.read)
        if content is None:
            content = b""
        if not isinstance(content, (bytes, bytearray)):
            content = bytes(content)
        storage_key, sha256_hash = await self.store(artifact_id=artifact_id, content=bytes(content), compute_sha256=compute_sha256)
        return storage_key, sha256_hash, len(content)

    async def retrieve(self, artifact_id: UUID) -> Optional[bytes]:
        return await asyncio.to_thread(self.retrieve_bytes, artifact_id=artifact_id)

    def get_path(self, artifact_id: UUID) -> Optional[Path]:
        return self.get_local_path(artifact_id=artifact_id)

    async def delete(self, artifact_id: UUID) -> bool:
        return await asyncio.to_thread(self.delete_artifact, artifact_id=artifact_id)


class LocalFSArtifactStore(ArtifactStoreBase):
    """Local filesystem-backed artifact storage."""

    provider = "local"
    mode = "filesystem"

    def __init__(self, base_path: str = DEFAULT_ARTIFACT_ROOT):
        self.base_path = Path(base_path).expanduser().resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _path_for_id(self, artifact_id: UUID, *, create_dirs: bool = False) -> Path:
        rel = Path(_artifact_key_for_id(artifact_id))
        path = self.base_path / rel
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def store_bytes(self, *, artifact_id: UUID, content: bytes, compute_sha256: bool = True) -> tuple[str, Optional[str]]:
        artifact_path = self._path_for_id(artifact_id, create_dirs=True)
        sha256_hash = hashlib.sha256(content).hexdigest() if compute_sha256 else None
        artifact_path.write_bytes(content)
        return str(artifact_path.relative_to(self.base_path)), sha256_hash

    async def store_stream(self, artifact_id: UUID, stream: BinaryIO, compute_sha256: bool = True) -> tuple[str, Optional[str], int]:
        artifact_path = self._path_for_id(artifact_id, create_dirs=True)
        sha256_hasher = hashlib.sha256() if compute_sha256 else None
        byte_length = 0
        async with aiofiles.open(artifact_path, "wb") as handle:
            while True:
                chunk = await asyncio.to_thread(stream.read, 8192)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    chunk = bytes(chunk)
                await handle.write(bytes(chunk))
                byte_length += len(chunk)
                if sha256_hasher:
                    sha256_hasher.update(chunk)
        return str(artifact_path.relative_to(self.base_path)), sha256_hasher.hexdigest() if sha256_hasher else None, byte_length

    def retrieve_bytes(self, *, artifact_id: UUID) -> Optional[bytes]:
        artifact_path = self._path_for_id(artifact_id, create_dirs=False)
        if not artifact_path.exists():
            return None
        return artifact_path.read_bytes()

    def get_local_path(self, *, artifact_id: UUID) -> Optional[Path]:
        artifact_path = self._path_for_id(artifact_id, create_dirs=False)
        return artifact_path if artifact_path.exists() else None

    def delete_artifact(self, *, artifact_id: UUID) -> bool:
        artifact_path = self._path_for_id(artifact_id, create_dirs=False)
        if not artifact_path.exists():
            return False
        artifact_path.unlink()
        return True

    def get_storage_info(self) -> dict[str, Any]:
        total_size = 0
        total_count = 0
        for path in self.base_path.rglob("*"):
            if path.is_file():
                total_size += path.stat().st_size
                total_count += 1
        return {
            "provider": self.provider,
            "mode": self.mode,
            "base_path": str(self.base_path),
            "total_artifacts": total_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "remote_durability_active": False,
        }


class S3ArtifactStore(ArtifactStoreBase):
    """S3-backed runtime artifact storage."""

    provider = "s3"
    mode = "object_storage"

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "",
        prefix: str = "",
        endpoint_url: str = "",
        kms_key_id: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        session_token: str = "",
        client: Any = None,
    ):
        if not bucket.strip():
            raise ValueError("S3 runtime artifact storage requires a non-empty bucket.")
        self.bucket = bucket.strip()
        self.region = region.strip()
        self.prefix = prefix.strip().strip("/")
        self.endpoint_url = endpoint_url.strip()
        self.kms_key_id = kms_key_id.strip()
        self._client = client or self._build_client(
            region=self.region,
            endpoint_url=self.endpoint_url,
            access_key_id=access_key_id.strip(),
            secret_access_key=secret_access_key.strip(),
            session_token=session_token.strip(),
        )

    @staticmethod
    def _build_client(*, region: str, endpoint_url: str, access_key_id: str, secret_access_key: str, session_token: str) -> Any:
        try:
            import boto3  # type: ignore
        except Exception as exc:  # pragma: no cover - exercised via explicit runtime configuration
            raise RuntimeError("S3 runtime artifact provider selected, but boto3 is not installed.") from exc
        kwargs: dict[str, Any] = {}
        if region:
            kwargs["region_name"] = region
        if endpoint_url:
            kwargs["endpoint_url"] = endpoint_url
        if access_key_id and secret_access_key:
            kwargs["aws_access_key_id"] = access_key_id
            kwargs["aws_secret_access_key"] = secret_access_key
        if session_token:
            kwargs["aws_session_token"] = session_token
        return boto3.client("s3", **kwargs)

    def _object_key(self, artifact_id: UUID) -> str:
        base_key = _artifact_key_for_id(artifact_id)
        return f"{self.prefix}/{base_key}" if self.prefix else base_key

    def store_bytes(self, *, artifact_id: UUID, content: bytes, compute_sha256: bool = True) -> tuple[str, Optional[str]]:
        object_key = self._object_key(artifact_id)
        put_args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": object_key,
            "Body": content,
        }
        if self.kms_key_id:
            put_args["ServerSideEncryption"] = "aws:kms"
            put_args["SSEKMSKeyId"] = self.kms_key_id
        self._client.put_object(**put_args)
        sha256_hash = hashlib.sha256(content).hexdigest() if compute_sha256 else None
        return object_key, sha256_hash

    def retrieve_bytes(self, *, artifact_id: UUID) -> Optional[bytes]:
        object_key = self._object_key(artifact_id)
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=object_key)
        except Exception:
            return None
        body = response.get("Body")
        if body is None:
            return None
        return body.read()

    def delete_artifact(self, *, artifact_id: UUID) -> bool:
        object_key = self._object_key(artifact_id)
        try:
            self._client.delete_object(Bucket=self.bucket, Key=object_key)
        except Exception:
            return False
        return True

    def get_storage_info(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "mode": self.mode,
            "bucket": self.bucket,
            "region": self.region,
            "prefix": self.prefix,
            "endpoint_url": self.endpoint_url,
            "remote_durability_active": True,
        }


def create_runtime_artifact_store(*, config: Optional[RuntimeArtifactStorageConfig] = None) -> ArtifactStoreBase:
    cfg = config or resolve_runtime_artifact_storage_config()
    if cfg.provider == "s3":
        if not cfg.s3_bucket:
            raise ValueError("XYN_RUNTIME_ARTIFACT_PROVIDER=s3 requires XYN_RUNTIME_ARTIFACT_S3_BUCKET.")
        return S3ArtifactStore(
            bucket=cfg.s3_bucket,
            region=cfg.s3_region,
            prefix=cfg.s3_prefix,
            endpoint_url=cfg.s3_endpoint_url,
            kms_key_id=cfg.s3_kms_key_id,
            access_key_id=cfg.s3_access_key_id,
            secret_access_key=cfg.s3_secret_access_key,
            session_token=cfg.s3_session_token,
        )
    return LocalFSArtifactStore(base_path=str(cfg.local_root))


def get_runtime_artifact_store(*, reset: bool = False) -> ArtifactStoreBase:
    global _RUNTIME_STORE_SINGLETON
    if reset or _RUNTIME_STORE_SINGLETON is None:
        _RUNTIME_STORE_SINGLETON = create_runtime_artifact_store()
    return _RUNTIME_STORE_SINGLETON


def runtime_artifact_storage_status(*, config: Optional[RuntimeArtifactStorageConfig] = None) -> dict[str, Any]:
    cfg = config or resolve_runtime_artifact_storage_config()
    if cfg.provider == "s3" and cfg.s3_bucket:
        return {
            "provider": "s3",
            "mode": "object_storage",
            "configured": True,
            "remote_durability_active": True,
            "bucket": cfg.s3_bucket,
            "prefix": cfg.s3_prefix,
            "region": cfg.s3_region,
        }
    return {
        "provider": "local",
        "mode": "filesystem",
        "configured": True,
        "remote_durability_active": False,
        "path": str(cfg.local_root.resolve()),
    }


__all__ = [
    "ArtifactStoreBase",
    "LocalFSArtifactStore",
    "S3ArtifactStore",
    "RuntimeArtifactStorageConfig",
    "resolve_runtime_artifact_storage_config",
    "create_runtime_artifact_store",
    "get_runtime_artifact_store",
    "runtime_artifact_storage_status",
]
