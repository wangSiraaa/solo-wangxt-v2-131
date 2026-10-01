from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+pysqlite:///./pq.db"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_region: str = "us-east-1"
    s3_bucket: str = "pq-raw-blocks"
    storage_backend: str = "s3"  # set to local for lightweight tests
    local_storage_path: str = "/tmp/pq-raw-blocks"

    nominal_frequency: float = 50.0
    worker_heartbeat_timeout_seconds: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
