import os

import numpy as np

from app.models.enums import ReviewState, TaskState
from app.schemas.api import AnalysisCreate, CalibrationCreate, ManifestCreate
from app.services import manifests as manifest_service
from app.services import tasks as task_service
from app.services.calibration import create_calibration
from app.worker import run_analysis_task
from tests.conftest import balanced_waveforms, build_manifest_payload


def _setup_manifest(db, blocks, sample_rate=1000.0, key="synthetic"):
    payload = ManifestCreate(**build_manifest_payload(blocks, sample_rate=sample_rate, key=key))
    manifest = manifest_service.create_manifest(db, payload)
    for i, block in enumerate(blocks):
        manifest_service.upload_block(db, key, i, np.asarray(block, dtype=np.float32).tobytes())
    manifest, _ = manifest_service.complete_manifest(db, key)
    return manifest


def test_end_to_end_known_harmonics_publishes_complete_report(isolated_app):
    SessionLocal, _ = isolated_app
    db = SessionLocal()
    blocks = [balanced_waveforms(1000, harmonic5=0.05), balanced_waveforms(1000, harmonic5=0.05)]
    _setup_manifest(db, blocks)
    cal = create_calibration(db, CalibrationCreate(name="default", channels=[
        {"channel": "Va", "gain": 1, "offset": 0, "phase_shift_rad": 0},
        {"channel": "Vb", "gain": 1, "offset": 0, "phase_shift_rad": 0},
        {"channel": "Vc", "gain": 1, "offset": 0, "phase_shift_rad": 0},
    ]))
    task, duplicate = task_service.create_analysis(
        db,
        AnalysisCreate(manifest_key="synthetic", calibration_id=cal.id, harmonic_count=25, thd_denominator="rms"),
        cal,
        enqueue=False,
    )
    assert duplicate is False
    same, duplicate = task_service.create_analysis(
        db,
        AnalysisCreate(manifest_key="synthetic", calibration_id=cal.id, harmonic_count=25, thd_denominator="rms"),
        cal,
        enqueue=False,
    )
    assert duplicate is True and same.id == task.id

    run_analysis_task(task.task_key)
    db.expire_all()
    report = db.get(type(task), task.id).report
    assert report.status.value == "complete"
    assert report.metrics["channels"]["Va"]["fundamental"]["rms"] == 1.0
    assert report.metrics["symmetrical_components"]["voltage"]["negative_unbalance_percent"] < 1e-8
    db.close()


def test_spectrum_crash_retry_publishes_exactly_one_report(isolated_app, monkeypatch):
    SessionLocal, _ = isolated_app
    db = SessionLocal()
    _setup_manifest(db, [balanced_waveforms(2000)], key="crash")
    cal = create_calibration(db, CalibrationCreate(name="default", channels=[
        {"channel": name, "gain": 1, "offset": 0, "phase_shift_rad": 0}
        for name in ["Va", "Vb", "Vc"]
    ]))
    task, _ = task_service.create_analysis(
        db, AnalysisCreate(manifest_key="crash", calibration_id=cal.id), cal, enqueue=False
    )
    key = task.task_key
    monkeypatch.setenv("PQ_CRASH_SPECTRUM_TASK_KEY", key)
    try:
        run_analysis_task(key)
    except RuntimeError:
        pass
    db.expire_all()
    db.rollback()
    task_row = db.get(type(task), task.id)
    assert task_row.state == TaskState.failed
    assert task_row.report is None
    assert "spectrum_crash_recorded" in task_row.stage_results

    # Simulate a duplicate Celery delivery while the row is failed: terminal lease is rejected.
    result = run_analysis_task(key)
    assert result["state"] == "lease_unavailable"

    task_service.retry_task(db, key)
    db.refresh(task_row)
    monkeypatch.delenv("PQ_CRASH_SPECTRUM_TASK_KEY", raising=False)
    run_analysis_task(key)
    db.expire_all()
    final = db.get(type(task), task.id)
    assert final.state == TaskState.published
    assert final.attempts == 2
    assert final.report is not None
    db.close()


