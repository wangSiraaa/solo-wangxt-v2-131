from __future__ import annotations

from celery import Celery

from .config import get_settings

settings = get_settings()
celery_app = Celery("power_quality", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.task_default_queue = "pq-analysis"
celery_app.conf.task_acks_late = True
celery_app.conf.worker_prefetch_multiplier = 1
celery_app.conf.task_track_started = True
celery_app.conf.broker_transport_options = {"visibility_timeout": settings.task_lock_seconds * 2}


@celery_app.task(name="run_analysis", bind=True, max_retries=None)
def run_analysis(self, task_id: str) -> dict:
    from .pipeline import execute_task

    return execute_task(task_id, lease_seconds=get_settings().task_lock_seconds)


@celery_app.task(name="run_analysis_crash_at")
def run_analysis_crash_at(task_id: str, stage: str) -> dict:
    from .pipeline import execute_task

    return execute_task(task_id, crash_after_stage=stage)
