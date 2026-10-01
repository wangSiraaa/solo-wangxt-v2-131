from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .database import SessionLocal
from .dsp import analyze_all_segments, group_chunks_by_rate
from .models import AnalysisTask, CalibrationVersion, Chunk, Manifest, Report, utcnow
from .services import snapshot_digest
from .storage import get_object_store

WORKER_ID = os.environ.get("PQ_WORKER_ID", f"worker-{uuid.uuid4().hex[:8]}")


class TaskCancelled(RuntimeError):
    pass


class SpectrumCrash(RuntimeError):
    """Synthetic crash used by the acceptance suite to prove no partial report is published."""


def acquire_task(db: Session, task_id: str, lease_seconds: int, worker_id: str = WORKER_ID) -> AnalysisTask | None:
    now = utcnow()
    lease_until = now + timedelta(seconds=lease_seconds)
    result = db.execute(
        update(AnalysisTask)
        .where(
            AnalysisTask.id == task_id,
            AnalysisTask.status.in_(["queued", "retry_wait"]),
            AnalysisTask.cancellation_requested.is_(False),
        )
        .values(
            status="running",
            lease_owner=worker_id,
            lease_until=lease_until,
            heartbeat_at=now,
            started_at=now,
            attempts=AnalysisTask.attempts + 1,
        )
    )
    if result.rowcount:
        db.flush()
        return db.get(AnalysisTask, task_id)
    # Reclaim a crashed worker's stale lease. This still only permits one owner.
    result = db.execute(
        update(AnalysisTask)
        .where(
            AnalysisTask.id == task_id,
            AnalysisTask.status == "running",
            AnalysisTask.lease_until < now,
        )
        .values(
            status="retry_wait",
            lease_owner=worker_id,
            lease_until=lease_until,
            heartbeat_at=now,
            attempts=AnalysisTask.attempts + 1,
        )
    )
    if result.rowcount:
        db.flush()
        return db.get(AnalysisTask, task_id)
    return None


def request_cancel(db: Session, task: AnalysisTask) -> AnalysisTask:
    task.cancellation_requested = True
    if task.status in {"queued", "retry_wait"}:
        task.status = "cancelled"
        task.ended_at = utcnow()
        task.lease_owner = None
        task.lease_until = None
    db.flush()
    return task


def request_retry(db: Session, task: AnalysisTask) -> AnalysisTask:
    if task.report is not None and task.report.status == "published":
        raise ValueError("a published report cannot be retried; create a new fixed task")
    task.status = "retry_wait"
    task.cancellation_requested = False
    task.lease_owner = None
    task.lease_until = None
    task.error_code = None
    task.error_message = None
    db.flush()
    return task


def recover_stale_tasks(db: Session) -> int:
    count = db.execute(
        update(AnalysisTask)
        .where(AnalysisTask.status == "running", AnalysisTask.lease_until < utcnow())
        .values(status="retry_wait", lease_owner=None, lease_until=None)
    ).rowcount
    db.commit()
    return int(count or 0)


def save_stage(db: Session, task: AnalysisTask, stage: str, value: dict, status: str = "running") -> None:
    task.stage_results = {**(task.stage_results or {}), stage: value}
    task.status = status
    task.heartbeat_at = utcnow()
    db.commit()


def _raise_if_cancelled(db: Session, task: AnalysisTask) -> None:
    db.refresh(task)
    if task.cancellation_requested:
        raise TaskCancelled("task cancellation requested")


def _load_calibration(db: Session, task: AnalysisTask) -> CalibrationVersion:
    calibration = db.get(CalibrationVersion, task.calibration_version_id)
    if calibration is None:
        raise LookupError(f"fixed calibration version is missing: {task.calibration_version_id}")
    return calibration


