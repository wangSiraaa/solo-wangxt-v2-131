from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import numpy as np

CHANNELS = ["Va", "Vb", "Vc"]


def phase_waveforms(n: int, fs: float = 6000.0, fundamental: float = 50.0, reversed_a: bool = False, saturation: bool = False, channels=None):
    channels = channels or CHANNELS
    t = np.arange(n, dtype=np.float64) / fs
    va_amp = -np.sqrt(2.0) * 300.0 if reversed_a else np.sqrt(2.0) * 300.0
    source = {
        "Va": va_amp * np.cos(2 * np.pi * fundamental * t)
        + np.sqrt(2.0) * 10.0 * np.cos(2 * np.pi * 3 * fundamental * t)
        + np.sqrt(2.0) * 20.0 * np.cos(2 * np.pi * 5 * fundamental * t),
        "Vb": np.sqrt(2.0) * 300.0 * np.cos(2 * np.pi * fundamental * t - 2 * np.pi / 3),
        "Vc": np.sqrt(2.0) * 300.0 * np.cos(2 * np.pi * fundamental * t + 2 * np.pi / 3),
    }
    if saturation:
        source["Va"] = np.clip(source["Va"], -400, 400)
    values = np.column_stack([source[channel] for channel in channels]).astype("<f4")
    return values


def make_chunks(sample_chunks=(300, 420), fs: float = 6000.0, channels=None, **wave_kwargs):
    channels = channels or CHANNELS
    total = sum(sample_chunks)
    full = phase_waveforms(total, fs=fs, channels=channels, **wave_kwargs)
    raw_chunks = []
    offset_samples = 0
    byte_offset = 0
    start = datetime(2026, 10, 1, tzinfo=None)
    for sequence, count in enumerate(sample_chunks):
        values = full[offset_samples : offset_samples + count, :]
        raw = values.astype("<f4").tobytes()
        start_time = start + timedelta(seconds=offset_samples / fs)
        end_time = start + timedelta(seconds=(offset_samples + count - 1) / fs)
        raw_chunks.append(
            {
                "sequence": sequence,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "byte_offset": byte_offset,
                "byte_length": len(raw),
                "sample_count": count,
                "sample_rate": fs,
                "channels": channels,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "encoding": "float32le-interleaved",
                "raw": raw,
            }
        )
        offset_samples += count
        byte_offset += len(raw)
    return raw_chunks


def make_rate_change_chunks():
    first = make_chunks(sample_chunks=(300,), fs=6000.0)[0]
    second_values = phase_waveforms(420, fs=7000.0)
    raw = second_values.tobytes()
    start = datetime.fromisoformat(first["end_time"]) + timedelta(seconds=1 / 6000.0)
    second = {
        "sequence": 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "byte_offset": first["byte_offset"] + first["byte_length"],
        "byte_length": len(raw),
        "sample_count": 420,
        "sample_rate": 7000.0,
        "channels": CHANNELS,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(seconds=419 / 7000.0)).isoformat(),
        "encoding": "float32le-interleaved",
        "raw": raw,
    }
    return [first, second]


def create_manifest(client, chunks, name="synthetic", nominal_sample_rate=None):
    payload = {
        "name": name,
        "nominal_sample_rate": nominal_sample_rate,
        "expected_chunks": [{k: v for k, v in chunk.items() if k != "raw"} for chunk in chunks],
    }
    response = client.post("/manifests", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def upload_chunks(client, manifest_id, chunks, order=None):
    order = order or [chunk["sequence"] for chunk in chunks]
    for sequence in order:
        chunk = next(item for item in chunks if item["sequence"] == sequence)
        response = client.put(
            f"/manifests/{manifest_id}/chunks/{sequence}/raw",
            files={"chunk_file": (f"{sequence}.bin", chunk["raw"], "application/octet-stream")},
        )
        assert response.status_code == 200, response.text
    return chunks


def coefficients(gain_a=1.0):
    return {
        channel: {"gain": gain_a if channel == "Va" else 1.0, "offset": 0.0, "phase_shift_rad": 0.0}
        for channel in CHANNELS
    }


def make_calibration(client, channel_set_hash, gain_a=1.0, note="initial"):
    response = client.post(
        "/calibrations",
        json={"channel_set_hash": channel_set_hash, "coefficients": coefficients(gain_a), "change_note": note},
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_and_run_task(client, manifest_id, calibration_id):
    response = client.post(
        "/analysis-tasks",
        json={"manifest_id": manifest_id, "calibration_version_id": calibration_id},
    )
    assert response.status_code == 201, response.text
    task = response.json()
    response = client.post(f"/analysis-tasks/{task['id']}/run")
    assert response.status_code == 200, response.text
    return task["id"]
