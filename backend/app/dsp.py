from __future__ import annotations

import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.fft import rfft, rfftfreq

# Conventions documented in /docs/analysis-conventions.md.
# Fundamental phase is the angle of the one-cycle-bin DFT coefficient:
# x[n] = sum_h A_h cos(2*pi*h*f0*n/fs + phi_h), phasor = X_h / N.
# RMS is true RMS including DC. THD = sqrt(sum(V2..Vmax^2)) / V1_RMS * 100%.

PHASE_ALIASES = {
    "a": "a",
    "va": "a",
    "ia": "a",
    "u_a": "a",
    "i_a": "a",
    "phase_a": "a",
    "l1": "a",
    "b": "b",
    "vb": "b",
    "ib": "b",
    "u_b": "b",
    "i_b": "b",
    "phase_b": "b",
    "l2": "b",
    "c": "c",
    "vc": "c",
    "ic": "c",
    "u_c": "c",
    "i_c": "c",
    "phase_c": "c",
    "l3": "c",
}

PHASE_ROTATION = {"a": 0, "b": 1, "c": 2}


@dataclass
class QualityItem:
    severity: str
    code: str
    message: str
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def default_params() -> dict[str, Any]:
    return {
        "fundamental_hz": 50.0,
        "cycles_per_window": 6,
        "max_harmonic": 15,
        "fundamental_tolerance": 5e-3,
        "saturation_warning_fraction": 0.01,
        "max_quality_status": "warning",
    }


def decode_chunk(raw: bytes, sample_count: int, channels: list[str], encoding: str = "float32le-interleaved") -> np.ndarray:
    if encoding != "float32le-interleaved":
        raise ValueError(f"unsupported encoding: {encoding}")
    expected_bytes = sample_count * len(channels) * 4
    if len(raw) != expected_bytes:
        raise ValueError(f"raw block has {len(raw)} bytes but metadata requires {expected_bytes}")
    values = np.frombuffer(raw, dtype="<f4").reshape(sample_count, len(channels)).astype(np.float64)
    if not np.all(np.isfinite(values)):
        raise ValueError("raw block contains NaN or infinite samples")
    return values


def group_chunks_by_rate(
    chunks: list[dict[str, Any]],
    raw_by_sequence: dict[int, bytes | str] | None = None,
    *,
    work_dir: str | Path | None = None,
    fetch_raw=None,
) -> list[dict[str, Any]]:
    """Build physically contiguous constant-rate segments.

    Blocks are assembled in verified sequence order into disk-backed float64
    files, keeping multi-GB recordings out of Python memory. A rate change
    starts a new segment; samples are never resampled or clock-concatenated.
    """

    ordered = sorted(chunks, key=lambda item: item["sequence"])
    if not ordered:
        raise ValueError("no raw chunks available to decode")
    actual_sequences = [int(item["sequence"]) for item in ordered]
    if actual_sequences != list(range(actual_sequences[0], actual_sequences[-1] + 1)):
        raise ValueError(f"chunk sequences are not contiguous: {actual_sequences}")

    scratch = Path(work_dir or tempfile.mkdtemp(prefix="pq-segments-"))
    scratch.mkdir(parents=True, exist_ok=True)
    segment_specs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for meta in ordered:
        sequence = int(meta["sequence"])
        if raw_by_sequence is not None and sequence in raw_by_sequence:
            raw = raw_by_sequence[sequence]
        elif fetch_raw is not None:
            raw = fetch_raw(meta)
        else:
            raise KeyError(f"missing raw bytes for sequence {sequence}")
        if raw is not None:
            if isinstance(raw, bytes | str | Path):
                if isinstance(raw, bytes):
                    values = decode_chunk(
                        raw,
                        int(meta["sample_count"]),
                        list(meta["channels"]),
                        meta.get("encoding", "float32le-interleaved"),
                    )
                else:
                    raw_path = Path(raw)
                    expected_bytes = int(meta["sample_count"]) * len(meta["channels"]) * 4
                    if raw_path.stat().st_size != expected_bytes:
                        raise ValueError(
                            f"raw block has {raw_path.stat().st_size} bytes but metadata requires {expected_bytes}"
                        )
                    raw_mmap = np.memmap(raw_path, dtype="<f4", mode="r")
                    values = raw_mmap.reshape(int(meta["sample_count"]), len(meta["channels"])).astype(np.float64)
                    if not np.all(np.isfinite(values)):
                        raise ValueError("raw block contains NaN or infinite samples")
            else:
                values = raw
        if current is None or not math.isclose(current["sample_rate"], float(meta["sample_rate"]), rel_tol=0.0):
            current = {
                "sample_rate": float(meta["sample_rate"]),
                "channels": list(meta["channels"]),
                "total_samples": 0,
                "path": scratch / f"segment-{len(segment_specs)}.f64",
                "start_sequence": sequence,
                "end_sequence": sequence,
            }
            segment_specs.append(current)
        elif list(meta["channels"]) != current["channels"]:
            raise ValueError(f"channel order changed at sequence {sequence}")
        with current["path"].open("ab") as out:
            values.tofile(out)
        current["total_samples"] += values.shape[0]
        current["end_sequence"] = sequence

    return [
        {
            "sample_rate": spec["sample_rate"],
            "channels": spec["channels"],
            "data": np.memmap(
                spec["path"],
                dtype=np.float64,
                mode="r",
                shape=(spec["total_samples"], len(spec["channels"])),
            ),
            "path": spec["path"],
            "start_sequence": spec["start_sequence"],
            "end_sequence": spec["end_sequence"],
        }
        for spec in segment_specs
    ]


