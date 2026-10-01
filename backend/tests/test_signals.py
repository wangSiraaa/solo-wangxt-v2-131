import numpy as np

from app.services.signals import channel_metrics, symmetrical_components


def test_known_harmonics_and_thd_conventions():
    fs = 1000.0
    n = 2000
    t = np.arange(n) / fs
    # Fundamental 1 Vrms; fifth 0.05 Vrms; no DC.
    x = np.sqrt(2) * np.cos(2 * np.pi * 50 * t) + 0.05 * np.sqrt(2) * np.cos(2 * np.pi * 250 * t)
    result = channel_metrics(
        x, fs, channel="Va", nominal_frequency=50, harmonic_count=25, thd_denominator="rms"
    )
    assert abs(result["fundamental"]["rms"] - 1.0) < 1e-9
    assert abs(result["harmonics"][4]["rms"] - 0.05) < 1e-9
    expected_rss = 0.05 / np.sqrt(1 + 0.05**2)
    assert abs(result["thd"]["value_percent"] / 100 - expected_rss) < 1e-8

    result_f = channel_metrics(
        x, fs, channel="Va", nominal_frequency=50, harmonic_count=25, thd_denominator="fundamental"
    )
    assert abs(result_f["thd"]["value_percent"] - 5.0) < 1e-6
    assert abs(result["fundamental"]["phase_rad"]) < 1e-9
    assert abs(result["rms"] - np.sqrt(1 + 0.05**2)) < 1e-8


def test_calibration_phase_and_gain_are_applied():
    fs = 1000.0
    t = np.arange(2000) / fs
    x = np.sqrt(2) * np.cos(2 * np.pi * 50 * t + 0.3)
    result = channel_metrics(
        x,
        fs,
        channel="Va",
        nominal_frequency=50,
        gain=2.0,
        offset=0.0,
        phase_shift_rad=0.3,
        include_dc_in_rms=False,
    )
    assert abs(result["fundamental"]["rms"] - 2.0) < 1e-9
    assert abs(result["fundamental"]["phase_rad"]) < 1e-9


def test_symmetrical_components_reverse_polarity_example():
    # Balanced ABC before reversal. Reversing C multiplies its phasor by -1.
    rms = 1.0
    va = rms * np.exp(0j)
    vb = rms * np.exp(-2j * np.pi / 3)
    vc_normal = rms * np.exp(2j * np.pi / 3)
    normal = symmetrical_components({"A": va, "B": vb, "C": vc_normal}, "voltage")
    assert abs(normal["positive"]["rms"] - 1.0) < 1e-12
    assert abs(normal["negative"]["rms"]) < 1e-12
    assert abs(normal["zero"]["rms"]) < 1e-12

    reversed_c = -vc_normal
    result = symmetrical_components({"A": va, "B": vb, "C": reversed_c}, "voltage")
    assert result["negative_unbalance_percent"] > 50
    assert result["zero_unbalance_percent"] > 50
    expected = (va + np.exp(2j*np.pi/3)*vb + np.exp(4j*np.pi/3)*reversed_c)/3
    assert abs(result["positive"]["rms"] - abs(expected)) < 1e-12
