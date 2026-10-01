from datetime import timedelta

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import AnalysisTask, Report, utcnow
from app.pipeline import execute_task
from tests.synthetic import CHANNELS, create_and_run_task, create_manifest, make_calibration, make_chunks, upload_chunks


def completed_recording(client):
    chunks = make_chunks(sample_chunks=(300, 420))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks, order=[1, 0])
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    return manifest, calibration


def test_task_fixes_manifest_calibration_and_params_and_publishes_once(client: TestClient):
    manifest, calibration = completed_recording(client)
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    second_run = execute_task(task_id)
    assert second_run["published"] is False
    with SessionLocal() as db:
        assert db.query(Report).filter(Report.task_id == task_id).count() == 1


def test_spectrum_crash_preserves_stage_results_but_publishes_no_report(client: TestClient):
    manifest, calibration = completed_recording(client)
    response = client.post(
        "/analysis-tasks",
        json={"manifest_id": manifest["id"], "calibration_version_id": calibration["id"]},
    )
    task_id = response.json()["id"]
    outcome = execute_task(task_id, crash_after_stage="spectrum")
    assert outcome["status"] == "retry_wait"
    assert outcome["published"] is False
    with SessionLocal() as db:
        task = db.get(AnalysisTask, task_id)
        assert "spectrum" in task.stage_results
        assert task.error_code == "spectrum_crash"
        assert db.query(Report).filter(Report.task_id == task_id).count() == 0


def test_worker_restart_lease_then_retry_does_not_duplicate_report(client: TestClient):
    manifest, calibration = completed_recording(client)
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    with SessionLocal() as db:
        task = db.get(AnalysisTask, task_id)
        task.status = "running"
        task.lease_owner = "dead-worker"
        task.lease_until = utcnow() - timedelta(seconds=1)
        task.attempts = 1
        db.commit()
    recovered = client.post("/maintenance/recover-stale-tasks").json()
    assert recovered["recovered"] == 1
    execute_task(task_id)
    with SessionLocal() as db:
        task = db.get(AnalysisTask, task_id)
        assert task.attempts == 2
        assert db.query(Report).filter(Report.task_id == task_id).count() == 1


def test_cancel_before_run_marks_task_cancelled(client: TestClient):
    manifest, calibration = completed_recording(client)
    response = client.post(
        "/analysis-tasks",
        json={"manifest_id": manifest["id"], "calibration_version_id": calibration["id"]},
    )
    task_id = response.json()["id"]
    cancelled = client.post(f"/analysis-tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    outcome = execute_task(task_id)
    assert outcome["published"] is False


def test_calibration_update_marks_old_report_needs_review_without_mixing_coefficients(client: TestClient):
    manifest, calibration = completed_recording(client)
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    old_report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    assert old_report["calibration_version_id"] == calibration["id"]

    updated = client.post(
        "/calibrations",
        json={
            "channel_set_hash": manifest["channel_set_hash"],
            "coefficients": {
                channel: {"gain": 1.05 if channel == "Va" else 1.0, "offset": 0, "phase_shift_rad": 0}
                for channel in CHANNELS
            },
            "change_note": "revised gain",
        },
    ).json()
    old_report = client.get(f"/reports/{old_report['id']}").json()
    assert old_report["status"] == "needs_review"
    assert old_report["superseded_by_calibration_id"] == updated["id"]
    # Snapshot inside old report remains immutable and still names old version.
    assert old_report["result"]["fixed_snapshot"]["calibration_version_id"] == calibration["id"]


def test_non_integer_cycle_is_warning_but_report_completes(client: TestClient):
    chunks = make_chunks(sample_chunks=(600,))
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    task = client.get(f"/analysis-tasks/{task_id}").json()
    report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    assert task["status"] == "succeeded"
    assert report["status"] == "published"
    assert any(item["code"] == "non_integer_cycle" for item in report["result"]["quality"])
    assert report["result"]["quality_status"] == "warning"


def test_saturation_is_quality_state(client: TestClient):
    chunks = make_chunks(sample_chunks=(720,), saturation=True)
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    coefficients = {
        channel: {
            "gain": 1,
            "offset": 0,
            "phase_shift_rad": 0,
            "saturation_low": -400 if channel == "Va" else None,
            "saturation_high": 400 if channel == "Va" else None,
        }
        for channel in CHANNELS
    }
    calibration = client.post(
        "/calibrations",
        json={"channel_set_hash": manifest["channel_set_hash"], "coefficients": coefficients},
    ).json()
    response = client.post(
        "/analysis-tasks",
        json={
            "manifest_id": manifest["id"],
            "calibration_version_id": calibration["id"],
            "params": {"saturation_warning_fraction": 1e-9},
        },
    )
    task_id = response.json()["id"]
    outcome = client.post(f"/analysis-tasks/{task_id}/run").json()
    assert outcome["published"] is True
    task = client.get(f"/analysis-tasks/{task_id}").json()
    assert task["status"] == "succeeded"
    report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    assert any(item["code"] == "saturation" for item in report["result"]["quality"])
    assert report["result"]["quality_status"] == "warning"


def test_missing_phase_creates_diagnostic_failed_report_not_published(client: TestClient):
    channels = ["Va", "Vb"]
    chunks = make_chunks(sample_chunks=(720,), channels=channels)
    manifest = create_manifest(client, chunks)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    incomplete_coefficients = {
        "Va": {"gain": 1, "offset": 0, "phase_shift_rad": 0},
        "Vb": {"gain": 1, "offset": 0, "phase_shift_rad": 0},
    }
    calibration = client.post(
        "/calibrations",
        json={"channel_set_hash": manifest["channel_set_hash"], "coefficients": incomplete_coefficients},
    ).json()
    response = client.post(
        "/analysis-tasks",
        json={"manifest_id": manifest["id"], "calibration_version_id": calibration["id"]},
    )
    task_id = response.json()["id"]
    outcome = client.post(f"/analysis-tasks/{task_id}/run").json()
    assert outcome["published"] is False
    task = client.get(f"/analysis-tasks/{task_id}").json()
    assert task["status"] == "failed"
    report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    assert report["status"] == "diagnostic_failed"
