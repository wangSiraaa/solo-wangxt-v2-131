from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import BlockStatus, ManifestStatus
from app.models.orm import DataBlock, UploadManifest, utcnow
from app.schemas.api import ManifestCreate
from app.services.storage import ImmutableObjectError, get_store, make_object_key, sha256_bytes

_DTYPE_ITEMSIZE = {"float32": 4, "int16": 2}


@dataclass
class ValidationIssue:
    code: str
    severity: str
    message: str
    detail: dict[str, Any]


def to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def epoch_seconds(value: datetime) -> float:
    value = to_naive_utc(value)
    return value.replace(tzinfo=timezone.utc).timestamp()


def canonical_manifest_digest(payload: ManifestCreate) -> str:
    """Digest of the manifest contract, independent of block arrival order."""
    canonical = {
        "manifest_key": payload.manifest_key,
        "name": payload.name,
        "nominal_frequency": payload.nominal_frequency,
        "expected_sample_rate": payload.expected_sample_rate,
        "channels": [c.model_dump() for c in payload.channels],
        "blocks": [
            b.model_dump(mode="json", exclude_none=True)
            for b in sorted(payload.blocks, key=lambda x: x.block_index)
        ],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def create_manifest(db: Session, payload: ManifestCreate) -> UploadManifest:
    indexes = [b.block_index for b in payload.blocks]
    if len(indexes) != len(set(indexes)):
        raise ValueError("block_index values must be unique")
    if sorted(indexes) != list(range(len(indexes))):
        raise ValueError("block_index values must be the dense sequence 0..N-1")

    digest = canonical_manifest_digest(payload)
    first = min(payload.blocks, key=lambda b: b.block_index)
    last = max(payload.blocks, key=lambda b: b.block_index)
    channels = [c.model_dump() for c in payload.channels]
    manifest = UploadManifest(
        manifest_key=payload.manifest_key,
        name=payload.name,
        nominal_frequency=payload.nominal_frequency,
        expected_blocks=len(payload.blocks),
        channels=channels,
        channel_order=[c.name for c in channels],
        expected_sample_rate=payload.expected_sample_rate,
        start_sample=first.start_sample,
        end_sample=last.start_sample + last.sample_count,
        start_timestamp=to_naive_utc(first.start_timestamp),
        raw_manifest_digest=digest,
        status=ManifestStatus.created,
        validation={},
    )
    db.add(manifest)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"manifest_key already exists: {payload.manifest_key}") from exc

    for item in sorted(payload.blocks, key=lambda b: b.block_index):
        expected_bytes = item.sample_count * len(payload.channels) * _DTYPE_ITEMSIZE[item.dtype]
        if item.byte_size != expected_bytes:
            raise ValueError(
                f"block {item.block_index} byte_size {item.byte_size} != interleaved payload {expected_bytes}"
            )
        db.add(
            DataBlock(
                manifest_id=manifest.id,
                block_index=item.block_index,
                object_key=make_object_key(payload.manifest_key, item.block_index, item.sha256),
                byte_size=item.byte_size,
                sha256=item.sha256,
                dtype=item.dtype,
                start_sample=item.start_sample,
                sample_count=item.sample_count,
                sample_rate=item.sample_rate,
                start_timestamp=to_naive_utc(item.start_timestamp),
                status=BlockStatus.pending,
            )
        )
    db.commit()
    db.refresh(manifest)
    return manifest


def get_manifest_by_key(db: Session, key: str) -> UploadManifest | None:
    return db.scalar(select(UploadManifest).where(UploadManifest.manifest_key == key))


