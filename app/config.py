from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

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
    previous_service_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="CODELEAF_RUNNER_PREVIOUS_SERVICE_TOKEN"
    )
    docker_gateway_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="RUNNER_DOCKER_GATEWAY_TOKEN"
    )
    redis_url: str = "redis://localhost:6379/0"
    piston_base_url: str = "http://localhost:2000"

    max_queue_depth: int = Field(default=250, ge=1, le=10000, validation_alias="RUNNER_MAX_QUEUE_DEPTH")
    max_active_per_user: int = Field(default=2, ge=1, le=20, validation_alias="RUNNER_MAX_ACTIVE_PER_USER")
    max_jobs_per_minute: int = Field(default=8, ge=1, le=120, validation_alias="RUNNER_MAX_JOBS_PER_MINUTE")
    worker_concurrency: int = Field(default=2, ge=1, le=64, validation_alias="RUNNER_WORKER_CONCURRENCY")
    job_ttl_seconds: int = Field(default=86400, ge=300, le=604800, validation_alias="RUNNER_JOB_TTL_SECONDS")
    request_rate_limit_per_ip: int = Field(default=3000, ge=60, le=20000, validation_alias="RUNNER_REQUESTS_PER_MINUTE_PER_IP")
    request_rate_window_seconds: int = Field(default=60, ge=10, le=300, validation_alias="RUNNER_REQUEST_RATE_WINDOW_SECONDS")
    max_api_body_bytes: int = Field(default=2097152, ge=65536, le=4194304, validation_alias="RUNNER_MAX_API_BODY_BYTES")
    production_mode: bool = Field(default=False, validation_alias="RUNNER_PRODUCTION_MODE")
    allowed_websocket_origins_csv: str = Field(default="", validation_alias="RUNNER_ALLOWED_WEBSOCKET_ORIGINS")
    websocket_connections_per_ip_per_minute: int = Field(default=600, ge=1, le=10000, validation_alias="RUNNER_WEBSOCKET_CONNECTIONS_PER_MINUTE_PER_IP")

    max_files_per_job: int = Field(default=25, ge=1, le=25)
    max_file_bytes: int = Field(default=262144, ge=1024, le=1048576)
    max_total_source_bytes: int = Field(default=1048576, ge=1024, le=2097152)
    max_stdin_bytes: int = Field(default=65536, ge=0, le=262144)
    max_args: int = Field(default=16, ge=0, le=64)

    max_interactive_sessions_per_user: int = Field(
        default=1, ge=1, le=5, validation_alias="RUNNER_MAX_INTERACTIVE_SESSIONS_PER_USER"
    )
    interactive_init_timeout_seconds: float = Field(default=5.0, ge=1.0, le=15.0)
    interactive_input_idle_timeout_seconds: float = Field(default=90.0, ge=10.0, le=300.0)
    interactive_max_session_seconds: int = Field(default=120, ge=15, le=600)
    interactive_session_ttl_seconds: int = Field(default=180, ge=30, le=900)
    interactive_max_message_bytes: int = Field(default=32768, ge=1024, le=131072)
    interactive_max_input_message_bytes: int = Field(default=16384, ge=256, le=65536)
    interactive_max_total_input_bytes: int = Field(default=65536, ge=1024, le=262144)
    interactive_max_output_bytes: int = Field(default=65536, ge=1024, le=262144)
    interactive_max_messages: int = Field(default=256, ge=4, le=2048)
    max_arg_length: int = Field(default=256, ge=1, le=2048)

    piston_compile_timeout_ms: int = Field(default=30000, ge=1000, le=30000)
    piston_run_timeout_ms: int = Field(default=3000, ge=500, le=15000)
    piston_compile_cpu_time_ms: int = Field(default=30000, ge=1000, le=30000)
    piston_run_cpu_time_ms: int = Field(default=3000, ge=500, le=15000)
    piston_compile_memory_limit_bytes: int = Field(default=536870912, ge=33554432, le=1073741824)
    piston_run_memory_limit_bytes: int = Field(default=134217728, ge=16777216, le=536870912)
    piston_output_max_size: int = Field(default=65536, ge=1024, le=262144)
    piston_http_timeout_seconds: float = Field(default=45.0, ge=5.0, le=90.0)
    piston_connect_timeout_seconds: float = Field(default=3.0, ge=1.0, le=15.0)

    # Database execution is a separate queue and worker. SQLite is in-memory;
    # MySQL/PostgreSQL use one disposable container per job on an internal network.
    database_execution_enabled: bool = Field(default=True, validation_alias="DATABASE_EXECUTION_ENABLED")
    database_max_query_bytes: int = Field(default=65536, ge=256, le=131072, validation_alias="RUNNER_DATABASE_MAX_QUERY_BYTES")
    database_max_setup_bytes: int = Field(default=131072, ge=0, le=262144, validation_alias="RUNNER_DATABASE_MAX_SETUP_BYTES")
    database_max_rows: int = Field(default=100, ge=1, le=500, validation_alias="RUNNER_DATABASE_MAX_ROWS")
    database_max_result_bytes: int = Field(default=65536, ge=1024, le=262144, validation_alias="RUNNER_DATABASE_MAX_RESULT_BYTES")
    database_query_timeout_seconds: float = Field(default=3.0, ge=0.25, le=10.0, validation_alias="RUNNER_DATABASE_QUERY_TIMEOUT_SECONDS")
    database_startup_timeout_seconds: float = Field(default=20.0, ge=5.0, le=60.0, validation_alias="RUNNER_DATABASE_STARTUP_TIMEOUT_SECONDS")
    database_job_timeout_seconds: float = Field(default=35.0, ge=10.0, le=90.0, validation_alias="RUNNER_DATABASE_JOB_TIMEOUT_SECONDS")
    database_max_active_per_user: int = Field(default=2, ge=1, le=5, validation_alias="RUNNER_DATABASE_MAX_ACTIVE_PER_USER")
    database_jobs_per_minute: int = Field(default=6, ge=1, le=30, validation_alias="RUNNER_DATABASE_JOBS_PER_MINUTE")
    database_max_queue_depth: int = Field(default=100, ge=1, le=1000, validation_alias="RUNNER_DATABASE_MAX_QUEUE_DEPTH")
    database_job_ttl_seconds: int = Field(default=86400, ge=300, le=604800, validation_alias="RUNNER_DATABASE_JOB_TTL_SECONDS")
    database_worker_concurrency: int = Field(default=2, ge=1, le=8, validation_alias="RUNNER_DATABASE_WORKER_CONCURRENCY")
    database_docker_socket: str = Field(default="http://docker-gateway:2375", validation_alias="RUNNER_DATABASE_DOCKER_SOCKET")
    database_docker_api_version: str = Field(default="1.45", validation_alias="RUNNER_DATABASE_DOCKER_API_VERSION")
    database_worker_container_name: str = Field(default="codeleaf-runner-database-worker", validation_alias="RUNNER_DATABASE_WORKER_CONTAINER")
    database_postgres_image: str = Field(default="postgres:17.11-bookworm", validation_alias="RUNNER_DATABASE_POSTGRES_IMAGE")
    database_mysql_image: str = Field(default="mysql:8.4.13", validation_alias="RUNNER_DATABASE_MYSQL_IMAGE")

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

    @field_validator("allowed_websocket_origins_csv")
    @classmethod
    def validate_websocket_origins(cls, value: str) -> str:
        origins = [item.strip() for item in value.split(",") if item.strip()]
        normalized = []
        for origin in origins:
            parsed = urlsplit(origin)
            try:
                parsed.port  # Force validation of malformed/out-of-range ports.
            except ValueError as exc:
                raise ValueError("WebSocket origins must contain a valid port") from exc
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or "*" in origin
            ):
                raise ValueError("WebSocket origins must be exact http(s) origins without paths or wildcards")
            normalized.append(f"{parsed.scheme}://{parsed.netloc}")
        return ",".join(normalized)

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
    def previous_token_value(self) -> str:
        return self.previous_service_token.get_secret_value()

    @property
    def docker_gateway_token_value(self) -> str:
        return self.docker_gateway_token.get_secret_value()

    @property
    def service_tokens(self) -> tuple[str, ...]:
        return tuple(value for value in (self.token_value, self.previous_token_value) if value)

    @property
    def allowed_websocket_origins(self) -> frozenset[str]:
        return frozenset(
            item.strip().rstrip("/")
            for item in self.allowed_websocket_origins_csv.split(",")
            if item.strip()
        )

    @property
    def service_token_ready(self) -> bool:
        return len(self.token_value) >= 32 and (
            not self.previous_token_value or len(self.previous_token_value) >= 32
        )

    @property
    def docker_gateway_token_ready(self) -> bool:
        return len(self.docker_gateway_token_value) >= 32


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
