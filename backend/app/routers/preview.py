from __future__ import annotations

import math
import tempfile
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..dsp import calibrate_series, group_chunks_by_rate
from ..models import CalibrationVersion, Chunk, Manifest
from ..storage import get_object_store

router = APIRouter(prefix="/manifests", tags=["preview"])


def _decimate(values: np.ndarray, max_points: int) -> np.ndarray:
    if values.size <= max_points:
        return values
    # Min/max buckets preserve waveform extrema for visual review instead of
    # hiding short peaks through naive stride sampling.
    bucket = math.ceil(values.size / (max_points / 2))
    trimmed = values[: values.size - (values.size % bucket)]
    grouped = trimmed.reshape(-1, bucket)
    mins = grouped.min(axis=1)
    maxs = grouped.max(axis=1)
    return np.column_stack((mins, maxs)).reshape(-1)


@router.get("/{manifest_id}/preview")
def preview_manifest(
    manifest_id: str,
    calibration_version_id: str | None = None,
    max_points: int = Query(default=4000, ge=100, le=20000),
    db: Session = Depends(get_db),
):
    manifest = db.get(Manifest, manifest_id)
    if manifest is None:
        raise HTTPException(404, "manifest not found")
    chunks = db.scalars(select(Chunk).where(Chunk.manifest_id == manifest.id).order_by(Chunk.sequence)).all()
    if not chunks:
        return {"channels": manifest.channel_set, "series": [], "segments": []}
    metadata = [
        {
            "sequence": chunk.sequence,
            "sha256": chunk.sha256,
            "byte_offset": chunk.byte_offset,
            "byte_length": chunk.byte_length,
            "sample_count": chunk.sample_count,
            "sample_rate": chunk.sample_rate,
            "channels": chunk.channels,
            "start_time": chunk.start_time,
            "end_time": chunk.end_time,
            "encoding": chunk.encoding,
            "object_key": chunk.object_key,
        }
        for chunk in chunks
    ]
    store = get_object_store()
    settings = get_settings()
    Path(settings.spool_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.spool_dir, prefix="preview-") as scratch:
        raw_dir = Path(scratch) / "raw"

        def fetch_raw(item):
            target = raw_dir / f"{int(item['sequence']):09d}.bin"
            store.get_to_path(item["object_key"], target)
            return str(target)

        segments = group_chunks_by_rate(metadata, fetch_raw=fetch_raw)

        calibration = None
        if calibration_version_id:
            calibration = db.get(CalibrationVersion, calibration_version_id)
            if calibration is None:
                raise HTTPException(404, "calibration version not found")

        output_segments = []
        time_cursor = 0.0
        for segment in segments:
            data = segment["data"]
            fs = float(segment["sample_rate"])
            samples = int(data.shape[0])
            duration = samples / fs
            decimated = []
            for index, channel in enumerate(segment["channels"]):
                values = _decimate(np.asarray(data[:, index]), max_points)
                if calibration is not None:
                    coefficient = calibration.coefficients.get(channel)
                    if coefficient is None:
                        raise HTTPException(422, f"missing calibration coefficient for {channel}")
                    values = calibrate_series(values, coefficient)
                decimated.append(values.tolist())
            n = len(decimated[0]) if decimated else 0
            times = [(time_cursor + duration * index / max(1, n - 1)) for index in range(n)]
            output_segments.append(
                {
                    "start_seconds": time_cursor,
                    "end_seconds": time_cursor + duration,
                    "sample_rate": fs,
                    "samples": samples,
                    "sequences": [segment["start_sequence"], segment["end_sequence"]],
                    "channels": segment["channels"],
                    "times": times,
                    "series": [
                        {"channel": channel, "values": values}
                        for channel, values in zip(segment["channels"], decimated)
                    ],
                }
            )
            time_cursor += duration
            data._mmap.close()

    return {
        "channels": manifest.channel_set,
        "status": manifest.status,
        "segments": output_segments,
        "issues": [
            {
                "severity": issue.severity,
                "code": issue.code,
                "message": issue.message,
                "details": issue.details,
            }
            for issue in sorted(manifest.issues, key=lambda item: item.created_at)
        ],
    }