def test_gap_sample_rate_and_reverse_wiring_keep_report_incomplete_or_flagged(isolated_app):
    SessionLocal, _ = isolated_app
    db = SessionLocal()
    normal = balanced_waveforms(800)
    changed_rate = balanced_waveforms(960, fs=1200)
    payload = build_manifest_payload([normal, changed_rate], sample_rate=[1000.0, 1200.0], key="defects")
    payload["blocks"][1]["start_sample"] = 900
    from datetime import datetime, timedelta, timezone
    payload["blocks"][1]["start_timestamp"] = (datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=900/1000)).isoformat()
    manifest = manifest_service.create_manifest(db, ManifestCreate(**payload))
    for i, arr in enumerate([normal, changed_rate]):
        manifest_service.upload_block(db, "defects", i, np.asarray(arr, dtype=np.float32).tobytes())
    manifest_service.complete_manifest(db, "defects")
    cal = create_calibration(db, CalibrationCreate(name="default", channels=[
        {"channel": name, "gain": 1, "offset": 0, "phase_shift_rad": 0}
        for name in ["Va", "Vb", "Vc"]
    ]))
    task, _ = task_service.create_analysis(db, AnalysisCreate(manifest_key="defects", calibration_id=cal.id), cal, enqueue=False)
    run_analysis_task(task.task_key)
    db.expire_all()
    report = db.get(type(task), task.id).report
    codes = {q["code"] for q in report.quality_flags}
    assert "missing_sample_interval" in codes
    assert "sample_rate_change" in codes
    assert report.status.value == "incomplete"
    db.close()


def test_reverse_polarity_has_sequence_unbalance(isolated_app):
    SessionLocal, _ = isolated_app
    db = SessionLocal()
    _setup_manifest(db, [balanced_waveforms(2000, reverse_c=True)], key="reverse")
    cal = create_calibration(db, CalibrationCreate(name="default", channels=[
        {"channel": name, "gain": 1, "offset": 0, "phase_shift_rad": 0}
        for name in ["Va", "Vb", "Vc"]
    ]))
    task, _ = task_service.create_analysis(db, AnalysisCreate(manifest_key="reverse", calibration_id=cal.id), cal, enqueue=False)
    run_analysis_task(task.task_key)
    db.expire_all()
    report = db.get(type(task), task.id).report
    seq = report.metrics["symmetrical_components"]["voltage"]
    assert seq["negative_unbalance_percent"] > 50
    db.close()


def test_calibration_revision_marks_old_report_review_required_without_mixing_coefficients(isolated_app):
    SessionLocal, _ = isolated_app
    db = SessionLocal()
    _setup_manifest(db, [balanced_waveforms(2000)], key="cal")
    cal1 = create_calibration(db, CalibrationCreate(name="default", channels=[
        {"channel": name, "gain": 1, "offset": 0, "phase_shift_rad": 0}
        for name in ["Va", "Vb", "Vc"]
    ], change_note="v1"))
    task, _ = task_service.create_analysis(db, AnalysisCreate(manifest_key="cal", calibration_id=cal1.id), cal1, enqueue=False)
    run_analysis_task(task.task_key)
    db.expire_all()
    report = db.get(type(task), task.id).report
    assert report.review_state == ReviewState.current

    cal2 = create_calibration(db, CalibrationCreate(name="default", channels=[
        {"channel": "Va", "gain": 2, "offset": 0, "phase_shift_rad": 0},
        {"channel": "Vb", "gain": 1, "offset": 0, "phase_shift_rad": 0},
        {"channel": "Vc", "gain": 1, "offset": 0, "phase_shift_rad": 0},
    ], change_note="v2"))
    db.refresh(report)
    assert report.review_state == ReviewState.review_required
    assert report.metrics["channels"]["Va"]["fundamental"]["rms"] == 1.0
    assert task.calibration_snapshot["channels"]["Va"]["gain"] == 1.0
    assert cal2.version == 2
    db.close()
