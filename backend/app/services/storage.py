from __future__ import annotations

import base64
import hashlib
from contextlib import contextmanager
from pathlib import Path
import tempfile
from typing import Iterator, Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings


class ImmutableObjectError(RuntimeError):
    pass


class ObjectStore(Protocol):
    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    @contextmanager
    def spool(self, key: str) -> Iterator[Path]:
        get_path = getattr(self, "path", None)
        if get_path is not None:
            yield get_path(key)
            return
        with tempfile.NamedTemporaryFile(suffix=".raw") as tmp:
            tmp.write(self.get(key))
            tmp.flush()
            yield Path(tmp.name)


class LocalObjectStore:
    """Filesystem implementation used for unit tests and air-gapped demos."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("invalid object key")
        return self.root / key

    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        path = self._path(key)
        digest_path = path.with_suffix(path.suffix + ".sha256")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = digest_path.read_text().strip()
            if existing != sha256:
                raise ImmutableObjectError(f"immutable object already exists with different digest: {key}")
            return key
        # A unique temp file in same directory gives atomic, content-addressed behavior.
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        tmp.write_bytes(data)
        digest_path.with_name(f".{path.name}.sha256.tmp-{os.getpid()}").write_text(sha256)
        os.replace(tmp, path)
        os.replace(digest_path.with_name(f".{path.name}.sha256.tmp-{os.getpid()}"), digest_path)
        os.chmod(path, 0o440)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def path(self, key: str) -> Path:
        p = self._path(key)
        if not p.exists():
            raise FileNotFoundError(key)
        return p

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3ObjectStore:
    """MinIO/S3 store. The bucket is versioned; same-key overwrite is rejected."""

    def __init__(self):
        s = get_settings()
        self.bucket = s.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url,
            aws_access_key_id=s.s3_access_key,
            aws_secret_access_key=s.s3_secret_key,
            region_name=s.s3_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        self.ensure_bucket()

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            # Object Lock is normally provisioned by docker-compose/IaC at bucket creation.
            self.client.create_bucket(Bucket=self.bucket)
        try:
            self.client.put_bucket_versioning(Bucket=self.bucket, VersioningConfiguration={"Status": "Enabled"})
        except ClientError:
            # Object Lock/versioning may already be managed by deployment policy.
            pass
        try:
            self.client.head_object(Bucket=self.bucket, Key="README.txt")
        except ClientError:
            self.client.put_object(
                Bucket=self.bucket,
                Key="README.txt",
                Body=(
                    "Raw waveform blocks are immutable. Object keys contain the manifest and "
                    "client-claimed SHA-256; writes use If-None-Match=*."
                ).encode(),
            )

    def put_if_absent(self, key: str, data: bytes, sha256: str) -> str:
        checksum_base64 = base64.b64encode(bytes.fromhex(sha256)).decode()
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType="application/octet-stream",
                Metadata={"sha256": sha256},
                ChecksumSHA256=checksum_base64,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"PreconditionFailed", "412"}:
                raise ImmutableObjectError(f"object key already exists: {key}") from exc
            raise
        return key

    def get(self, key: str) -> bytes:
        obj = self.client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    @contextmanager
    def spool(self, key: str) -> Iterator[Path]:
        with tempfile.NamedTemporaryFile(suffix=".raw") as tmp:
            self.client.download_fileobj(Bucket=self.bucket, Key=key, Fileobj=tmp)
            tmp.flush()
            yield Path(tmp.name)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store
    if _store is None:
        settings = get_settings()
        _store = LocalObjectStore(settings.local_storage_path) if settings.storage_backend == "local" else S3ObjectStore()
    return _store


def reset_store_cache() -> None:
    global _store
    _store = None


def make_object_key(manifest_key: str, block_index: int, sha256: str) -> str:
    safe = manifest_key.replace("/", "_").replace(" ", "_")
    return f"manifests/{safe}/blocks/{block_index:09d}-{sha256}.raw"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
