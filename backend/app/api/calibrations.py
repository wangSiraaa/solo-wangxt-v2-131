from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.orm import CalibrationSet
from app.schemas.api import CalibrationCreate, CalibrationOut
from app.services.calibration import create_calibration, mark_reports_for_review

router = APIRouter(prefix="/calibrations", tags=["calibrations"])


@router.post("", response_model=CalibrationOut, status_code=201)
def create(payload: CalibrationCreate, db: Session = Depends(get_db)):
    try:
        cal = create_calibration(db, payload)
        # If this activation superseded prior version(s), reports made with old coefficients are
        # only flagged for human review; their stored JSON and metrics remain immutable.
        old = db.scalars(
            select(CalibrationSet).where(
                CalibrationSet.name == cal.name, CalibrationSet.id != cal.id
            )
        ).all()
        count = 0
        for item in old:
            count += mark_reports_for_review(
                db,
                item.id,
                f"Calibration '{cal.name}' version {cal.version} activated after publication",
            )
        db.commit()
        return cal
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=list[CalibrationOut])
def list_all(db: Session = Depends(get_db)):
    return db.scalars(select(CalibrationSet).order_by(CalibrationSet.id.desc())).all()


@router.get("/{calibration_id}", response_model=CalibrationOut)
def get_one(calibration_id: int, db: Session = Depends(get_db)):
    cal = db.get(CalibrationSet, calibration_id)
    if cal is None:
        raise HTTPException(404, "calibration not found")
    return cal