def upload_block(db: Session, manifest_key: str, block_index: int, data: bytes) -> DataBlock:
    manifest = get_manifest_by_key(db, manifest_key)
    if manifest is None:
        raise KeyError(f"unknown manifest: {manifest_key}")
    block = db.scalar(
        select(DataBlock).where(
            DataBlock.manifest_id == manifest.id, DataBlock.block_index == block_index
        )
    )
    if block is None:
        raise KeyError(f"unknown block {block_index}")

    actual_digest = sha256_bytes(data)
    if len(data) != block.byte_size:
        raise ValueError(
            f"byte length mismatch for block {block_index}: got {len(data)}, expected {block.byte_size}"
        )
    if actual_digest != block.sha256:
        raise ValueError(
            f"sha256 mismatch for block {block_index}: got {actual_digest}, expected {block.sha256}"
        )
    channel_count = len(manifest.channels)
    expected_bytes = block.sample_count * channel_count * _DTYPE_ITEMSIZE[block.dtype]
    if len(data) != expected_bytes:
        raise ValueError(f"block {block_index} does not contain {channel_count} interleaved channels")

    if block.status == BlockStatus.stored:
        return block

    store = get_store()
    try:
        store.put_if_absent(block.object_key, data, actual_digest)
    except ImmutableObjectError as exc:
        raise ValueError(f"immutable block conflict: {exc}") from exc

    block.status = BlockStatus.stored
    db.commit()
    db.refresh(block)
    return block


