from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import signal


@dataclass
class QualityEvent:
    code: str
    severity: str
    message: str
    channel: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "channel": self.channel,
            "detail": self.detail,
        }


def estimate_frequency(x: np.ndarray, sample_rate: float, nominal_frequency: float) -> tuple[float, float]:
    """Estimate the strongest component near nominal. Returns (frequency_hz, peak_spectral_amplitude)."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    x = x - float(np.mean(x))
    if x.size < 8:
        return nominal_frequency, 0.0
    nperseg = min(x.size, max(256, int(round(sample_rate * 0.2))))
    nperseg = min(nperseg, x.size)
    if nperseg % 2:
        nperseg -= 1
    freqs, psd = signal.welch(
        x,
        fs=sample_rate,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        window="hann",
        scaling="spectrum",
    )
    low = max(1, nominal_frequency * 0.8)
    high = min(sample_rate / 2.5, nominal_frequency * 1.2)
    mask = (freqs >= low) & (freqs <= high)
    if not np.any(mask):
        return nominal_frequency, 0.0
    idx = np.argmax(psd[mask]) + int(np.argmax(mask))
    # Parabolic interpolation gives sub-bin frequency.
    if 0 < idx < len(psd) - 1:
        y0, y1, y2 = np.log(max(psd[idx - 1], 1e-300)), np.log(max(psd[idx], 1e-300)), np.log(
            max(psd[idx + 1], 1e-300)
        )
        denom = 2 * (y0 - (2 * y1) + y2)
        delta = (y0 - y2) / denom if abs(denom) > 1e-15 else 0.0
        delta = float(np.clip(delta, -1, 1))
        bin_hz = freqs[1] - freqs[0]
        f = float(freqs[idx] + delta * bin_hz)
    else:
        f = float(freqs[idx])
    return f, float(np.sqrt(max(psd[idx], 0.0)))


def select_integer_window(
    x: np.ndarray,
    sample_rate: float,
    frequency: float,
    tolerance_fraction: float = 0.05,
) -> tuple[np.ndarray, int, float, bool]:
    samples_per_cycle = int(round(sample_rate / frequency))
    max_cycles = int(x.size // samples_per_cycle)
    if max_cycles < 1:
        return x, 0, x.size / samples_per_cycle, True
    used = max_cycles * samples_per_cycle
    leftover = x.size - used
    fraction = leftover / samples_per_cycle
    non_integer = fraction > tolerance_fraction
    return x[:used], max_cycles, fraction, non_integer


def calibrated_series(
    raw: np.ndarray, gain: float = 1.0, offset: float = 0.0
) -> np.ndarray:
    """Physical SI unit model: physical = raw * gain + offset."""
    return np.asarray(raw, dtype=np.float64) * float(gain) + float(offset)


def harmonic_phasors(
    x: np.ndarray,
    sample_rate: float,
    fundamental_frequency: float,
    harmonic_count: int,
) -> np.ndarray:
    """Single-sided DFT coefficients for DC and harmonics 1..harmonic_count.

    Cosine reference: x(t)=A cos(2πft+φ) gives coefficient A e^{jφ}; therefore its
    RMS phasor is coefficient/sqrt(2), and phase is radians relative to cos(ωt).
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    t_index = np.arange(n, dtype=np.float64)
    coefficients = np.zeros(harmonic_count + 1, dtype=np.complex128)
    for harmonic in range(1, harmonic_count + 1):
        angular = -2j * np.pi * harmonic * fundamental_frequency * t_index / sample_rate
        coefficients[harmonic] = (2.0 / n) * np.sum(x * np.exp(angular))
    coefficients[0] = complex(float(np.mean(x)), 0.0)
    return coefficients


def apply_phase_calibration(phasor: complex, phase_shift_rad: float) -> complex:
    """Remove a sensor/channel phase lead from the measured RMS phasor."""
    return complex(phasor) * np.exp(-1j * float(phase_shift_rad))


