from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ChannelSpec(BaseModel):
    name: str
    kind: Literal["voltage", "current", "other"] = "other"
    phase: str | None = None
    unit: str | None = None
    missing: bool = False


class BlockManifestItem(BaseModel):
    block_index: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(gt=0)
    start_sample: int = Field(ge=0)
    sample_count: int = Field(gt=0)
    sample_rate: float = Field(gt=0)
    start_timestamp: datetime
    dtype: Literal["float32", "int16"] = "float32"

    @field_validator("sha256")
    @classmethod
    def hex_digest(cls, value: str) -> str:
        int(value, 16)
        return value.lower()


class ManifestCreate(BaseModel):
    manifest_key: str = Field(min_length=1, max_length=128)
    name: str
    nominal_frequency: float = 50.0
    expected_sample_rate: float | None = Field(default=None, gt=0)
    channels: list[ChannelSpec]
    blocks: list[BlockManifestItem]

    @field_validator("channels")
    @classmethod
    def unique_channels(cls, value: list[ChannelSpec]) -> list[ChannelSpec]:
        names = [c.name for c in value]
        if len(names) != len(set(names)):
            raise ValueError("channel names must be unique")
        return value


class ManifestOut(BaseModel):
    id: int
    manifest_key: str
    name: str
    status: str
    nominal_frequency: float
    expected_blocks: int
    expected_sample_rate: float | None
    channels: list[dict[str, Any]]
    validation: dict[str, Any]
    raw_manifest_digest: str | None
    completed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CompleteResult(BaseModel):
    manifest: ManifestOut
    already_completed: bool


class CalibrationChannelIn(BaseModel):
    channel: str
    gain: float = 1.0
    offset: float = 0.0
    phase_shift_rad: float = 0.0


class CalibrationCreate(BaseModel):
    name: str
    channels: list[CalibrationChannelIn]
    activate: bool = True
    change_note: str | None = None


class CalibrationOut(BaseModel):
    id: int
    name: str
    version: int
    status: str
    channels: list[dict[str, Any]]
    activated_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AnalysisCreate(BaseModel):
    manifest_key: str
    calibration_id: int | None = None
    harmonic_count: int = Field(default=25, ge=2, le=100)
    thd_denominator: Literal["rms", "fundamental"] = "rms"
    window: Literal["integer_cycle", "full"] = "integer_cycle"
    saturation_fraction: float = Field(default=0.98, gt=0, le=1)
    use_raw_phase: bool = False
    include_dc_in_rms: bool = False
    gap_max_fill: int = 0
    frequency_tolerance_hz: float = 0.5
    non_integer_cycle_tolerance: float = 0.05


class TaskOut(BaseModel):
    id: int
    task_key: str
    state: str
    stage: str | None
    error: str | None
    attempts: int
    parameters: dict[str, Any]
    manifest_snapshot: dict[str, Any]
    calibration_snapshot: dict[str, Any]
    stage_results: dict[str, Any]
    stage_errors: dict[str, Any]
    quality_flags: list[Any]
    published_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReportOut(BaseModel):
    id: int
    report_key: str
    status: str
    review_state: str
    convention: dict[str, Any]
    metrics: dict[str, Any]
    charts: dict[str, Any]
    quality_flags: list[Any]
    superseded_reason: str | None
    published_at: datetime | None

    model_config = {"from_attributes": True}