def calibrate_series(values: np.ndarray, coefficient: dict[str, Any]) -> np.ndarray:
    gain = float(coefficient.get("gain", 1.0))
    offset = float(coefficient.get("offset", 0.0))
    phase_shift = float(coefficient.get("phase_shift_rad", 0.0))
    if phase_shift:
        spectrum = rfft(values)
        spectrum[1:] *= np.exp(-1j * phase_shift)
        from scipy.fft import irfft

        values = irfft(spectrum, n=values.shape[0])
    return values * gain + offset


def apply_calibration(data: np.ndarray, channels: list[str], coefficients: dict[str, Any]) -> np.ndarray:
    calibrated = np.empty_like(data)
    for index, channel in enumerate(channels):
        coef = coefficients.get(channel)
        if coef is None:
            raise KeyError(f"missing calibration coefficient for channel {channel}")
        calibrated[:, index] = calibrate_series(data[:, index], coef)
    return calibrated


def detect_saturation(
    values: np.ndarray, coefficient: dict[str, Any], warning_fraction: float
) -> list[QualityItem]:
    n = values.size
    low = coefficient.get("saturation_low")
    high = coefficient.get("saturation_high")
    mask = np.zeros(n, dtype=bool)
    details: dict[str, Any] = {}
    if low is not None:
        low_mask = values <= float(low)
        mask |= low_mask
        details["low_fraction"] = float(np.mean(low_mask))
    if high is not None:
        high_mask = values >= float(high)
        mask |= high_mask
        details["high_fraction"] = float(np.mean(high_mask))
    if low is None and high is None:
        return []
    fraction = float(np.mean(mask))
    details["fraction"] = fraction
    if fraction > 0.5:
        return [QualityItem("error", "saturation", "more than half samples are at a configured saturation limit", details)]
    if fraction >= warning_fraction:
        return [QualityItem("warning", "saturation", "samples reached a configured saturation limit", details)]
    return []


def find_fundamental_bin(n: int, fs: float, f0: float) -> tuple[int, float, float]:
    frequencies = rfftfreq(n, d=1.0 / fs)
    target_index = int(round(f0 * n / fs))
    if target_index <= 0 or target_index >= frequencies.size:
        raise ValueError(f"fundamental {f0} Hz is outside the sampled spectrum at fs={fs}, N={n}")
    return target_index, float(frequencies[target_index]), f0