def rms_from_phasor(coefficient: complex) -> float:
    return abs(complex(coefficient)) / np.sqrt(2.0)


def channel_metrics(
    raw: np.ndarray,
    sample_rate: float,
    *,
    channel: str,
    nominal_frequency: float,
    harmonic_count: int = 25,
    gain: float = 1.0,
    offset: float = 0.0,
    phase_shift_rad: float = 0.0,
    use_raw_phase: bool = False,
    include_dc_in_rms: bool = False,
    thd_denominator: str = "rms",
    window_mode: str = "integer_cycle",
    non_integer_tolerance: float = 0.05,
    saturation_fraction: float = 0.98,
    raw_dtype: str = "float32",
) -> dict[str, Any]:
    quality: list[QualityEvent] = []
    finite = np.isfinite(raw)
    if not np.all(finite):
        quality.append(
            QualityEvent(
                "nan_or_inf_sample",
                "fatal",
                "channel contains missing/non-finite samples",
                channel,
                {"bad_samples": int(np.sum(~finite))},
            )
        )
        raise ValueError("non-finite samples cannot be analyzed")

    physical = calibrated_series(raw, gain, offset)
    scale_limit = 1.0 if raw_dtype == "float32" else float(np.iinfo(np.int16).max)
    saturated = int(np.sum(np.abs(raw) >= scale_limit * saturation_fraction))
    if saturated:
        quality.append(
            QualityEvent(
                "saturation",
                "warning",
                "raw values reach the configured ADC/full-scale saturation threshold",
                channel,
                {
                    "saturated_samples": saturated,
                    "fraction": saturated / raw.size,
                    "threshold": scale_limit * saturation_fraction,
                },
            )
        )

    estimated_frequency, _ = estimate_frequency(physical, sample_rate, nominal_frequency)
    if abs(estimated_frequency - nominal_frequency) > max(0.5, nominal_frequency * 0.02):
        quality.append(
            QualityEvent(
                "off_nominal_frequency",
                "warning",
                "estimated fundamental differs from nominal frequency",
                channel,
                {"nominal_hz": nominal_frequency, "estimated_hz": estimated_frequency},
            )
        )
    else:
        # Coherent nominal-frequency records should use the nominal grid convention exactly;
        # a tiny Welch interpolation error must not manufacture spectral leakage.
        estimated_frequency = float(nominal_frequency)

    if window_mode == "integer_cycle":
        window, cycles, leftover_fraction, non_integer = select_integer_window(
            physical, sample_rate, estimated_frequency, non_integer_tolerance
        )
        if cycles < 1:
            quality.append(
                QualityEvent(
                    "shorter_than_one_cycle",
                    "fatal",
                    "record segment is shorter than one estimated fundamental cycle",
                    channel,
                    {"samples": physical.size, "sample_rate": sample_rate, "estimated_hz": estimated_frequency},
                )
            )
            raise ValueError("shorter than one cycle")
    else:
        window = physical
        samples_per_cycle = sample_rate / estimated_frequency
        cycles = window.size / samples_per_cycle
        non_integer = abs(round(cycles) - cycles) > non_integer_tolerance
        leftover_fraction = cycles - int(cycles)
    if non_integer:
        quality.append(
            QualityEvent(
                "non_integer_cycle",
                "warning",
                "window is not an integral number of cycles; harmonic leakage may be elevated",
                channel,
                {"cycles": float(cycles), "leftover_cycle_fraction": float(leftover_fraction)},
            )
        )

    coefficients = harmonic_phasors(window, sample_rate, estimated_frequency, harmonic_count)
    if not use_raw_phase:
        for h in range(1, harmonic_count + 1):
            coefficients[h] = apply_phase_calibration(coefficients[h], phase_shift_rad)

    harmonic_rms = [rms_from_phasor(coefficients[h]) for h in range(harmonic_count + 1)]
    fundamental_rms = harmonic_rms[1]
    if fundamental_rms <= 1e-12:
        quality.append(QualityEvent("missing_fundamental", "fatal", "fundamental RMS is zero", channel))
        raise ValueError("zero fundamental")

    dc = float(np.real(coefficients[0]))
    ac_window = window if include_dc_in_rms else window - float(np.mean(window))
    total_rms = float(np.sqrt(np.mean(np.square(ac_window, dtype=np.float64))))
    harmonic_rss_except_fundamental = float(np.sqrt(np.sum(np.square(harmonic_rms[2:]))))
    denominator = total_rms if thd_denominator == "rms" else fundamental_rms
    thd = harmonic_rss_except_fundamental / denominator

    # rfft spectrum uses the same window; return RMS amplitude for chart/API.
    spectrum = np.fft.rfft(window - np.mean(window))
    spectrum_rms = np.abs(spectrum) * np.sqrt(2.0) / window.size
    spectrum_frequencies = np.fft.rfftfreq(window.size, d=1.0 / sample_rate)

    return {
        "channel": channel,
        "sample_rate_hz": float(sample_rate),
        "sample_count": int(physical.size),
        "window_samples": int(window.size),
        "cycles": float(cycles),
        "estimated_frequency_hz": float(estimated_frequency),
        "dc": dc,
        "rms": total_rms,
        "rms_includes_dc": bool(include_dc_in_rms),
        "fundamental": {
            "frequency_hz": float(estimated_frequency),
            "rms": float(fundamental_rms),
            "phase_rad": float(np.angle(coefficients[1])),
            "phase_reference": "cos(2*pi*f*t); calibrated phase has sensor shift removed",
        },
        "harmonics": [
            {
                "order": h,
                "frequency_hz": float(h * estimated_frequency),
                "rms": float(harmonic_rms[h]),
                "phase_rad": float(np.angle(coefficients[h])),
                "percent_of_fundamental": float(100.0 * harmonic_rms[h] / fundamental_rms),
            }
            for h in range(1, harmonic_count + 1)
        ],
        "thd": {
            "value_percent": float(100.0 * thd),
            "numerator": "sqrt(sum(H2..Hn rms^2))",
            "denominator": thd_denominator,
            "denominator_value": float(denominator),
        },
        "spectrum": {
            "frequencies_hz": spectrum_frequencies,
            "rms_amplitude": spectrum_rms,
        },
        "quality_flags": [q.as_dict() for q in quality],
    }


