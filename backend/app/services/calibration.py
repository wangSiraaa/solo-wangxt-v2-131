from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import CalibrationStatus
from app.models.orm import AnalysisTask, CalibrationChannel, CalibrationSet, Report, ReviewState, utcnow
from app.schemas.api import CalibrationCreate


def create_calibration(db: Session, payload: CalibrationCreate) -> CalibrationSet:
    if not payload.channels:
        raise ValueError("calibration must define at least one channel")
    channels = [c.channel for c in payload.channels]
    if len(channels) != len(set(channels)):
        raise ValueError("calibration channel entries must be unique")

    latest = db.scalar(
        select(CalibrationSet)
        .where(CalibrationSet.name == payload.name)
        .order_by(CalibrationSet.version.desc())
        .limit(1)
    )
    version = (latest.version + 1) if latest else 1
    cal = CalibrationSet(
        name=payload.name,
        # Kept for SQL-friendly reporting; the normalized source of truth is channels[].
        channel="__set__",
        version=version,
        status=CalibrationStatus.draft,
        change_note=payload.change_note,
    )
    db.add(cal)
    db.flush()
    for item in payload.channels:
        db.add(
            CalibrationChannel(
                calibration_id=cal.id,
                channel=item.channel,
                gain=item.gain,
                offset=item.offset,
                phase_shift_rad=item.phase_shift_rad,
            )
        )
    if payload.activate:
        activate_calibration(db, cal)
    db.commit()
    db.refresh(cal)
    return cal


def activate_calibration(db: Session, calibration: CalibrationSet) -> None:
    """One active version per calibration name. Activation never mutates old task snapshots."""
    old_versions = db.scalars(
        select(CalibrationSet).where(
            CalibrationSet.name == calibration.name,
            CalibrationSet.status == CalibrationStatus.active,
            CalibrationSet.id != calibration.id,
        )
    ).all()
    for old in old_versions:
        old.status = CalibrationStatus.superseded
    calibration.status = CalibrationStatus.active
    calibration.activated_at = utcnow()
    db.flush()


def calibration_as_dict(cal: CalibrationSet) -> dict:
    return {
        "id": cal.id,
        "name": cal.name,
        "version": cal.version,
        "status": cal.status.value,
        "channels": {
            c.channel: {"gain": c.gain, "offset": c.offset, "phase_shift_rad": c.phase_shift_rad}
            for c in sorted(cal.channels, key=lambda x: x.channel)
        },
    }


def get_active_calibration(db: Session) -> CalibrationSet | None:
    return db.scalar(select(CalibrationSet).where(CalibrationSet.status == CalibrationStatus.active).order_by(CalibrationSet.id.desc()))


def mark_reports_for_review(db: Session, superseded_calibration_id: int, reason: str) -> int:
    reports = db.scalars(
        select(Report)
        .join(AnalysisTask, AnalysisTask.id == Report.task_id)
        .where(
            Report.review_state == ReviewState.current,
            AnalysisTask.calibration_id == superseded_calibration_id,
        )
    ).all()
    for report in reports:
        report.review_state = ReviewState.review_required
        report.superseded_reason = reason
    db.flush()
    return len(reports)