def execute_task(task_id: str, *, lease_seconds: int = 900, crash_after_stage: str | None = None) -> dict:
    """Idempotent execution boundary.

    A report row is inserted only in the terminal `publish_report` transaction.
    If the spectrum stage crashes, prior diagnostics remain on the task but the
    task is not `succeeded` and no report exists.
    """

    db = SessionLocal()
    try:
        task = acquire_task(db, task_id, lease_seconds)
        db.commit()
        if task is None:
            task = db.get(AnalysisTask, task_id)
            return {"status": task.status if task else "missing", "published": False}

        task = db.get(AnalysisTask, task_id)
        manifest = db.get(Manifest, task.manifest_snapshot["manifest_id"])
        if manifest is None:
            return _fail(db, task, "manifest_missing", "manifest no longer exists", fatal=True)
        calibration = _load_calibration(db, task)
        snapshot = dict(task.manifest_snapshot)
        params = dict(snapshot["params"])
        # Defensive invariant: task creation fixes parameters and coefficients.
        # The mutable calibration row is used only to prove the frozen version
        # still exists; its current coefficients are never copied in.
        if calibration.channel_set_hash != snapshot["channel_set_hash"]:
            return _fail(db, task, "calibration_channel_mismatch", "calibration does not belong to frozen channel set", fatal=True)

        save_stage(db, task, "fixed_snapshot", {"snapshot_digest": snapshot_digest(snapshot)})
        _raise_if_cancelled(db, task)

        expected_by_seq = {int(item["sequence"]): item for item in snapshot["expected_chunks"]}
        chunks = db.scalars(select(Chunk).where(Chunk.manifest_id == manifest.id)).all()
        loaded_meta = sorted(
            (
                {
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
                    "object_key": chunk.object_key,
                }
                for chunk in chunks
                if chunk.sequence in expected_by_seq
            ),
            key=lambda item: item["sequence"],
        )
        if len(loaded_meta) != len(expected_by_seq):
            missing = sorted(set(expected_by_seq) - {item["sequence"] for item in loaded_meta})
            return _fail(
                db,
                task,
                "frozen_manifest_incomplete",
                f"frozen manifest cannot be assembled; missing sequences: {missing}",
                fatal=True,
            )
        save_stage(
            db,
            task,
            "raw_inventory",
            {"sequences": [item["sequence"] for item in loaded_meta], "count": len(loaded_meta)},
        )
        _raise_if_cancelled(db, task)
        if crash_after_stage == "raw_inventory":
            raise SpectrumCrash("simulated crash after raw inventory")

        store = get_object_store()
        for item in loaded_meta:
            expected = expected_by_seq[item["sequence"]]
            if item["sha256"] != expected["sha256"]:
                return _fail(db, task, "frozen_metadata_mismatch", f"chunk {item['sequence']} metadata differs", fatal=True)
        settings = get_settings()
        os.makedirs(settings.spool_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.spool_dir, prefix="analysis-") as scratch_name:
            raw_dir = os.path.join(scratch_name, "raw")
            segment_dir = os.path.join(scratch_name, "segments")
            os.makedirs(raw_dir, exist_ok=True)
            bytes_loaded = 0

            def fetch_raw(item: dict):
                expected = expected_by_seq[int(item["sequence"])]
                destination = Path(raw_dir) / f"{int(item['sequence']):09d}.bin"
                store.get_to_path(item["object_key"], destination)
                digest = hashlib.sha256()
                with destination.open("rb") as source:
                    for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != expected["sha256"]:
                    raise ValueError(f"object digest mismatch at sequence {item['sequence']}")
                return str(destination)

            segments = group_chunks_by_rate(loaded_meta, work_dir=segment_dir, fetch_raw=fetch_raw)
            bytes_loaded = sum(int(item["byte_length"]) for item in loaded_meta)
            save_stage(db, task, "raw_bytes", {"bytes_loaded": bytes_loaded})
            if crash_after_stage == "raw_bytes":
                raise SpectrumCrash("simulated crash after raw bytes")

            save_stage(
                db,
                task,
                "segmentation",
                {
                    "segments": [
                        {
                            "sample_rate": segment["sample_rate"],
                            "samples": segment["data"].shape[0],
                            "sequences": [segment["start_sequence"], segment["end_sequence"]],
                        }
                        for segment in segments
                    ]
                },
            )
            if crash_after_stage == "segmentation":
                raise SpectrumCrash("simulated crash after segmentation")

            result = analyze_all_segments(
                segments,
                snapshot["calibration_coefficients"],
                params,
                sample_rate_changed=len({segment["sample_rate"] for segment in segments}) > 1,
            )
            save_stage(
                db,
                task,
                "spectrum",
                {
                    "quality_status": result["quality_status"],
                    "segments": len(result["segments"]),
                    "quality_codes": sorted({item["code"] for item in result["quality"]}),
                },
            )
            if crash_after_stage == "spectrum":
                raise SpectrumCrash("simulated crash before publication")

            result["fixed_snapshot"] = snapshot
            result["snapshot_digest"] = snapshot_digest(snapshot)
            published = publish_report(db, task, manifest, calibration, result)

            for segment in segments:
                segment["data"]._mmap.close()

        if result.get("quality_status") == "error":
            task.status = "failed"
            task.error_code = "quality_error"
            task.error_message = "one or more analysis quality checks failed; diagnostic report is not published as complete"
        else:
            task.status = "succeeded"
            task.error_code = None
            task.error_message = None
        task.ended_at = utcnow()
        task.lease_owner = None
        task.lease_until = None
        db.commit()
        return {"status": task.status, "published": published and result.get("quality_status") != "error"}
    except TaskCancelled as exc:
        return _cancel(db, db.get(AnalysisTask, task_id), str(exc))
    except SpectrumCrash as exc:
        return _fail(db, db.get(AnalysisTask, task_id), "spectrum_crash", str(exc), fatal=False)
    except Exception as exc:  # diagnostics are mandatory for unexpected stage failures
        return _fail(db, db.get(AnalysisTask, task_id), type(exc).__name__, str(exc), fatal=False)
    finally:
        db.close()