def symmetrical_components(phasors: dict[str, complex], kind: str) -> dict[str, Any]:
    missing = sorted({"A", "B", "C"} - set(phasors))
    if missing:
        raise ValueError(f"missing phases for {kind} symmetrical components: {missing}")
    va, vb, vc = phasors["A"], phasors["B"], phasors["C"]
    a = np.exp(2j * np.pi / 3.0)
    positive = (va + a * vb + a * a * vc) / 3.0
    negative = (va + a * a * vb + a * vc) / 3.0
    zero = (va + vb + vc) / 3.0
    ref = abs(positive) if abs(positive) > 1e-12 else None
    return {
        "kind": kind,
        "reference_synonym": "positive_sequence_also_called_positive_order_component",
        "transform": "Fortescue: V1=(Va+aVb+a²Vc)/3, V2=(Va+a²Vb+aVc)/3, V0=(Va+Vb+Vc)/3; a=e^(j2π/3)",
        "positive": {"rms": float(abs(positive)), "phase_rad": float(np.angle(positive))},
        "negative": {"rms": float(abs(negative)), "phase_rad": float(np.angle(negative))},
        "zero": {"rms": float(abs(zero)), "phase_rad": float(np.angle(zero))},
        "negative_unbalance_percent": float(100.0 * abs(negative) / abs(positive)) if ref else None,
        "zero_unbalance_percent": float(100.0 * abs(zero) / abs(positive)) if ref else None,
        "input_phase_rad": {p: float(np.angle(v)) for p, v in phasors.items()},
    }
