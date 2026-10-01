from __future__ import annotations

import hashlib
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from .config import Settings, get_settings


class ImmutableObjectError(RuntimeError):
    pass


class ObjectStore(ABC):
    @abstractmethod
    def put_if_absent(self, key: str, path: Path, sha256: str, length: int) -> None: ...

    @abstractmethod
    def get_bytes(self, key: str) -> bytes: ...

    @abstractmethod
    def get_to_path(self, key: str, destination: Path) -> Path: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...


class MemoryObjectStore(ObjectStore):
    """Deterministic in-process store used by the test suite and local SQLite demos."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._digests: dict[str, str] = {}
        self._lock = threading.Lock()

    def put_if_absent(self, key: str, path: Path, sha256: str, length: int) -> None:
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise ValueError(f"digest mismatch for {key}: expected {sha256}, calculated {actual}")
        with self._lock:
            if key in self._objects:
                if self._digests[key] != sha256 or self._objects[key] != data:
                    raise ImmutableObjectError(f"immutable object already exists with different bytes: {key}")
                return
            self._objects[key] = data
            self._digests[key] = sha256

    def get_bytes(self, key: str) -> bytes:
        with self._lock:
            return self._objects[key]

    def get_to_path(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.get_bytes(key))
        return destination

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._objects


class MinioObjectStore(ObjectStore):
    def __init__(self, settings: Settings) -> None:
        from minio import Minio

        self._client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        self.bucket = settings.minio_bucket
        if not self._client.bucket_exists(self.bucket):
            # Object Lock must be enabled at bucket creation in MinIO's supported
            # deployment mode. Retention is normally applied by infrastructure;
            # application keys are content-addressed and never mutated.
            self._client.make_bucket(self.bucket, object_lock=True)

    @staticmethod
    def _meta_digest(metadata: dict[str, str] | None) -> str | None:
        if not metadata:
            return None
        for key, value in metadata.items():
            if key.lower() in {"x-amz-meta-sha256", "sha256"}:
                return value
        return None

    def put_if_absent(self, key: str, path: Path, sha256: str, length: int) -> None:
        try:
            stat = self._client.stat_object(self.bucket, key)
        except Exception:
            stat = None
        if stat is not None:
            old = self._meta_digest(getattr(stat, "metadata", None))
            if old and old != sha256:
                raise ImmutableObjectError(f"immutable object already exists with different digest: {key}")
            return
        self._client.fput_object(
            self.bucket,
            key,
            str(path),
            length,
            content_type="application/octet-stream",
            metadata={"sha256": sha256},
        )

    def get_bytes(self, key: str) -> bytes:
        response = None
        try:
            response = self._client.get_object(self.bucket, key)
            return response.read()
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def get_to_path(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._client.fget_object(self.bucket, key, str(destination))
        return destination

    def exists(self, key: str) -> bool:
        try:
            self._client.stat_object(self.bucket, key)
            return True
        except Exception:
            return False


_store: ObjectStore | None = None
_store_lock = threading.Lock()


def get_object_store(settings: Settings | None = None) -> ObjectStore:
    global _store
    settings = settings or get_settings()
    with _store_lock:
        if _store is None:
            if settings.object_store == "memory" or (
                settings.object_store == "auto" and settings.database_url.startswith("sqlite")
            ):
                _store = MemoryObjectStore()
            else:
                _store = MinioObjectStore(settings)
        return _store


def set_object_store(store: ObjectStore | None) -> None:
    """Test seam allowing each pytest invocation to isolate object state."""

    global _store
    with _store_lock:
        _store = store


def spool_upload(chunk, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with destination.open("wb") as out:
        while True:
            data = chunk.read(8 * 1024 * 1024)
            if not data:
                break
            digest.update(data)
            size += len(data)
            out.write(data)
    os.chmod(destination, 0o440)
    if size == 0:
        raise ValueError("empty chunk is not accepted")
    return digest.hexdigest()
