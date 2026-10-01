from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.enums import ManifestStatus, TaskState
from app.models.orm import AnalysisTask, Report, UploadManifest, utcnow
from app.schemas.api import AnalysisCreate
from app.services.calibration import calibration_as_dict
from app.services.signals import QualityEvent


class TaskConflict(RuntimeError):
    pass


class LeaseUnavailable(RuntimeError):
    pass


def _manifest_snapshot(manifest: UploadManifest) -> dict[str, Any]:
    return {
        "manifest_id": manifest.id,
        "manifest_key": manifest.manifest_key,
        "raw_manifest_digest": manifest.raw_manifest_digest,
        "nominal_frequency": manifest.nominal_frequency,
        "channels": manifest.channels,
        "channel_order": manifest.channel_order,
        "validation": manifest.validation,
        "blocks": [
            {
                "block_index": b.block_index,
                "object_key": b.object_key,
                "sha256": b.sha256,
                "byte_size": b.byte_size,
                "dtype": b.dtype,
                "start_sample": b.start_sample,
                "sample_count": b.sample_count,
                "sample_rate": b.sample_rate,
                "start_timestamp": b.start_timestamp.isoformat(),
            }
            for b in sorted(manifest.blocks, key=lambda b: b.block_index)
        ],
    }


def request_digest(manifest: UploadManifest, calibration_id: int, parameters: dict[str, Any]) -> str:
    payload = {
        "raw_manifest_digest": manifest.raw_manifest_digest,
        "calibration_id": calibration_id,
        "parameters": parameters,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def create_analysis(
    db: Session, payload: AnalysisCreate, calibration, enqueue: bool = True
) -> tuple[AnalysisTask, bool]:
    manifest = db.scalar(
        select(UploadManifest).where(UploadManifest.manifest_key == payload.manifest_key)
    )
    if manifest is None:
        raise KeyError(payload.manifest_key)
    if manifest.status == ManifestStatus.validation_failed:
        raise ValueError("manifest failed upload validation and cannot produce a normal report")
    if manifest.status == ManifestStatus.created:
        raise ValueError("manifest upload has not completed verification")
    if calibration is None:
        raise ValueError("calibration is required")

    parameters = payload.model_dump(exclude={"manifest_key", "calibration_id"})
    digest = request_digest(manifest, calibration.id, parameters)
    existing = db.scalar(select(AnalysisTask).where(AnalysisTask.request_digest == digest))
    if existing is not None:
        return existing, True

    task_key = uuid.uuid4().hex
    task = AnalysisTask(
        task_key=task_key,
        manifest_id=manifest.id,
        calibration_id=calibration.id,
        state=TaskState.pending,
        request_digest=digest,
        parameters=parameters,
        # Immutable inputs are fixed before execution. Later calibration activation updates the
        # calibration table, but cannot alter these JSON snapshots or coefficients mid-result.
        manifest_snapshot=_manifest_snapshot(manifest),
        calibration_snapshot=calibration_as_dict(calibration),
        max_attempts=3,
    )
    try:
        db.add(task)
        db.flush()
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(AnalysisTask).where(AnalysisTask.request_digest == digest))
        if existing is not None:
            return existing, True
        raise
    db.refresh(task)
    if enqueue and os.environ.get("PYTEST_CURRENT_TEST") is None:
        from app.worker import run_analysis_task

        run_analysis_task.apply_async(args=[task.task_key], task_id=f"analysis-{task.task_key}")
    return task, False


def claim_task(db: Session, task_key: str, lease_seconds: int = 120) -> AnalysisTask:
    task = db.scalar(select(AnalysisTask).where(AnalysisTask.task_key == task_key).with_for_update())
    if task is None:
        raise KeyError(task_key)
    now = utcnow()
    if task.state == TaskState.pending:
        pass
    elif task.state in {TaskState.running, TaskState.cancel_requested}:
        if task.leased_until and task.leased_until > now:
            raise LeaseUnavailable(f"task {task_key} is actively leased by {task.lease_owner}")
        if task.attempts >= task.max_attempts:
            raise LeaseUnavailable(f"task {task_key} exhausted {task.max_attempts} attempts")
    elif task.state in {TaskState.published, TaskState.canceled, TaskState.failed}:
        raise LeaseUnavailable(f"task {task_key} is terminal: {task.state.value}")
    else:
        raise LeaseUnavailable(str(task.state))

    task.attempts += 1
    task.fence_token += 1
    task.state = TaskState.running
    task.stage = "claimed"
    task.error = None
    task.lease_owner = f"{uuid.getnode()}-{uuid.uuid4().hex[:8]}"
    task.heartbeat_at = now
    task.leased_until = now + timedelta(seconds=lease_seconds)
    db.commit()
    db.refresh(task)
    return task


def refresh_lease(db: Session, task_id: int, fence_token: int) -> AnalysisTask | None:
    """Persist a heartbeat and return None when a newer owner or cancel request fenced this run."""
    task = db.get(AnalysisTask, task_id)
    if task is None or task.fence_token != fence_token or task.state != TaskState.running:
        return None
    now = utcnow()
    task.heartbeat_at = now
    task.leased_until = now + timedelta(seconds=120)
    db.commit()
    return task