def harmonic_analysis(
    values: np.ndarray,
    fs: float,
    params: dict[str, Any],
    coefficients: dict[str, Any] | None = None,
    *,
    complete_window: bool = True,
) -> dict[str, Any]:
    n = values.size
    f0_nominal = float(params["fundamental_hz"])
    cycles = int(params["cycles_per_window"])
    max_h = int(params["max_harmonic"])
    tolerance = float(params["fundamental_tolerance"])
    expected_n = int(round(cycles * fs / f0_nominal))
    qualities: list[QualityItem] = []

    if n != expected_n:
        if not complete_window:
            qualities.append(
                QualityItem(
                    "warning",
                    "non_integer_cycle",
                    "segment/window length is not an exact integer-cycle record",
                    {"samples": n, "expected_samples": expected_n},
                )
            )
        else:
            return {
                "rms": float(np.sqrt(np.mean(np.square(values)))),
                "dc": float(np.mean(values)),
                "fundamental": {"frequency_hz": f0_nominal, "rms": None, "phase_rad": None, "phase_deg": None},
                "thd_percent": None,
                "harmonics": [],
                "quality": [
                    QualityItem(
                        "warning",
                        "non_integer_cycle",
                        "window length unexpectedly differs from configured integer-cycle length",
                        {"samples": n, "expected_samples": expected_n},
                    ).as_dict()
                ],
            }

    spectrum = rfft(values)
    bin_index, actual_f0, _ = find_fundamental_bin(n, fs, f0_nominal)
    relative_error = abs(actual_f0 - f0_nominal) / f0_nominal
    if relative_error > tolerance:
        qualities.append(
            QualityItem(
                "warning",
                "fundamental_off_bin",
                "fundamental DFT bin differs from configured nominal frequency",
                {"nominal_hz": f0_nominal, "bin_hz": actual_f0, "relative_error": relative_error},
            )
        )

    fundamental_coef = spectrum[bin_index] / n
    fundamental_rms = float(2.0 * abs(fundamental_coef) / math.sqrt(2.0))
    if fundamental_rms <= 1e-12:
        qualities.append(QualityItem("error", "missing_fundamental", "fundamental RMS is zero", {}))

    harmonics = []
    sum_sq = 0.0
    for harmonic in range(1, max_h + 1):
        idx = bin_index * harmonic
        if idx >= spectrum.size:
            break
        phasor_coef = spectrum[idx] / n
        amplitude = float(2.0 * abs(phasor_coef))
        rms = amplitude / math.sqrt(2.0)
        phase = float(np.angle(phasor_coef))
        item = {
            "order": harmonic,
            "frequency_hz": float(actual_f0 * harmonic),
            "amplitude": amplitude,
            "rms": rms,
            "phase_rad": phase,
            "phase_deg": math.degrees(phase),
        }
        harmonics.append(item)
        if harmonic > 1 and idx < spectrum.size:
            sum_sq += rms**2

    # True RMS is independent of DFT window coherence and includes DC/noise.
    rms = float(np.sqrt(np.mean(np.square(values))))
    dc = float(np.mean(values))
    thd = math.sqrt(sum_sq) / fundamental_rms * 100.0 if fundamental_rms > 1e-12 else None
    if coefficients:
        qualities.extend(
            detect_saturation(values, coefficients, float(params["saturation_warning_fraction"]))
        )

    return {
        "rms": rms,
        "dc": dc,
        "fundamental": {
            "frequency_hz": actual_f0,
            "rms": fundamental_rms,
            "phase_rad": harmonics[0]["phase_rad"],
            "phase_deg": harmonics[0]["phase_deg"],
        },
        "thd_percent": thd,
        "harmonics": harmonics,
        "quality": [item.as_dict() for item in qualities],
    }


