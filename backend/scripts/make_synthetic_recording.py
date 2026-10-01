from __future__ import annotations

"""Generate one synthetic manifest/raw chunks for manual API upload.

Example:
  python scripts/make_synthetic_recording.py /tmp/pq-demo
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.synthetic import make_chunks  # noqa: E402


def main(out_dir: str) -> None:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    chunks = make_chunks(sample_chunks=(300, 420))
    manifest_payload = {
        "name": "synthetic-harmonic-three-phase",
        "nominal_sample_rate": 6000,
        "expected_chunks": [{k: v for k, v in chunk.items() if k != "raw"} for chunk in chunks],
    }
    (target / "manifest.json").write_text(json.dumps(manifest_payload, indent=2, ensure_ascii=False))
    for chunk in chunks:
        (target / f"chunk-{chunk['sequence']:03d}.bin").write_bytes(chunk["raw"])
    print(target)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/pq-demo")