def save_stage(
    db: Session,
    task_id: int,
    fence_token: int,
    stage: str,
    result: Any = None,
    quality_flags: list[dict[str, Any]] | None = None,
    stage_error: str | None = None,
) -> AnalysisTask | None:
    task = db.get(AnalysisTask, task_id)
    if task is None or task.fence_token != fence_token or task.state != TaskState.running:
        return None
    task.stage = stage
    if result is not None:
        task.stage_results = {**(task.stage_results or {}), stage: result}
    if quality_flags:
        task.quality_flags = [*(task.quality_flags or []), *quality_flags]
    if stage_error:
        task.stage_errors = {**(task.stage_errors or {}), stage: stage_error}
    db.commit()
    return task


def fail_task(db: Session, task_id: int, fence_token: int, stage: str, message: str) -> bool:
    task = db.get(AnalysisTask, task_id)
    if task is None or task.fence_token != fence_token:
        return False
    if task.state in {TaskState.published, TaskState.canceled}:
        return False
    task.state = TaskState.failed
    task.stage = stage
    task.error = message
    task.leased_until = None
    db.commit()
    return True


def request_cancel(db: Session, task_key: str) -> AnalysisTask:
    task = db.scalar(select(AnalysisTask).where(AnalysisTask.task_key == task_key).with_for_update())
    if task is None:
        raise KeyError(task_key)
    if task.state == TaskState.pending:
        task.state = TaskState.canceled
        task.cancel_requested_at = utcnow()
        task.leased_until = None
    elif task.state in {TaskState.running, TaskState.cancel_requested}:
        # The cancel request keeps the fence token; live worker cooperative cancellation is
        # idempotent. Lease expiry after a hard crash, however, must invalidate the old worker.
        task.state = TaskState.cancel_requested
        task.cancel_requested_at = utcnow()
    db.commit()
    db.refresh(task)
    return task


def retry_task(db: Session, task_key: str) -> AnalysisTask:
    task = db.scalar(select(AnalysisTask).where(AnalysisTask.task_key == task_key).with_for_update())
    if task is None:
        raise KeyError(task_key)
    if task.state not in {TaskState.failed, TaskState.canceled}:
        raise TaskConflict("only failed or canceled tasks can be retried as a new attempt")
    # Retrying an already exhausted task is an explicit operator action.
    task.attempts = 0
    task.state = TaskState.pending
    task.stage = None
    task.error = None
    task.fence_token += 1
    task.leased_until = None
    db.commit()
    db.refresh(task)
    if os.environ.get("PYTEST_CURRENT_TEST") is None:
        from app.worker import run_analysis_task

        run_analysis_task.apply_async(args=[task.task_key], task_id=f"analysis-retry-{task.task_key}-{task.fence_token}")
    return task


def publish_report(
    db: Session,
    task_id: int,
    fence_token: int,
    *,
    status: str,
    convention: dict[str, Any],
    metrics: dict[str, Any],
    charts: dict[str, Any],
    quality_flags: list[dict[str, Any]],
) -> Report | None:
    """Conditional one-row publication. A duplicate/recovered worker cannot publish twice."""
    task = db.scalar(
        select(AnalysisTask).where(
            AnalysisTask.id == task_id, AnalysisTask.fence_token == fence_token
        ).with_for_update()
    )
    if task is None or task.state != TaskState.running:
        return None
    if task.report is not None:
        return task.report

    report = Report(
        report_key=uuid.uuid4().hex,
        task_id=task.id,
        status=status,
        convention=convention,
        metrics=metrics,
        charts=charts,
        quality_flags=quality_flags,
        published_at=utcnow(),
    )
    db.add(report)
    task.state = TaskState.published
    task.stage = "published"
    task.published_at = utcnow()
    task.leased_until = None
    db.commit()
    db.refresh(report)
    return report


def cancel_if_requested(db: Session, task_id: int, fence_token: int) -> bool:
    task = db.get(AnalysisTask, task_id)
    if task and task.state == TaskState.cancel_requested and task.fence_token == fence_token:
        task.state = TaskState.canceled
        task.leased_until = None
        db.commit()
        return True
    return False


def requeue_stale_tasks(db: Session) -> list[str]:
    """Recover running leases whose worker disappeared without finalizing a report."""
    now = utcnow()
    query = select(AnalysisTask).where(AnalysisTask.state.in_({TaskState.running, TaskState.cancel_requested}))
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    stale = db.scalars(query).all()
    keys: list[str] = []
    for task in stale:
        if task.leased_until and task.leased_until > now:
            continue
        task.state = TaskState.pending
        task.stage = (task.stage or "") + ":lease_recovered"
        task.lease_owner = None
        task.heartbeat_at = None
        task.leased_until = None
        task.fence_token += 1
        keys.append(task.task_key)
    db.commit()
    return keys


def quality_event(code: str, severity: str, message: str, channel: str | None = None, **detail) -> dict[str, Any]:
    return QualityEvent(code, severity, message, channel, detail).as_dict()