def identify_phase(channel: str) -> str | None:
    normalized = channel.strip().lower().replace("-", "_")
    normalized = re.sub(r"[^a-z0-9_]", "", normalized)
    if normalized in PHASE_ALIASES:
        return PHASE_ALIASES[normalized]
    if "_" in normalized:
        token = normalized.rsplit("_", 1)[-1]
        if token in PHASE_ALIASES:
            return PHASE_ALIASES[token]
    if normalized and normalized[-1] in {"a", "b", "c"} and normalized != normalized[-1]:
        return normalized[-1]
    return PHASE_ALIASES.get(normalized)


def phase_channels(channels: list[str]) -> dict[str, dict[str, str]]:
    mappings: dict[str, dict[str, str]] = {"voltage": {}, "current": {}}
    for channel in channels:
        phase = identify_phase(channel)
        if phase is None:
            continue
        lower = channel.lower().replace("-", "_")
        kind = "current" if lower.startswith(("i", "current")) or lower.endswith(("_ia", "_ib", "_ic")) else "voltage"
        mappings[kind][phase] = channel
    return mappings


def symmetrical_components(phasors: dict[str, complex]) -> dict[str, Any]:
    required = ("a", "b", "c")
    missing = [phase for phase in required if phasors.get(phase) is None]
    if missing:
        return {
            "status": "failed",
            "missing_phases": missing,
            "message": "symmetrical components require A, B and C phasors",
        }
    a = phasors["a"]
    b = phasors["b"]
    c = phasors["c"]
    alpha = complex(-0.5, math.sqrt(3.0) / 2.0)
    positive = (a + alpha * b + alpha**2 * c) / 3.0
    negative = (a + alpha**2 * b + alpha * c) / 3.0
    zero = (a + b + c) / 3.0

    def output(value: complex, name: str) -> dict[str, Any]:
        return {
            f"{name}_phasor": {"real": value.real, "imag": value.imag},
            f"{name}_rms": float(abs(value) / math.sqrt(2.0)),
            f"{name}_phase_deg": math.degrees(math.atan2(value.imag, value.real)),
        }

    result: dict[str, Any] = {"status": "ok"}
    result.update(output(zero, "zero"))
    result.update(output(positive, "positive"))
    result.update(output(negative, "negative"))
    p1 = result["positive_rms"]
    if p1 > 1e-12:
        result["unbalance_percent"] = result["negative_rms"] / p1 * 100.0
    else:
        result["unbalance_percent"] = None
    return result


