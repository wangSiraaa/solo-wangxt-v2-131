from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///./test.db")
os.environ.setdefault("STORAGE_BACKEND", "local")


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_PATH", str(tmp_path / "objects"))
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")

    import app.core.config as config_module
    config_module.get_settings.cache_clear()

    from app.db import session as session_module
    from app.services import storage as storage_module
    storage_module.reset_store_cache()

    # Rebuild engine bound to the temporary database.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine(f"sqlite+pysqlite:///{db_path}", future=True, connect_args={"check_same_thread": False})
    session_module.engine = engine
    session_module.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    from app.models import orm  # noqa: F401
    session_module.Base.metadata.create_all(bind=engine)

    yield session_module.SessionLocal, storage_module.get_store()

    storage_module.reset_store_cache()
    config_module.get_settings.cache_clear()


def make_block_payload(arr: np.ndarray) -> bytes:
    return np.asarray(arr, dtype=np.float32).tobytes()


def build_manifest_payload(
    blocks: list[np.ndarray],
    *,
    key: str = "m-1",
    sample_rate: float | list[float] = 1000.0,
    start_sample: int = 0,
    channels: list[dict] | None = None,
    start_time: datetime | None = None,
):
    start_time = start_time or datetime(2026, 1, 1, tzinfo=timezone.utc)
    if channels is None:
        channels = [
            {"name": "Va", "kind": "voltage", "phase": "A", "unit": "V"},
            {"name": "Vb", "kind": "voltage", "phase": "B", "unit": "V"},
            {"name": "Vc", "kind": "voltage", "phase": "C", "unit": "V"},
        ]
    rate_list = sample_rate if isinstance(sample_rate, list) else [sample_rate] * len(blocks)
    block_items = []
    cursor = start_sample
    current_time = start_time
    for i, arr in enumerate(blocks):
        arr = np.asarray(arr, dtype=np.float32)
        data = arr.tobytes()
        rate = rate_list[i]
        block_items.append(
            {
                "block_index": i,
                "sha256": hashlib.sha256(data).hexdigest(),
                "byte_size": len(data),
                "start_sample": cursor,
                "sample_count": arr.shape[0],
                "sample_rate": rate,
                "start_timestamp": current_time.isoformat(),
                "dtype": "float32",
            }
        )
        cursor += arr.shape[0]
        current_time = current_time + timedelta(seconds=arr.shape[0] / rate)
    return {
        "manifest_key": key,
        "name": key,
        "nominal_frequency": 50.0,
        "expected_sample_rate": rate_list[0],
        "channels": channels,
        "blocks": block_items,
    }


def balanced_waveforms(n: int = 2000, fs: float = 1000.0, amp: float = np.sqrt(2), reverse_c: bool = False, harmonic5: float = 0.0):
    t = np.arange(n) / fs
    phases = [0.0, -2 * np.pi / 3, 2 * np.pi / 3]
    if reverse_c:
        phases[2] = phases[2] + np.pi  # physical C polarity reversed
    cols = []
    for phase in phases:
        cols.append(amp * np.cos(2 * np.pi * 50 * t + phase) + harmonic5 * amp * np.cos(2 * np.pi * 250 * t + 3 * phase))
    return np.stack(cols, axis=1)