def publish_report(db: Session, task: AnalysisTask, manifest: Manifest, calibration, result: dict) -> bool:
    # The unique constraint on task_id and this existence check make duplicate
    # publication impossible even if a worker retries a commit timeout.
    existing = db.scalar(select(Report).where(Report.task_id == task.id).limit(1))
    if existing:
        task.status = "succeeded" if existing.status == "published" else "failed"
        return False
    report = Report(
        task_id=task.id,
        manifest_id=manifest.id,
        calibration_version_id=calibration.id,
        status="published" if result.get("quality_status") != "error" else "diagnostic_failed",
        result=result,
        snapshot_digest=result["snapshot_digest"],
        published_at=utcnow() if result.get("quality_status") != "error" else None,
        review_reason="analysis quality status is error; diagnostic only, not a normal completed report"
        if result.get("quality_status") == "error"
        else None,
    )
    db.add(report)
    db.flush()
    return True


def _fail(db: Session, task: AnalysisTask | None, code: str, message: str, *, fatal: bool) -> dict:
    if task is None:
        return {"status": "missing", "published": False}
    task.status = "failed" if fatal else "retry_wait"
    task.error_code = code
    task.error_message = message
    task.ended_at = utcnow() if fatal else task.ended_at
    task.lease_owner = None
    task.lease_until = None
    db.commit()
    return {"status": task.status, "published": False, "error_code": code}


def _cancel(db: Session, task: AnalysisTask | None, message: str) -> dict:
    if task is None:
        return {"status": "missing", "published": False}
    task.status = "cancelled"
    task.error_code = "cancelled"
    task.error_message = message
    task.ended_at = utcnow()
    task.lease_owner = None
    task.lease_until = None
    db.commit()
    return {"status": "cancelled", "published": False}