def aggregate_mean(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return float(np.mean(clean))


def analyze_segment(data: np.ndarray, channels: list[str], fs: float, coefficients: dict, params: dict) -> dict[str, Any]:
    cycles = int(params["cycles_per_window"])
    f0 = float(params["fundamental_hz"])
    window = int(round(cycles * fs / f0))
    channel_results: dict[str, Any] = {}
    quality: list[dict[str, Any]] = []

    if data.shape[0] < window:
        ranges = [(0, data.shape[0], False)]
        quality.append({
            "channel": "*",
            "severity": "warning",
            "code": "non_integer_cycle",
            "message": "segment is shorter than the configured integer-cycle window",
            "details": {"samples": int(data.shape[0]), "required_samples": window},
        })
    else:
        usable = data.shape[0] - (data.shape[0] % window)
        ranges = [(start, start + window, True) for start in range(0, usable, window)]
        if usable < data.shape[0]:
            quality.append({
                "channel": "*",
                "severity": "warning",
                "code": "non_integer_cycle",
                "message": "trailing samples do not form a complete integer-cycle window and are not analyzed",
                "details": {"omitted_samples": int(data.shape[0] - usable)},
            })

    for channel_index, channel in enumerate(channels):
        coefficient = coefficients.get(channel)
        if coefficient is None:
            raise KeyError(f"missing calibration coefficient for channel {channel}")
        windows = []
        rms_values = []
        thd_values = []
        for start, end, complete_window in ranges:
            calibrated = calibrate_series(np.asarray(data[start:end, channel_index]), coefficient)
            item = harmonic_analysis(
                calibrated,
                fs,
                params,
                coefficient,
                complete_window=complete_window,
            )
            item["start_sample"] = start
            item["end_sample"] = end
            windows.append(item)
            rms_values.append(item["rms"])
            thd_values.append(item["thd_percent"])
            for q in item["quality"]:
                quality.append({"channel": channel, **q})
        channel_results[channel] = {
            "rms": aggregate_mean(rms_values),
            "thd_percent": aggregate_mean(thd_values),
            "windows": windows,
            "phase": identify_phase(channel),
        }

    mappings = phase_channels(channels)
    sequence_results: dict[str, Any] = {}
    for kind, mapping in mappings.items():
        if not mapping:
            continue
        phase_windows: dict[str, list[complex | None]] = {
            phase: [] for phase in ("a", "b", "c")
        }
        missing = [phase for phase in ("a", "b", "c") if phase not in mapping]
        for phase in ("a", "b", "c"):
            channel = mapping.get(phase)
            if channel is None:
                phase_windows[phase] = [None] * len(ranges)
                continue
            for window_index, window_result in enumerate(channel_results[channel]["windows"]):
                if not ranges[window_index][2]:
                    phase_windows[phase].append(None)
                    continue
                fund = window_result["fundamental"]
                if fund.get("rms") is None:
                    phase_windows[phase].append(None)
                    continue
                amp = fund["rms"] * math.sqrt(2.0)
                phase_windows[phase].append(amp * np.exp(1j * fund["phase_rad"]))
        for index, range_info in enumerate(ranges):
            if not range_info[2]:
                continue
            phasors = {}
            for phase in ("a", "b", "c"):
                values = phase_windows.get(phase, [])
                phasors[phase] = values[index] if index < len(values) else None
            seq = symmetrical_components(phasors)
            seq.update({"window_index": index, "start_sample": range_info[0], "end_sample": range_info[1]})
            if seq["status"] != "ok":
                quality.append(
                    {
                        "channel": kind,
                        "severity": "error",
                        "code": "missing_phase",
                        "message": seq["message"],
                        "missing_phases": missing,
                    }
                )
            sequence_results.setdefault(kind, []).append(seq)

    status = aggregate_quality(quality)
    return {
        "sample_rate": fs,
        "samples": int(data.shape[0]),
        "channels": channel_results,
        "symmetrical_components": sequence_results,
        "quality": quality,
        "quality_status": status,
    }


def aggregate_quality(quality: list[dict[str, Any]]) -> str:
    severities = {item.get("severity") for item in quality}
    if "error" in severities:
        return "error"
    if "warning" in severities:
        return "warning"
    return "ok"


def analyze_all_segments(
    segments: list[dict[str, Any]],
    coefficients: dict[str, Any],
    params: dict[str, Any],
    sample_rate_changed: bool = False,
) -> dict[str, Any]:
    results = []
    quality: list[dict[str, Any]] = []
    for segment in segments:
        result = analyze_segment(segment["data"], segment["channels"], segment["sample_rate"], coefficients, params)
        results.append(result)
        quality.extend(result["quality"])
    if sample_rate_changed and len(segments) > 1:
        quality.append(
            {
                "channel": "*",
                "severity": "warning",
                "code": "sample_rate_changed",
                "message": "recording spans multiple sampling rates; segments are not resampled or joined",
                "sample_rates": [segment["sample_rate"] for segment in segments],
            }
        )
    return {
        "segments": results,
        "quality": quality,
        "quality_status": aggregate_quality(quality),
        "conventions": {
            "rms": "true RMS over the calibrated time-domain window, including DC",
            "fundamental_phase": "angle of DFT bin coefficient for x=A cos(2πf t+φ)",
            "thd": "sqrt(sum RMS_2..N^2) / fundamental_RMS * 100%",
            "symmetrical_components": "fundamental RMS phasors, Fortescue A=(Va+aVb+a²Vc)/3",
        },
    }
