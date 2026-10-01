from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..models import Chunk, Manifest, ValidationIssue, utcnow
from ..schemas import ChunkOut, IssueOut, ManifestCreate, ManifestOut
from ..storage import get_object_store, spool_upload
from ..validation import (
    begin_completion,
    channel_set_hash,
    finish_completion_failure,
    manifest_digest,
    validate_declaration,
    validate_received_chunks,
)

router = APIRouter(prefix="/manifests", tags=["manifests"])


@router.post("", response_model=ManifestOut, status_code=status.HTTP_201_CREATED)
def create_manifest(payload: ManifestCreate, db: Session = Depends(get_db)):
    expected = [item.model_dump() for item in payload.expected_chunks]
    declaration_errors = validate_declaration(expected)
    if declaration_errors:
        raise HTTPException(status_code=422, detail={"code": "invalid_manifest", "issues": declaration_errors})

    channels = sorted({channel for item in expected for channel in item["channels"]})
    digest = channel_set_hash(channels)
    manifest = Manifest(
        name=payload.name,
        status="open",
        expected_chunks=expected,
        channel_set=channels,
        channel_set_hash=digest,
        nominal_sample_rate=payload.nominal_sample_rate,
        start_time=payload.start_time or min(item["start_time"] for item in expected),
        end_time=payload.end_time or max(item["end_time"] for item in expected),
        manifest_digest=manifest_digest(expected, channels),
    )
    db.add(manifest)
    db.commit()
    db.refresh(manifest)
    return manifest


@router.get("", response_model=list[ManifestOut])
def list_manifests(db: Session = Depends(get_db)):
    return db.scalars(select(Manifest).order_by(Manifest.created_at.desc())).all()


@router.get("/{manifest_id}", response_model=ManifestOut)
def get_manifest(manifest_id: str, db: Session = Depends(get_db)):
    manifest = db.get(Manifest, manifest_id)
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    return manifest


@router.put("/{manifest_id}/chunks/{sequence}/raw", response_model=ChunkOut)
def upload_raw_chunk(
    manifest_id: str,
    sequence: int,
    chunk_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    manifest = db.get(Manifest, manifest_id)
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    if manifest.status != "open":
        raise HTTPException(409, f"manifest is {manifest.status}; raw blocks are immutable")
    expected = next((item for item in manifest.expected_chunks if int(item["sequence"]) == sequence), None)
    if expected is None:
        raise HTTPException(422, f"sequence {sequence} is not declared in immutable manifest")

    existing = db.scalar(
        select(Chunk).where(Chunk.manifest_id == manifest_id, Chunk.sequence == sequence).limit(1)
    )
    if existing is not None:
        # Repeated upload of the exact content-addressed block is a safe retry.
        return existing

    settings = get_settings()
    spool_dir = Path(settings.spool_dir) / manifest_id
    with tempfile.NamedTemporaryFile(prefix=f"chunk-{sequence}-", dir=spool_dir, delete=False) as tmp:
        temp_path = Path(tmp.name)
    try:
        actual_digest = spool_upload(chunk_file.file, temp_path)
        if actual_digest != expected["sha256"]:
            raise HTTPException(
                422,
                detail={
                    "code": "byte_digest_mismatch",
                    "sequence": sequence,
                    "expected": expected["sha256"],
                    "actual": actual_digest,
                },
            )
        if temp_path.stat().st_size != expected["byte_length"]:
            raise HTTPException(
                422,
                detail={
                    "code": "byte_length_mismatch",
                    "sequence": sequence,
                    "expected": expected["byte_length"],
                    "actual": temp_path.stat().st_size,
                },
            )
        object_key = f"raw/{manifest_id}/{sequence:09d}/{expected['sha256']}.bin"
        get_object_store().put_if_absent(
            object_key, temp_path, expected["sha256"], int(expected["byte_length"])
        )
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except PermissionError:
            pass

    chunk = Chunk(
        manifest_id=manifest_id,
        sequence=sequence,
        object_key=object_key,
        sha256=expected["sha256"],
        byte_offset=int(expected["byte_offset"]),
        byte_length=int(expected["byte_length"]),
        sample_count=int(expected["sample_count"]),
        sample_rate=float(expected["sample_rate"]),
        channels=list(expected["channels"]),
        start_time=expected["start_time"],
        end_time=expected["end_time"],
        encoding=expected.get("encoding", "float32le-interleaved"),
    )
    db.add(chunk)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        existing = db.scalar(
            select(Chunk).where(Chunk.manifest_id == manifest_id, Chunk.sequence == sequence).limit(1)
        )
        if existing and existing.sha256 == expected["sha256"]:
            return existing
        raise HTTPException(409, f"concurrent chunk insert conflict: {exc}") from exc
    db.refresh(chunk)
    return chunk


@router.get("/{manifest_id}/chunks", response_model=list[ChunkOut])
def list_chunks(manifest_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(Chunk).where(Chunk.manifest_id == manifest_id).order_by(Chunk.sequence)
    ).all()


@router.post("/{manifest_id}/finalize")
def finalize_manifest(manifest_id: str, db: Session = Depends(get_db)):
    manifest = db.get(Manifest, manifest_id)
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    if manifest.status == "completed":
        return {"completed": False, "already_completed": True, "manifest_id": manifest_id}
    if not begin_completion(db, manifest_id):
        db.rollback()
        raise HTTPException(409, "another completion attempt is in progress")

    ok, issues = validate_received_chunks(db, manifest)
    if not ok:
        latest = next((issue for issue in issues if issue.severity == "error"), None)
        error = {
            "code": latest.code if latest else "validation_failed",
            "message": latest.message if latest else "validation failed",
            "issue_count": len(issues),
        }
        finish_completion_failure(db, manifest, error)
        db.commit()
        raise HTTPException(
            422,
            detail={
                "code": "manifest_validation_failed",
                "manifest_id": manifest_id,
                "issues": [
                    {
                        "severity": issue.severity,
                        "code": issue.code,
                        "message": issue.message,
                        "details": issue.details,
                    }
                    for issue in issues
                ],
            },
        )

    ordered = sorted(manifest.expected_chunks, key=lambda item: item["sequence"])
    manifest.status = "completed"
    manifest.start_time = ordered[0]["start_time"]
    manifest.end_time = ordered[-1]["end_time"]
    manifest.completed_at = utcnow()
    manifest.error = None
    db.commit()
    return {
        "completed": True,
        "already_completed": False,
        "manifest_id": manifest_id,
        "warnings": [
            {"code": issue.code, "message": issue.message, "details": issue.details}
            for issue in issues
            if issue.severity == "warning"
        ],
    }


@router.get("/{manifest_id}/issues", response_model=list[IssueOut])
def get_issues(manifest_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(ValidationIssue)
        .where(ValidationIssue.manifest_id == manifest_id)
        .order_by(ValidationIssue.created_at)
    ).all()
