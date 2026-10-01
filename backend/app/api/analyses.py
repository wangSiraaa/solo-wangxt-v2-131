from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.orm import AnalysisTask, CalibrationSet, Report
from app.schemas.api import AnalysisCreate, ReportOut, TaskOut
from app.services import tasks as task_service
from app.services.calibration import get_active_calibration

router = APIRouter(prefix="/analyses", tags=["analyses"])


def _load_calibration(db: Session, calibration_id: int | None) -> CalibrationSet:
    cal = db.get(CalibrationSet, calibration_id) if calibration_id is not None else get_active_calibration(db)
    if cal is None:
        raise HTTPException(400, "active calibration required; create/activate one or specify calibration_id")
    return cal


@router.post("", response_model=TaskOut, status_code=202)
def create_analysis(payload: AnalysisCreate, db: Session = Depends(get_db)):
    calibration = _load_calibration(db, payload.calibration_id)
    try:
        task, _ = task_service.create_analysis(db, payload, calibration, enqueue=True)
        return task
    except KeyError as exc:
        raise HTTPException(404, f"manifest not found: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("", response_model=list[TaskOut])
def list_tasks(limit: int = 50, db: Session = Depends(get_db)):
    return db.scalars(select(AnalysisTask).order_by(AnalysisTask.id.desc()).limit(limit)).all()


@router.get("/by-key/{task_key}", response_model=TaskOut)
def get_task_by_key(task_key: str, db: Session = Depends(get_db)):
    task = db.scalar(select(AnalysisTask).where(AnalysisTask.task_key == task_key))
    if task is None:
        raise HTTPException(404, "task not found")
    return task


@router.post("/by-key/{task_key}/cancel", response_model=TaskOut)
def cancel(task_key: str, db: Session = Depends(get_db)):
    try:
        return task_service.request_cancel(db, task_key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/by-key/{task_key}/retry", response_model=TaskOut)
def retry(task_key: str, db: Session = Depends(get_db)):
    try:
        return task_service.retry_task(db, task_key)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except task_service.TaskConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/by-key/{task_key}/report", response_model=ReportOut)
def report_by_task(task_key: str, db: Session = Depends(get_db)):
    task = db.scalar(select(AnalysisTask).where(AnalysisTask.task_key == task_key))
    if task is None:
        raise HTTPException(404, "task not found")
    if task.report is None:
        raise HTTPException(409, f"no report published; task state={task.state.value}")
    return task.report


@router.get("/reports/{report_key}", response_model=ReportOut)
def report(report_key: str, db: Session = Depends(get_db)):
    item = db.scalar(select(Report).where(Report.report_key == report_key))
    if item is None:
        raise HTTPException(404, "report not found")
    return item