def _block_issues(manifest: UploadManifest, blocks: list[DataBlock]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    by_index = {b.block_index: b for b in blocks}
    expected_indexes = set(range(manifest.expected_blocks))
    missing_indexes = sorted(expected_indexes - set(by_index))
    extra_indexes = sorted(set(by_index) - expected_indexes)
    if missing_indexes:
        issues.append(
            ValidationIssue(
                "missing_block",
                "fatal",
                "block sequence is not dense; missing blocks cannot be silently filled",
                {"block_indexes": missing_indexes},
            )
        )
    if extra_indexes:
        issues.append(ValidationIssue("unexpected_block", "fatal", "unexpected block index", {"block_indexes": extra_indexes}))

    ordered = [by_index[i] for i in sorted(by_index)]
    unstored = [b.block_index for b in ordered if b.status != BlockStatus.stored]
    if unstored:
        issues.append(ValidationIssue("unstored_block", "fatal", "declared block has no verified object bytes", {"block_indexes": unstored}))

    previous: DataBlock | None = None
    rate_segments: list[dict[str, Any]] = []
    for block in ordered:
        if previous is None:
            rate_segments.append({"start_block": 0, "sample_rate": block.sample_rate})
        else:
            expected_sample = previous.start_sample + previous.sample_count
            if block.start_sample < expected_sample:
                issues.append(
                    ValidationIssue(
                        "overlapping_samples",
                        "fatal",
                        "sample ranges overlap; overlap must not be sorted away",
                        {
                            "previous_block": previous.block_index,
                            "block": block.block_index,
                            "overlap_samples": expected_sample - block.start_sample,
                            "sample_range": [block.start_sample, expected_sample],
                        },
                    )
                )
            elif block.start_sample > expected_sample:
                # An explicit acquisition gap is retained for analysis/display as NaN and makes
                # the resulting report incomplete; it is never closed by sorting adjacent blocks.
                gap_count = block.start_sample - expected_sample
                expected_time = epoch_seconds(previous.start_timestamp) + (
                    previous.sample_count + gap_count
                ) / previous.sample_rate
                actual_time = epoch_seconds(block.start_timestamp)
                tolerance = 1.01 / max(previous.sample_rate, block.sample_rate)
                if abs(actual_time - expected_time) > tolerance:
                    issues.append(
                        ValidationIssue(
                            "timestamp_discontinuity",
                            "fatal",
                            "declared gap does not match block timestamp discontinuity",
                            {
                                "after_block": previous.block_index,
                                "before_block": block.block_index,
                                "expected_timestamp": datetime.fromtimestamp(expected_time, timezone.utc).isoformat(),
                                "actual_timestamp": block.start_timestamp.isoformat(),
                                "delta_seconds": actual_time - expected_time,
                            },
                        )
                    )
                issues.append(
                    ValidationIssue(
                        "missing_sample_interval",
                        "warning",
                        "sample interval is absent; gap is represented explicitly instead of concatenated",
                        {
                            "after_block": previous.block_index,
                            "before_block": block.block_index,
                            "start_sample": expected_sample,
                            "end_sample": block.start_sample,
                            "missing_samples": block.start_sample - expected_sample,
                        },
                    )
                )
            else:
                expected_time = epoch_seconds(previous.start_timestamp) + previous.sample_count / previous.sample_rate
                actual_time = epoch_seconds(block.start_timestamp)
                tolerance = 1.01 / max(previous.sample_rate, block.sample_rate)
                if abs(actual_time - expected_time) > tolerance:
                    issues.append(
                        ValidationIssue(
                            "timestamp_discontinuity",
                            "fatal",
                            "timestamps are not continuous across contiguous samples",
                            {
                                "after_block": previous.block_index,
                                "before_block": block.block_index,
                                "expected_timestamp": datetime.fromtimestamp(expected_time, timezone.utc).isoformat(),
                                "actual_timestamp": block.start_timestamp.isoformat(),
                                "delta_seconds": actual_time - expected_time,
                            },
                        )
                    )
            if block.sample_rate != previous.sample_rate:
                rate_segments.append({"start_block": block.block_index, "sample_rate": block.sample_rate})
                issues.append(
                    ValidationIssue(
                        "sample_rate_change",
                        "warning",
                        "sample rate changes at block boundary; analysis records it as a quality state",
                        {
                            "block": block.block_index,
                            "from_hz": previous.sample_rate,
                            "to_hz": block.sample_rate,
                        },
                    )
                )
        previous = block

    if manifest.expected_sample_rate is not None:
        changed = [
            {"block": b.block_index, "sample_rate": b.sample_rate}
            for b in ordered
            if b.sample_rate != manifest.expected_sample_rate
        ]
        if changed:
            issues.append(
                ValidationIssue(
                    "sample_rate_differs_from_manifest",
                    "warning",
                    "block sample rate differs from declared manifest sample rate",
                    {"expected_hz": manifest.expected_sample_rate, "blocks": changed},
                )
            )

    channel_count = len(manifest.channels)
    for block in ordered:
        expected_bytes = block.sample_count * channel_count * _DTYPE_ITEMSIZE[block.dtype]
        if block.byte_size != expected_bytes:
            issues.append(
                ValidationIssue(
                    "channel_set_byte_mismatch",
                    "fatal",
                    "byte payload cannot be decoded as the declared channel set",
                    {
                        "block": block.block_index,
                        "expected_bytes": expected_bytes,
                        "actual_bytes": block.byte_size,
                        "channels": manifest.channel_order,
                    },
                )
            )
    return issues


def complete_manifest(db: Session, manifest_key: str) -> tuple[UploadManifest, bool]:
    # Row lock serializes concurrent completion on PostgreSQL. SQLite relies on its write lock.
    q = select(UploadManifest).where(UploadManifest.manifest_key == manifest_key).with_for_update()
    try:
        manifest = db.scalar(q)
    except Exception:
        manifest = get_manifest_by_key(db, manifest)
    if manifest is None:
        raise KeyError(f"unknown manifest: {manifest_key}")
    if manifest.status in {ManifestStatus.completed, ManifestStatus.completed_with_defects}:
        return manifest, True

    blocks = list(manifest.blocks)
    issues = _block_issues(manifest, blocks)
    fatal = [i for i in issues if i.severity == "fatal"]
    warnings = [i for i in issues if i.severity != "fatal"]
    validation = {
        "checked_at": utcnow().isoformat(),
        "block_count": len(blocks),
        "expected_block_count": manifest.expected_blocks,
        "channels": manifest.channel_order,
        "issues": [
            {"code": i.code, "severity": i.severity, "message": i.message, "detail": i.detail} for i in issues
        ],
        "sample_rate_segments": _sample_rate_segments(blocks),
    }
    manifest.validation = validation
    if fatal:
        manifest.status = ManifestStatus.validation_failed
        manifest.completed_at = None
        db.commit()
        db.refresh(manifest)
        return manifest, False

    manifest.status = (
        ManifestStatus.completed_with_defects if warnings else ManifestStatus.completed
    )
    manifest.completed_at = utcnow()
    db.commit()
    db.refresh(manifest)
    return manifest, False


def _sample_rate_segments(blocks: list[DataBlock]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for block in sorted(blocks, key=lambda b: b.block_index):
        if not segments or segments[-1]["sample_rate"] != block.sample_rate:
            segments.append({"start_block": block.block_index, "sample_rate": block.sample_rate})
    return segments
