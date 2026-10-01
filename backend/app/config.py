from __future__ import annotations

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PQ_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./pq_review.db"
    redis_url: str = "redis://localhost:6379/0"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "pq-immutable-raw"
    object_store: str = "auto"  # auto|minio|memory
    spool_dir: str = "/tmp/pq-uploads"
    task_lock_seconds: int = 900
    max_spool_bytes: int = 512 * 1024 * 1024
    cors_origins: str = "http://localhost:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()
