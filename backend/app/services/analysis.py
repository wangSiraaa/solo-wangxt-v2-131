from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.models.enums import ReportStatus
from app.models.orm import AnalysisTask
from app.services.signals import (
    QualityEvent,
    estimate_frequency,
    select_integer_window,
    symmetrical_components,
)
from app.services.storage import get_store

_DTYPE = {"float32": np.float32, "int16": np.int16}


@dataclass
class WaveformSegment:
    index: int
    sample_rate_hz: float
    start_sample_global: int
    start_timestamp: str
    sample_count: int
    source_blocks: list[int]
    matrices: list[np.memmap] = field(default_factory=list)
    is_gap: bool = False


def _quality(code: str, severity: str, message: str, channel: str | None = None, **detail) -> dict[str, Any]:
    return QualityEvent(code, severity, message, channel, detail).as_dict()


def _spool_block(row: dict[str, Any], workdir: Path) -> np.memmap:
    """Map raw immutable block; S3 blocks are spooled, local blocks remain zero-copy mmap."""
    store = get_store()
    local_path: Path
    if hasattr(store, "path"):
        local_path = store.path(row["object_key"])
    else:
        local_path = workdir / f"block-{row['block_index']}.raw"
        with store.spool(row["object_key"]) as downloaded:
            local_path.write_bytes(downloaded.read_bytes())
    raw = np.memmap(local_path, dtype=_DTYPE[row["dtype"]], mode="r")
    expected = row["sample_count"] * row["_channel_count"]
    if raw.size != expected:
        raise ValueError(f"block {row['block_index']} decoded {raw.size} values, expected {expected}")
    return raw.reshape(row["sample_count"], row["_channel_count"])


