from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Chunk, Manifest, ValidationIssue

TIME_TOLERANCE_SECONDS = 1e-6
CHANNEL_ORDER_NOT_SIGNIFICANT = True


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: Any) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def channel_set_hash(channels: list[str]) -> str:
    return hashlib.sha256(canonical_json(sorted(channels))).hexdigest()


def manifest_digest(expected_chunks: list[dict[str, Any]], channels: list[str]) -> str:
    payload = {"channel_set": sorted(channels), "expected_chunks": sorted(expected_chunks, key=lambda x: x["sequence"])}
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def add_issue(db: Session, manifest_id: str, severity: str, code: str, message: str, **details: Any) -> ValidationIssue:
    issue = ValidationIssue(
        manifest_id=manifest_id, severity=severity, code=code, message=message, details=details
    )
    db.add(issue)
    return issue


def _duration_seconds(entry: dict[str, Any]) -> float:
    return (entry["sample_count"] - 1) / float(entry["sample_rate"])


def compare_expected(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    keys = (
        "sha256",
        "byte_offset",
        "byte_length",
        "sample_count",
        "sample_rate",
        "start_time",
        "end_time",
        "encoding",
    )
    mismatches = []
    for key in keys:
        if actual[key] != expected[key]:
            mismatches.append(key)
    if sorted(actual["channels"]) != sorted(expected["channels"]):
        mismatches.append("channels")
    return mismatches


def validate_declaration(expected_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Structural validation performed before any byte is accepted.

    Sorting is deliberately used only to produce ordered diagnostics; every
    overlap/gap/duplicate is still reported, never collapsed.
    """

    errors: list[dict[str, Any]] = []
    if not expected_chunks:
        return [{"code": "empty_manifest", "message": "manifest contains no chunks"}]

    seen: dict[int, dict[str, Any]] = {}
    for entry in expected_chunks:
        seq = int(entry["sequence"])
        if seq in seen:
            errors.append({"code": "duplicate_sequence", "sequence": seq, "message": f"duplicate sequence {seq}"})
            continue
        seen[seq] = entry

    ordered = sorted(seen.values(), key=lambda item: item["sequence"])
    if [item["sequence"] for item in ordered] != list(range(len(ordered))):
        errors.append({"code": "sequence_not_contiguous", "message": "chunk sequences must be 0..N-1"})

    prev_end_offset = 0
    for item in ordered:
        if int(item["byte_offset"]) != prev_end_offset:
            errors.append(
                {
                    "code": "byte_range_discontinuous",
                    "sequence": item["sequence"],
                    "expected_offset": prev_end_offset,
                    "actual_offset": item["byte_offset"],
                }
            )
        prev_end_offset = int(item["byte_offset"]) + int(item["byte_length"])

    for prev, item in zip(ordered, ordered[1:]):
        if int(item["byte_offset"]) < int(prev["byte_offset"]) + int(prev["byte_length"]):
            errors.append(
                {
                    "code": "byte_range_overlap",
                    "sequences": [prev["sequence"], item["sequence"]],
                }
            )

    for item in ordered:
        try:
            start = parse_time(item["start_time"])
            end = parse_time(item["end_time"])
        except ValueError as exc:
            errors.append({"code": "invalid_timestamp", "sequence": item["sequence"], "message": str(exc)})
            continue
        actual_duration = (end - start).total_seconds()
        expected_duration = _duration_seconds(item)
        if abs(actual_duration - expected_duration) > TIME_TOLERANCE_SECONDS:
            errors.append(
                {
                    "code": "chunk_duration_mismatch",
                    "sequence": item["sequence"],
                    "actual_seconds": actual_duration,
                    "expected_seconds": expected_duration,
                }
            )

    for prev, item in zip(ordered, ordered[1:]):
        try:
            delta = (parse_time(item["start_time"]) - parse_time(prev["end_time"])).total_seconds()
        except ValueError:
            continue
        sample_interval = 1.0 / float(prev["sample_rate"])
        if abs(delta - sample_interval) > TIME_TOLERANCE_SECONDS:
            # The next block's first timestamp is measured with its own clock.
            expected_start = parse_time(prev["end_time"]) + (
                parse_time(item["end_time"]) - parse_time(item["start_time"])
            ) / max(1, int(item["sample_count"]) - 1)
            if abs((parse_time(item["start_time"]) - expected_start).total_seconds()) > TIME_TOLERANCE_SECONDS:
                errors.append(
                    {
                        "code": "sample_time_discontinuous",
                        "sequences": [prev["sequence"], item["sequence"]],
                        "gap_seconds": delta - sample_interval,
                    }
                )

    return errors


def begin_completion(db: Session, manifest_id: str) -> bool:
    result = db.execute(
        update(Manifest)
        .where(Manifest.id == manifest_id, Manifest.status == "open")
        .values(status="validating")
    )
    db.flush()
    return result.rowcount == 1


def finish_completion_failure(db: Session, manifest: Manifest, latest_error: dict[str, Any]) -> None:
    manifest.status = "open"
    manifest.error = latest_error


def validate_received_chunks(db: Session, manifest: Manifest) -> tuple[bool, list[ValidationIssue]]:
    # Keep prior failed attempts as an audit trail. Each issue is timestamped;
    # callers return the current attempt's list from this function.
    expected = {item["sequence"]: item for item in manifest.expected_chunks}
    received = db.scalars(select(Chunk).where(Chunk.manifest_id == manifest.id)).all()
    received_by_seq = {item.sequence: item for item in received}
    issues: list[ValidationIssue] = []

    missing = sorted(set(expected) - set(received_by_seq))
    for seq in missing:
        issues.append(
            add_issue(
                db,
                manifest.id,
                "error",
                "missing_chunk",
                f"chunk {seq} declared by manifest has not been uploaded",
                sequence=seq,
            )
        )

    for chunk in received:
        if chunk.sequence not in expected:
            issues.append(
                add_issue(
                    db,
                    manifest.id,
                    "error",
                    "unexpected_chunk",
                    f"chunk {chunk.sequence} is not part of the immutable manifest",
                    sequence=chunk.sequence,
                )
            )
            continue
        actual = _chunk_metadata(chunk)
        mismatches = compare_expected(actual, expected[chunk.sequence])
        if mismatches:
            issues.append(
                add_issue(
                    db,
                    manifest.id,
                    "error",
                    "chunk_metadata_mismatch",
                    f"chunk {chunk.sequence} does not match manifest",
                    sequence=chunk.sequence,
                    fields=mismatches,
                    expected=expected[chunk.sequence],
                    actual=actual,
                )
            )
        if channel_set_hash(chunk.channels) != manifest.channel_set_hash:
            issues.append(
                add_issue(
                    db,
                    manifest.id,
                    "error",
                    "channel_set_mismatch",
                    f"chunk {chunk.sequence} channel set differs from manifest",
                    sequence=chunk.sequence,
                    expected=sorted(manifest.channel_set),
                    actual=sorted(chunk.channels),
                )
            )

    ordered_chunks = sorted(received, key=lambda item: item.sequence)
    for prev, item in zip(ordered_chunks, ordered_chunks[1:]):
        if item.byte_offset < prev.byte_offset + prev.byte_length:
            issues.append(
                add_issue(
                    db,
                    manifest.id,
                    "error",
                    "byte_range_overlap",
                    "received chunks overlap; refusing to merge bytes",
                    sequences=[prev.sequence, item.sequence],
                )
            )
        elif item.byte_offset > prev.byte_offset + prev.byte_length:
            issues.append(
                add_issue(
                    db,
                    manifest.id,
                    "error",
                    "byte_range_gap",
                    "received chunks have a byte gap; refusing to infer missing samples",
                    sequences=[prev.sequence, item.sequence],
                    gap_bytes=item.byte_offset - (prev.byte_offset + prev.byte_length),
                )
            )
        try:
            next_interval = (parse_time(item.end_time) - parse_time(item.start_time)).total_seconds() / max(
                1, item.sample_count - 1
            )
            time_gap = (parse_time(item.start_time) - parse_time(prev.end_time)).total_seconds()
            if abs(time_gap - next_interval) > TIME_TOLERANCE_SECONDS:
                issues.append(
                    add_issue(
                        db,
                        manifest.id,
                        "error",
                        "sample_time_discontinuous",
                        "received chunks do not form one continuous sample timeline",
                        sequences=[prev.sequence, item.sequence],
                        gap_seconds=time_gap - next_interval,
                    )
                )
        except ValueError:
            pass

    rates = sorted({float(item["sample_rate"]) for item in manifest.expected_chunks})
    if len(rates) > 1:
        issues.append(
            add_issue(
                db,
                manifest.id,
                "warning",
                "sample_rate_changed",
                "sample rate is not constant; segments are analyzed separately and never interpolated",
                sample_rates=rates,
            )
        )

    db.flush()
    ok = not any(issue.severity == "error" for issue in issues)
    return ok, issues


def _chunk_metadata(chunk: Chunk) -> dict[str, Any]:
    return {
        "sequence": chunk.sequence,
        "sha256": chunk.sha256,
        "byte_offset": chunk.byte_offset,
        "byte_length": chunk.byte_length,
        "sample_count": chunk.sample_count,
        "sample_rate": chunk.sample_rate,
        "channels": list(chunk.channels),
        "start_time": chunk.start_time,
        "end_time": chunk.end_time,
        "encoding": chunk.encoding,
    }
