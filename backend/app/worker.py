from __future__ import annotations

import os
import traceback
import uuid

from celery import Celery
from celery.signals import worker_ready

from app.core.config import get_settings
from app.db.session import SessionLocal, init_db
from app.models.orm import AnalysisTask, CalibrationSet
from app.services.analysis import analyze_task
from app.services import tasks as task_service

settings = get_settings()
celery_app = Celery(
    "pq",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
)


@worker_ready.connect
def recover_stale_on_worker_start(sender=None, **_kwargs):
    init_db()
    db = SessionLocal()
    try:
        keys = task_service.requeue_stale_tasks(db)
        for key in keys:
            run_analysis_task.apply_async(
                args=[key], task_id=f"analysis-recovered-{key}-{uuid.uuid4().hex[:8]}"
            )
    finally:
        db.close()


@celery_app.task(name="analysis.run", bind=True, max_retries=0, acks_late=True)
def run_analysis_task(self, task_key: str) -> dict | None:
    init_db()
    db = SessionLocal()
    task = None
    original_token = None
    try:
        task = task_service.claim_task(db, task_key)
        task_id, token = task.id, task.fence_token
        original_token = token

        task_service.save_stage(db, task_id, token, "input_snapshot", {
            "manifest_digest": task.manifest_snapshot.get("raw_manifest_digest"),
            "calibration_id": task.calibration_id,
            "calibration_version": task.calibration_snapshot.get("version"),
            "parameters": task.parameters,
        })
        if task_service.cancel_if_requested(db, task_id, token):
            return {"task_key": task_key, "state": "canceled"}

        # Stage model allows partial diagnostic data to survive a crash in later stages.
        task_service.save_stage(db, task_id, token, "load_blocks", {
            "block_count": len(task.manifest_snapshot.get("blocks", [])),
        })
        if task_service.cancel_if_requested(db, task_id, token):
            return {"task_key": task_key, "state": "canceled"}

        # The crash injection is deliberate for acceptance testing: it happens after immutable
        # inputs/leases are recorded and before publication, proving at-least-once delivery cannot
        # create duplicate reports.
        if os.environ.get("PQ_CRASH_SPECTRUM_TASK_KEY") == task_key and task.stage_results.get("spectrum_crash_recorded") is None:
            task_service.save_stage(db, task_id, token, "spectrum_crash_recorded", {"injected": True})
            raise RuntimeError("injected spectrum-task crash before publication")

        def heartbeat():
            alive = task_service.refresh_lease(db, task_id, token)
            if alive is None:
                raise RuntimeError("analysis worker was fenced by cancel or lease takeover")
            return alive

        payload = analyze_task(db.get(AnalysisTask, task_id), heartbeat=heartbeat)
        if task_service.cancel_if_requested(db, task_id, token):
            return {"task_key": task_key, "state": "canceled"}

        report = task_service.publish_report(
            db,
            task_id,
            token,
            status=payload["status"].value,
            convention=payload["convention"],
            metrics=payload["metrics"],
            charts=payload["charts"],
            quality_flags=payload["quality_flags"],
        )
        if report is None:
            return {"task_key": task_key, "state": "not_published_fenced"}
        return {"task_key": task_key, "report_key": report.report_key, "status": report.status}
    except task_service.LeaseUnavailable:
        return {"task_key": task_key, "state": "lease_unavailable"}
    except Exception as exc:
        if task is not None and db.is_active:
            task_service.fail_task(db, task.id, original_token, task.stage or "unknown", f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        raise
    finally:
        db.close()
