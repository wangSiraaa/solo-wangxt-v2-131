from datetime import datetime, timedelta, timezone

import numpy as np

from app.models.enums import ManifestStatus
from app.schemas.api import ManifestCreate
from app.services import manifests as service


def test_out_of_order_upload_completes_and_repeated_completion_is_idempotent(isolated_app):
    SessionLocal, _ = isolated_app
    a = np.zeros((100, 3), dtype=np.float32)
    b = np.ones((120, 3), dtype=np.float32)
    payload = ManifestCreate(**build_payload([a, b]))

    db = SessionLocal()
    manifest = service.create_manifest(db, payload)
    db.close()

    # Late network retransmission arrives after block 1.
    db = SessionLocal()
    service.upload_block(db, "m-1", 1, b.tobytes())
    service.upload_block(db, "m-1", 0, a.tobytes())
    stored_again = service.upload_block(db, "m-1", 0, a.tobytes())
    assert stored_again.status.value == "stored"

    m1, first = service.complete_manifest(db, "m-1")
    m2, second = service.complete_manifest(db, "m-1")
    assert first is False and second is True
    assert m1.id == m2.id
    assert m1.status == ManifestStatus.completed
    assert m1.raw_manifest_digest == m2.raw_manifest_digest
    db.close()


def test_overlap_is_validation_failure_not_sorted_away(isolated_app):
    SessionLocal, _ = isolated_app
    a = np.zeros((100, 3), dtype=np.float32)
    b = np.ones((100, 3), dtype=np.float32)
    payload = build_payload([a, b])
    payload["blocks"][1]["start_sample"] = 150
    payload["blocks"][1]["start_timestamp"] = (
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=150 / 1000)
    ).isoformat()
    db = SessionLocal()
    m = service.create_manifest(db, ManifestCreate(**payload))
    service.upload_block(db, m.manifest_key, 0, a.tobytes())
    service.upload_block(db, m.manifest_key, 1, b.tobytes())
    result, _ = service.complete_manifest(db, m.manifest_key)
    codes = [i["code"] for i in result.validation["issues"]]
    assert "overlapping_samples" in codes
    assert result.status == ManifestStatus.validation_failed
    db.close()


def test_missing_interval_and_sample_rate_change_are_explicit_defects(isolated_app):
    SessionLocal, _ = isolated_app
    a = np.zeros((100, 3), dtype=np.float32)
    b = np.ones((100, 3), dtype=np.float32)
    payload = build_payload([a, b], sample_rate=[1000.0, 1200.0])
    payload["blocks"][1]["start_sample"] = 250
    payload["blocks"][1]["start_timestamp"] = (
        datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=250 / 1000)
    ).isoformat()
    db = SessionLocal()
    m = service.create_manifest(db, ManifestCreate(**payload))
    service.upload_block(db, m.manifest_key, 0, a.tobytes())
    service.upload_block(db, m.manifest_key, 1, b.tobytes())
    result, _ = service.complete_manifest(db, m.manifest_key)
    codes = {i["code"]: i for i in result.validation["issues"]}
    assert result.status == ManifestStatus.completed_with_defects
    assert "missing_sample_interval" in codes
    assert codes["missing_sample_interval"]["detail"]["missing_samples"] == 50
    assert "sample_rate_change" in codes
    db.close()


def test_digest_and_channel_byte_size_are_enforced(isolated_app):
    SessionLocal, _ = isolated_app
    a = np.zeros((100, 3), dtype=np.float32)
    payload = ManifestCreate(**build_payload([a]))
    db = SessionLocal()
    service.create_manifest(db, payload)
    try:
        service.upload_block(db, "m-1", 0, np.zeros((99, 3), dtype=np.float32).tobytes())
        assert False
    except ValueError as exc:
        assert "sha256" in str(exc) or "byte length" in str(exc)
    db.close()


def build_payload(blocks, **kwargs):
    from tests.conftest import build_manifest_payload
    return build_manifest_payload(blocks, **kwargs)
