from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.models.enums import (
    BlockStatus,
    CalibrationStatus,
    ManifestStatus,
    QualitySeverity,
    ReportStatus,
    ReviewState,
    TaskState,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UploadManifest(Base):
    __tablename__ = "upload_manifests"

    id: Mapped[int] = mapped_column(primary_key=True)
    manifest_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    nominal_frequency: Mapped[float] = mapped_column(Float, default=50.0)
    expected_blocks: Mapped[int] = mapped_column(Integer)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    channel_order: Mapped[list] = mapped_column(JSON, default=list)
    expected_sample_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_sample: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    end_sample: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    start_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    raw_manifest_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[ManifestStatus] = mapped_column(Enum(ManifestStatus), default=ManifestStatus.created)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    blocks: Mapped[list["DataBlock"]] = relationship(back_populates="manifest", cascade="all, delete-orphan")
    analyses: Mapped[list["AnalysisTask"]] = relationship(back_populates="manifest")


class DataBlock(Base):
    __tablename__ = "data_blocks"
    __table_args__ = (UniqueConstraint("manifest_id", "block_index", name="uq_block_manifest_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    manifest_id: Mapped[int] = mapped_column(ForeignKey("upload_manifests.id", ondelete="CASCADE"))
    block_index: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    byte_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    dtype: Mapped[str] = mapped_column(String(16), default="float32")
    start_sample: Mapped[int] = mapped_column(BigInteger)
    sample_count: Mapped[int] = mapped_column(BigInteger)
    sample_rate: Mapped[float] = mapped_column(Float)
    start_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    status: Mapped[BlockStatus] = mapped_column(Enum(BlockStatus), default=BlockStatus.stored)
    stored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    manifest: Mapped[UploadManifest] = relationship(back_populates="blocks")


class CalibrationSet(Base):
    __tablename__ = "calibration_sets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)
    gain: Mapped[float] = mapped_column(Float, default=1.0)
    offset: Mapped[float] = mapped_column(Float, default=0.0)
    phase_shift_rad: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[CalibrationStatus] = mapped_column(Enum(CalibrationStatus), default=CalibrationStatus.draft)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    channels: Mapped[list["CalibrationChannel"]] = relationship(
        back_populates="calibration", cascade="all, delete-orphan"
    )


class CalibrationChannel(Base):
    __tablename__ = "calibration_channels"
    __table_args__ = (UniqueConstraint("calibration_id", "channel", name="uq_cal_channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    calibration_id: Mapped[int] = mapped_column(ForeignKey("calibration_sets.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(128))
    gain: Mapped[float] = mapped_column(Float, default=1.0)
    offset: Mapped[float] = mapped_column(Float, default=0.0)
    phase_shift_rad: Mapped[float] = mapped_column(Float, default=0.0)

    calibration: Mapped[CalibrationSet] = relationship(back_populates="channels")


class AnalysisTask(Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (UniqueConstraint("manifest_id", "request_digest", name="uq_analysis_request"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    manifest_id: Mapped[int] = mapped_column(ForeignKey("upload_manifests.id"))
    calibration_id: Mapped[int] = mapped_column(ForeignKey("calibration_sets.id"))
    state: Mapped[TaskState] = mapped_column(Enum(TaskState), default=TaskState.pending, index=True)
    request_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    manifest_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    calibration_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_results: Mapped[dict] = mapped_column(JSON, default=dict)
    stage_errors: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_flags: Mapped[list] = mapped_column(JSON, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    fence_token: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    manifest: Mapped[UploadManifest] = relationship(back_populates="analyses")
    report: Mapped["Report"] = relationship(back_populates="task", uselist=False, cascade="all, delete-orphan")


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("task_id", name="uq_report_task"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    report_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("analysis_tasks.id", ondelete="CASCADE"))
    status: Mapped[ReportStatus] = mapped_column(Enum(ReportStatus), default=ReportStatus.draft)
    review_state: Mapped[ReviewState] = mapped_column(Enum(ReviewState), default=ReviewState.current)
    convention: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    charts: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_flags: Mapped[list] = mapped_column(JSON, default=list)
    superseded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped[AnalysisTask] = relationship(back_populates="report")


class QualityFlag(Base):
    __tablename__ = "quality_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_tasks.id", ondelete="CASCADE"), nullable=True)
    manifest_id: Mapped[int | None] = mapped_column(ForeignKey("upload_manifests.id", ondelete="CASCADE"), nullable=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[QualitySeverity] = mapped_column(Enum(QualitySeverity))
    channel: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
