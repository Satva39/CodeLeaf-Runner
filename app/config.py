from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    service_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="CODELEAF_RUNNER_SERVICE_TOKEN"
    )
    redis_url: str = "redis://localhost:6379/0"
    piston_base_url: str = "http://localhost:2000"

    max_queue_depth: int = Field(default=250, ge=1, le=10000, validation_alias="RUNNER_MAX_QUEUE_DEPTH")
    max_active_per_user: int = Field(default=2, ge=1, le=20, validation_alias="RUNNER_MAX_ACTIVE_PER_USER")
    max_jobs_per_minute: int = Field(default=8, ge=1, le=120, validation_alias="RUNNER_MAX_JOBS_PER_MINUTE")
    worker_concurrency: int = Field(default=2, ge=1, le=64, validation_alias="RUNNER_WORKER_CONCURRENCY")
    job_ttl_seconds: int = Field(default=86400, ge=300, le=604800, validation_alias="RUNNER_JOB_TTL_SECONDS")

    max_files_per_job: int = Field(default=10, ge=1, le=25)
    max_file_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_total_source_bytes: int = Field(default=131072, ge=1024, le=2097152)
    max_stdin_bytes: int = Field(default=16384, ge=0, le=262144)
    max_args: int = Field(default=16, ge=0, le=64)
    max_arg_length: int = Field(default=256, ge=1, le=2048)

    piston_compile_timeout_ms: int = Field(default=10000, ge=1000, le=30000)
    piston_run_timeout_ms: int = Field(default=3000, ge=500, le=15000)
    piston_compile_cpu_time_ms: int = Field(default=10000, ge=1000, le=30000)
    piston_run_cpu_time_ms: int = Field(default=3000, ge=500, le=15000)
    piston_compile_memory_limit_bytes: int = Field(default=268435456, ge=33554432, le=1073741824)
    piston_run_memory_limit_bytes: int = Field(default=134217728, ge=16777216, le=536870912)
    piston_output_max_size: int = Field(default=16384, ge=1024, le=262144)
    piston_http_timeout_seconds: float = Field(default=22.0, ge=5.0, le=90.0)
    piston_connect_timeout_seconds: float = Field(default=3.0, ge=1.0, le=15.0)

    @field_validator("piston_base_url")
    @classmethod
    def normalize_piston_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must use redis://, rediss://, or unix://")
        return value

    @model_validator(mode="after")
    def validate_timeouts(self) -> "Settings":
        required_margin = (
            self.piston_compile_timeout_ms + self.piston_run_timeout_ms
        ) / 1000 + 3
        if self.piston_http_timeout_seconds < required_margin:
            raise ValueError(
                "PISTON_HTTP_TIMEOUT_SECONDS must exceed compile + run timeout by at least 3s"
            )
        return self

    @property
    def token_value(self) -> str:
        return self.service_token.get_secret_value()

    @property
    def service_token_ready(self) -> bool:
        return len(self.token_value) >= 32


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
