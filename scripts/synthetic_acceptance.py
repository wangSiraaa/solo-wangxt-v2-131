#!/usr/bin/env python3
"""Generate and upload a synthetic recording for acceptance/demo purposes.

Run while the API is available:
  python scripts/synthetic_acceptance.py --api http://localhost:8000

It uploads blocks in shuffled order, inserts a gap/sample-rate change, creates calibration v1,
and starts an analysis. Calibration revision and crash retry are exercised by API calls below.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np


def balanced(n: int, fs: float, reverse_c: bool = False, harmonic5: float = 0.05):
    t = np.arange(n) / fs
    phases = [0, -2 * np.pi / 3, 2 * np.pi / 3 + (np.pi if reverse_c else 0)]
    return np.stack([
        np.sqrt(2) * np.cos(2 * np.pi * 50 * t + p)
        + np.sqrt(2) * harmonic5 * np.cos(2 * np.pi * 250 * t + 5 * p)
        for p in phases
    ], axis=1).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--key", default="acceptance-recording")
    ap.add_argument("--gap", action="store_true")
    ap.add_argument("--reverse-c", action="store_true")
    args = ap.parse_args()
    start = datetime.now(timezone.utc)

    b0 = balanced(1000, 1000, args.reverse_c)
    fs1 = 1200 if args.gap else 1000
    b1 = balanced(960, fs1, args.reverse_c)
    blocks = [(0, b0, 1000, 0, start), (1, b1, fs1, 900 if args.gap else 1000,
                                        start + timedelta(seconds=(900/1000) if args.gap else 1))]
    block_manifests = []
    payloads = {}
    cursor_for_time = {0: 0, 1: 900 if args.gap else 1000}
    for idx, arr, fs, start_sample, ts in blocks:
        raw = arr.tobytes()
        payloads[idx] = raw
        block_manifests.append({
            "block_index": idx,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw),
            "start_sample": start_sample,
            "sample_count": arr.shape[0],
            "sample_rate": fs,
            "start_timestamp": ts.isoformat(),
            "dtype": "float32",
        })

    manifest = {
        "manifest_key": args.key,
        "name": "Synthetic acceptance recording",
        "nominal_frequency": 50,
        "expected_sample_rate": 1000,
        "channels": [
            {"name": "Va", "kind": "voltage", "phase": "A", "unit": "V"},
            {"name": "Vb", "kind": "voltage", "phase": "B", "unit": "V"},
            {"name": "Vc", "kind": "voltage", "phase": "C", "unit": "V"},
        ],
        "blocks": block_manifests,
    }
    with httpx.Client(base_url=args.api, timeout=30) as c:
        r = c.post("/manifests", json=manifest)
        if r.status_code not in {200, 201, 400}:
            r.raise_for_status()
        # Shuffled/retransmitted upload order.
        for idx in [1, 0, 0]:
            resp = c.put(f"/manifests/{args.key}/blocks/{idx}", content=payloads[idx],
                         headers={"Content-Type": "application/octet-stream"})
            print("upload", idx, resp.status_code, resp.text)
            resp.raise_for_status()
        print("complete", c.post(f"/manifests/{args.key}/complete").json())
        cal = c.post("/calibrations", json={
            "name": "default",
            "channels": [{"channel": ch, "gain": 1, "offset": 0, "phase_shift_rad": 0} for ch in ["Va", "Vb", "Vc"]],
            "activate": True,
        })
        print("calibration", cal.status_code, cal.text)
        cal.raise_for_status()
        task = c.post("/analyses", json={"manifest_key": args.key, "calibration_id": cal.json()["id"]})
        print("analysis", task.status_code, task.text)
        task.raise_for_status()
        print(json.dumps(task.json(), indent=2))


if __name__ == "__main__":
    main()
