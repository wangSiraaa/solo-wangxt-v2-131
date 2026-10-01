import math

from app.dsp import analyze_all_segments, analyze_segment, default_params, group_chunks_by_rate
from tests.synthetic import CHANNELS, coefficients, make_chunks


def test_known_harmonics_thd_and_true_rms():
    chunks = make_chunks(sample_chunks=(720,))
    segment = group_chunks_by_rate(
        [{k: v for k, v in chunks[0].items() if k != "raw"}], {0: chunks[0]["raw"]}
    )
    result = analyze_segment(segment[0]["data"], CHANNELS, 6000.0, coefficients(), default_params())
    va = result["channels"]["Va"]
    assert abs(va["rms"] - math.sqrt(300**2 + 10**2 + 20**2)) < 1e-7
    assert abs(va["thd_percent"] - math.sqrt(500) / 3) < 1e-7
    assert result["quality_status"] == "ok"
    harmonic_rms = {item["order"]: item["rms"] for item in va["windows"][0]["harmonics"]}
    assert abs(harmonic_rms[1] - 300.0) < 1e-7
    assert abs(harmonic_rms[3] - 10.0) < 1e-7
    assert abs(harmonic_rms[5] - 20.0) < 1e-7


def test_balanced_three_phase_sequence_components():
    chunks = make_chunks(sample_chunks=(720,))
    segment = group_chunks_by_rate(
        [{k: v for k, v in chunks[0].items() if k != "raw"}], {0: chunks[0]["raw"]}
    )
    result = analyze_segment(segment[0]["data"], CHANNELS, 6000.0, coefficients(), default_params())
    seq = result["symmetrical_components"]["voltage"][0]
    assert seq["status"] == "ok"
    assert abs(seq["positive_rms"] - 300.0) < 1e-7
    assert seq["negative_rms"] < 1e-9
    assert abs(seq["zero_rms"]) < 1e-9
    assert seq["unbalance_percent"] < 1e-9


def test_reversed_polarity_is_negative_sequence():
    chunks = make_chunks(sample_chunks=(720,), reversed_a=True)
    segment = group_chunks_by_rate(
        [{k: v for k, v in chunks[0].items() if k != "raw"}], {0: chunks[0]["raw"]}
    )
    result = analyze_segment(segment[0]["data"], CHANNELS, 6000.0, coefficients(), default_params())
    seq = result["symmetrical_components"]["voltage"][0]
    assert abs(seq["negative_rms"] - 200.0) < 1e-7
    assert abs(seq["zero_rms"] - 100.0) < 1e-7
    assert abs(seq["positive_rms"] - 100.0) < 1e-7


def test_sample_rate_change_remains_segmented_warning():
    from tests.synthetic import make_rate_change_chunks

    raw_chunks = make_rate_change_chunks()
    segments = group_chunks_by_rate(
        [{k: v for k, v in item.items() if k != "raw"} for item in raw_chunks],
        {0: raw_chunks[0]["raw"], 1: raw_chunks[1]["raw"]},
    )
    assert len(segments) == 2
    result = analyze_all_segments(segments, coefficients(), default_params(), sample_rate_changed=True)
    assert result["quality_status"] == "warning"
    assert any(item["code"] == "sample_rate_changed" for item in result["quality"])