def build_segments(snapshot: dict[str, Any], workdir: Path) -> tuple[list[WaveformSegment], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build constant-rate groups. Missing samples become explicit NaN/gap segments."""
    blocks = sorted(snapshot["blocks"], key=lambda b: b["block_index"])
    channel_count = len(snapshot["channel_order"])
    for block in blocks:
        block["_channel_count"] = channel_count
    maps = {b["block_index"]: _spool_block(b, workdir) for b in blocks}

    segments: list[WaveformSegment] = []
    quality: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None

    def flush_group(gap_samples: int = 0) -> None:
        if not current:
            return
        first = current[0]
        count = sum(b["sample_count"] for b in current)
        segments.append(
            WaveformSegment(
                index=len(segments),
                sample_rate_hz=float(first["sample_rate"]),
                start_sample_global=first["start_sample"],
                start_timestamp=first["start_timestamp"],
                sample_count=count,
                source_blocks=[b["block_index"] for b in current],
                matrices=[maps[b["block_index"]] for b in current],
            )
        )
        if gap_samples:
            gap_start = current[-1]["start_sample"] + current[-1]["sample_count"]
            segments.append(
                WaveformSegment(
                    index=len(segments),
                    sample_rate_hz=float(current[-1]["sample_rate"]),
                    start_sample_global=gap_start,
                    start_timestamp="",
                    sample_count=gap_samples,
                    source_blocks=[],
                    is_gap=True,
                )
            )

    for block in blocks:
        if previous is not None:
            expected_sample = previous["start_sample"] + previous["sample_count"]
            delta = block["start_sample"] - expected_sample
            if delta < 0:
                quality.append(_quality("overlapping_samples", "fatal", "overlap reached analysis", block=str(block["block_index"])))
            elif delta > 0:
                gap = {
                    "start_sample": expected_sample,
                    "end_sample": block["start_sample"],
                    "sample_count": delta,
                    "sample_rate_hz": float(previous["sample_rate"]),
                    "after_block": previous["block_index"],
                    "before_block": block["block_index"],
                }
                gaps.append(gap)
                flush_group(delta)
                current = []
            if previous["sample_rate"] != block["sample_rate"]:
                # If no gap was found, flush current before starting a new physical-rate group.
                if current:
                    flush_group(0)
                    current = []
        current.append(block)
        previous = block
    flush_group(0)
    return segments, gaps, quality


def _iter_physical_raw(matrices: list[np.memmap], channel: int, limit: int | None = None):
    used = 0
    for matrix in matrices:
        view = matrix[:, channel]
        if limit is not None:
            remain = limit - used
            if remain <= 0:
                break
            view = view[:remain]
        yield view
        used += view.size


def _stream_channel(
    segment: WaveformSegment,
    channel_index: int,
    channel: str,
    *,
    nominal_frequency: float,
    harmonic_count: int,
    gain: float,
    offset: float,
    phase_shift_rad: float,
    use_raw_phase: bool,
    include_dc_in_rms: bool,
    thd_denominator: str,
    non_integer_tolerance: float,
    saturation_fraction: float,
    raw_dtype: str,
    heartbeat=None,
) -> dict[str, Any]:
    if heartbeat:
        heartbeat()
    quality: list[dict[str, Any]] = []
    scale_limit = 1.0 if raw_dtype == "float32" else float(np.iinfo(np.int16).max)
    saturated = 0
    total_n = segment.sample_count

    # Frequency and chart spectrum can use a bounded contiguous prefix; harmonic sums are full.
    estimate_limit = min(total_n, int(segment.sample_rate_hz * 2.0))
    prefix = np.concatenate([np.asarray(v, dtype=np.float64) for v in _iter_physical_raw(segment.matrices, channel_index, estimate_limit)])
    if prefix.size and np.any(~np.isfinite(prefix)):
        raise ValueError("non-finite samples")
    frequency, _ = estimate_frequency(prefix * gain + offset, segment.sample_rate_hz, nominal_frequency)
    if abs(frequency - nominal_frequency) > max(0.5, nominal_frequency * 0.02):
        quality.append(_quality("off_nominal_frequency", "warning", "estimated fundamental differs from nominal", channel,
                                nominal_hz=nominal_frequency, estimated_hz=frequency))
    else:
        frequency = float(nominal_frequency)

    samples_per_cycle = int(round(segment.sample_rate_hz / frequency))
    cycles = total_n // samples_per_cycle
    if cycles < 1:
        raise ValueError("shorter than one cycle")
    used_n = cycles * samples_per_cycle
    leftover = (total_n - used_n) / samples_per_cycle
    non_integer = leftover > non_integer_tolerance
    if non_integer:
        quality.append(_quality("non_integer_cycle", "warning", "window is not integral cycles", channel,
                                cycles=float(total_n / samples_per_cycle), leftover_cycle_fraction=float(leftover)))

    sums = np.zeros(harmonic_count + 1, dtype=np.complex128)
    physical_sum = 0.0
    # First pass: mean and saturation. Block columns are bounded by upload chunk size.
    offset_global = 0
    for raw in _iter_physical_raw(segment.matrices, channel_index, used_n):
        raw64 = np.asarray(raw, dtype=np.float64)
        physical = raw64 * gain + offset
        raw_sum += float(np.sum(raw64))
        physical_sum += float(np.sum(physical))
        saturated += int(np.sum(np.abs(raw64) >= scale_limit * saturation_fraction))
    raw_mean = raw_sum / used_n
    physical_mean = physical_sum / used_n
    dc_square_sum = used_n * (physical_mean if include_dc_in_rms else 0.0) ** 2

    for raw in _iter_physical_raw(segment.matrices, channel_index, used_n):
        raw64 = np.asarray(raw, dtype=np.float64)
        # Physical RMS around measured physical mean; dc_square_sum adds DC when requested.
        centered_physical = (raw64 - raw_mean) * gain
        square_sum_without_dc += float(np.sum(centered_physical * centered_physical))
        idx = np.arange(raw64.size)
        for harmonic in range(1, harmonic_count + 1):
            angular = -2j * np.pi * harmonic * frequency * (offset_global + idx) / segment.sample_rate_hz
            sums[harmonic] += np.sum((raw64 - raw_mean) * gain * np.exp(angular))
        offset_global += raw64.size
    coefficients = sums * (2.0 / used_n)
    coefficients[0] = complex(physical_mean, 0)
    if not use_raw_phase:
        for h in range(1, harmonic_count + 1):
            coefficients[h] *= np.exp(-1j * phase_shift_rad)

    if saturated:
        quality.append(_quality("saturation", "warning", "raw values reach configured full-scale threshold", channel,
                                saturated_samples=saturated, fraction=saturated / used_n,
                                threshold=scale_limit * saturation_fraction))
    harmonic_rms = np.abs(coefficients) / np.sqrt(2.0)
    harmonic_rms[0] = abs(physical_mean if include_dc_in_rms else 0.0)
    fundamental = float(harmonic_rms[1])
    if fundamental <= 1e-12:
        raise ValueError("zero fundamental")
    rms = float(np.sqrt((square_sum_without_dc + dc_square_sum) / used_n))
    numerator = float(np.sqrt(np.sum(harmonic_rms[2:] ** 2)))
    denominator = rms if thd_denominator == "rms" else fundamental
    thd = numerator / denominator

    # Preview spectrum: bounded prefix is enough for UI and avoids rFFT allocation over GB blocks.
    spectrum_n = min(used_n, max(samples_per_cycle, 8192))
    spectrum_x = prefix[:spectrum_n] * gain + offset
    spectrum_x = spectrum_x - np.mean(spectrum_x)
    fft = np.fft.rfft(spectrum_x)
    freqs = np.fft.rfftfreq(spectrum_x.size, d=1 / segment.sample_rate_hz)
    amp = np.abs(fft) * np.sqrt(2.0) / spectrum_x.size

    return {
        "channel": channel,
        "sample_rate_hz": float(segment.sample_rate_hz),
        "sample_count": int(total_n),
        "window_samples": int(used_n),
        "cycles": float(cycles),
        "estimated_frequency_hz": float(frequency),
        "dc": float(physical_mean),
        "rms": rms,
        "rms_includes_dc": bool(include_dc_in_rms),
        "fundamental": {"frequency_hz": float(frequency), "rms": fundamental,
                        "phase_rad": float(np.angle(coefficients[1])),
                        "phase_reference": "cos(2*pi*f*t); calibrated phase has sensor shift removed"},
        "harmonics": [
            {"order": h, "frequency_hz": float(h * frequency), "rms": float(harmonic_rms[h]),
             "phase_rad": float(np.angle(coefficients[h])),
             "percent_of_fundamental": float(100 * harmonic_rms[h] / fundamental)}
            for h in range(1, harmonic_count + 1)
        ],
        "thd": {"value_percent": float(100 * thd), "numerator": "sqrt(sum(H2..Hn rms^2))",
                "denominator": thd_denominator, "denominator_value": float(denominator)},
        "spectrum": {"frequencies_hz": freqs, "rms_amplitude": amp, "preview_samples": int(spectrum_x.size)},
        "quality_flags": [q.as_dict() if hasattr(q, "as_dict") else q for q in quality],
    }


def _jsonable_channel_result(result: dict[str, Any], include_spectrum: bool) -> dict[str, Any]:
    copy = dict(result)
    if include_spectrum:
        copy["spectrum"] = {
            "frequencies_hz": np.asarray(copy["spectrum"]["frequencies_hz"]).tolist(),
            "rms_amplitude": np.asarray(copy["spectrum"]["rms_amplitude"]).tolist(),
            "preview_samples": copy["spectrum"]["preview_samples"],
        }
    else:
        copy.pop("spectrum", None)
    return copy


def _downsample_channel(segment: WaveformSegment, channel: int, limit: int = 8000):
    points_x, points_y = [], []
    if segment.is_gap:
        return list(range(segment.start_sample_global, segment.start_sample_global + segment.sample_count, max(1, segment.sample_count // 100))), []
    per_block_budget = max(64, limit // max(1, len(segment.matrices)))
    offset = segment.start_sample_global
    for matrix in segment.matrices:
        y = np.asarray(matrix[:, channel], dtype=np.float64)
        x = np.arange(offset, offset + y.size)
        if y.size <= per_block_budget:
            xs, ys = x, y
        else:
            idx = np.linspace(0, y.size - 1, per_block_budget).astype(int)
            xs, ys = x[idx], y[idx]
        points_x.extend(xs.tolist()); points_y.extend(ys.tolist())
        offset += y.size
    return points_x, points_y


def _waveform_chart(segments: list[WaveformSegment], channels: list[str]) -> dict[str, Any]:
    series = []
    for ci, channel in enumerate(channels):
        xs, ys = [], []
        for seg in segments:
            x, y = _downsample_channel(seg, ci)
            if seg.is_gap:
                # ECharts connects finite points only across arrays; explicit gap marks power the table.
                continue
            xs.extend(x); ys.extend(y)
        series.append({"channel": channel, "sample": xs, "value": ys})
    return {"series": series}


def analyze_task(task: AnalysisTask, heartbeat=None) -> dict[str, Any]:
    snapshot = task.manifest_snapshot
    params = task.parameters
    channels = snapshot["channel_order"]
    channel_specs = {c["name"]: c for c in snapshot["channels"]}
    calibration = task.calibration_snapshot["channels"]
    quality: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="pq-analysis-") as tmp:
        segments, gaps, build_quality = build_segments(snapshot, Path(tmp))
        quality.extend(build_quality)
        if gaps:
            for gap in gaps:
                quality.append(_quality("missing_sample_interval", "warning",
                                        "acquisition gap retained explicitly instead of concatenating", detail=gap))
        rate_segments = snapshot.get("validation", {}).get("sample_rate_segments", [])
        if len(rate_segments) > 1:
            quality.append(_quality("sample_rate_change", "warning",
                                    "sample rate changes; calculations are segment-local with no implicit resampling",
                                    detail={"segments": rate_segments}))
        for spec in snapshot["channels"]:
            if spec.get("missing"):
                quality.append(_quality("missing_phase_or_channel", "fatal", "channel declared missing", spec["name"]))

        valid = [s for s in segments if not s.is_gap]
        if not valid:
            return {"status": ReportStatus.incomplete, "convention": {"raw_manifest_digest": snapshot["raw_manifest_digest"]},
                    "metrics": {"segments": [], "channels": {}, "symmetrical_components": {}, "stage_errors": {"global": ["no valid samples"]}},
                    "charts": {"waveforms": {}, "gaps": gaps, "spectrum": {}}, "quality_flags": quality + [_quality("no_valid_samples", "fatal", "no valid samples")]}
        representative = max(valid, key=lambda s: s.sample_count)
        stage_errors: dict[str, Any] = {}
        segment_results = []
        representative_full: dict[str, Any] = {}

        for seg in segments:
            if seg.is_gap:
                quality.append(_quality("gap_segment", "warning", "NaN gap segment excluded from phasor calculations",
                                        detail={"samples": seg.sample_count, "sample_rate_hz": seg.sample_rate_hz}))
                segment_results.append({"segment": seg.index, "gap": True, "samples": seg.sample_count, "channels": {}})
                continue
            channel_results = {}
            for ci, name in enumerate(channels):
                spec = channel_specs.get(name, {})
                if spec.get("missing"):
                    stage_errors.setdefault(name, []).append("channel declared missing")
                    continue
                cal = calibration.get(name, {"gain": 1.0, "offset": 0.0, "phase_shift_rad": 0.0})
                if name not in calibration:
                    quality.append(_quality("calibration_missing", "warning", "identity calibration used", name))
                try:
                    result = _stream_channel(
                        seg, ci, name,
                        nominal_frequency=snapshot["nominal_frequency"],
                        harmonic_count=params["harmonic_count"],
                        gain=cal["gain"], offset=cal["offset"], phase_shift_rad=cal["phase_shift_rad"],
                        use_raw_phase=params["use_raw_phase"], include_dc_in_rms=params["include_dc_in_rms"],
                        thd_denominator=params["thd_denominator"], non_integer_tolerance=params["non_integer_cycle_tolerance"],
                        saturation_fraction=params["saturation_fraction"], raw_dtype=snapshot["blocks"][0]["dtype"],
                        heartbeat=heartbeat,
                    )
                    quality.extend(result["quality_flags"])
                    json_result = _jsonable_channel_result(result, seg.index == representative.index)
                    channel_results[name] = _jsonable_channel_result(result, False)
                    if seg.index == representative.index:
                        representative_full[name] = json_result
                except Exception as exc:
                    stage_errors.setdefault(name, []).append(f"segment {seg.index}: {type(exc).__name__}: {exc}")
                    quality.append(_quality("phase_calculation_failed", "fatal",
                                            "phase calculation failed; diagnostics retained but report is incomplete",
                                            name, segment=seg.index, error=str(exc)))
            segment_results.append({"segment": seg.index, "sample_rate_hz": seg.sample_rate_hz,
                                    "start_sample_global": seg.start_sample_global, "samples": seg.sample_count,
                                    "source_blocks": seg.source_blocks, "channels": channel_results})

        phase_groups: dict[str, list[str]] = {}
        for name in channels:
            spec = channel_specs[name]
            if spec.get("phase") in {"A", "B", "C"} and spec.get("kind") in {"voltage", "current"}:
                phase_groups.setdefault(spec["kind"], []).append(name)
        components = {}
        rep_metrics = {name: _jsonable_channel_result(r, False) for name, r in representative_full.items()}
        for kind, names in phase_groups.items():
            phase_to_name = {channel_specs[n]["phase"]: n for n in names}
            phasors = {}
            for phase in ["A", "B", "C"]:
                name = phase_to_name.get(phase)
                if not name or name not in rep_metrics:
                    stage_errors.setdefault(kind, []).append(f"missing phase {phase}")
                    quality.append(_quality("missing_phase", "fatal", f"{kind} phase {phase} unavailable", name))
                    break
                f = rep_metrics[name]["fundamental"]
                phasors[phase] = f["rms"] * np.exp(1j * f["phase_rad"])
            if len(phasors) == 3:
                components[kind] = symmetrical_components(phasors, kind)

        if stage_errors:
            quality.append(_quality("report_incomplete", "fatal", "report contains failed stages and cannot be marked complete",
                                    detail={"failed_keys": list(stage_errors)}))
        status = ReportStatus.incomplete if any(q["severity"] in {"warning", "fatal"} for q in quality) else ReportStatus.complete
        charts = {"waveforms": _waveform_chart(segments, channels), "gaps": gaps,
                  "spectrum": {"segment": representative.index, "sample_rate_hz": representative.sample_rate_hz,
                               "series": [{"channel": n, **r["spectrum"]} for n, r in representative_full.items()]}}
        convention = {
            "calibration_model": "physical = raw * gain + offset; phasor phase multiplied by exp(-j*phase_shift_rad)",
            "phase_reference": "cosine: A cos(2*pi*f*t+phi) => RMS phasor A/sqrt(2)*exp(j*phi)",
            "rms": {"window": "integer-cycle streaming window", "includes_dc": bool(params["include_dc_in_rms"])},
            "thd": {"numerator": "sqrt(sum(H2..Hn RMS^2))", "denominator": params["thd_denominator"],
                    "note": "rms=THD-R; fundamental=THD-F"},
            "symmetrical_components": "Fortescue a=e^(j2pi/3)",
            "sample_rate_changes": "separate constant-rate segments, no implicit resampling",
            "gaps": "explicit gap/NaN records; never sorted away",
            "calibration_snapshot_version": task.calibration_snapshot.get("version"),
            "raw_manifest_digest": snapshot["raw_manifest_digest"],
        }
        return {"status": status, "convention": convention,
                "metrics": {"representative_segment": representative.index, "segments": segment_results,
                            "channels": rep_metrics, "symmetrical_components": components, "stage_errors": stage_errors},
                "charts": charts, "quality_flags": quality}
