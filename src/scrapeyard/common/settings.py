"""Service-level settings read from environment variables via Pydantic BaseSettings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings

from scrapeyard.engine.proxy import normalize_proxy_url


class ServiceSettings(BaseSettings):
    """Central service configuration populated from environment variables.

    All variables are prefixed with ``SCRAPEYARD_`` and grouped by subsystem.
    """

    workers_max_concurrent: int = Field(default=4, ge=1)
    workers_max_browsers: int = Field(default=2, ge=1)
    workers_memory_limit_mb: int = Field(default=4096, ge=0)
    sync_timeout_seconds: int = Field(default=15, ge=0)
    sync_poll_delay_seconds: float = Field(default=0.5, gt=0)
    basic_fetch_timeout_seconds: float = Field(default=30.0, gt=0)
    workers_shutdown_grace_seconds: int = Field(default=30, ge=0)
    workers_cancellation_grace_seconds: float = Field(default=10.0, gt=0)
    workers_queued_claim_timeout_seconds: int = Field(default=300, gt=0)
    workers_running_heartbeat_timeout_seconds: int = Field(default=600, gt=0)
    workers_heartbeat_interval_seconds: int = Field(default=30, gt=0)
    workers_redis_connect_timeout_seconds: float = Field(default=10.0, gt=0)

    run_max_duration_seconds: float = Field(default=900.0, gt=0)
    run_max_fetched_bytes: int = Field(default=104857600, ge=1)
    run_max_extracted_records: int = Field(default=100000, ge=1)
    run_max_serialized_result_bytes: int = Field(default=52428800, ge=4096)
    run_max_browser_debug_bytes: int = Field(default=26214400, ge=1)
    transform_regex_timeout_seconds: float = Field(default=0.1, gt=0, le=5)
    transform_regex_max_pattern_bytes: int = Field(default=2048, ge=1, le=16384)

    redis_dsn: str = "redis://redis:6379/0"
    queue_name: str = "scrapeyard"

    admin_read_default_limit: int = Field(default=100, ge=1)
    admin_read_max_limit: int = Field(default=500, ge=1)

    idempotency_key_max_bytes: int = Field(default=128, ge=1, le=1024)
    idempotency_retention_hours: int = Field(default=24, ge=1)
    idempotency_cleanup_batch_size: int = Field(default=1000, ge=1)

    rate_limit_requests: int = Field(default=600, ge=0)
    rate_limit_window_seconds: int = Field(default=60, ge=0)

    scheduler_jitter_max_seconds: int = Field(default=120, ge=0)
    scheduler_misfire_grace_seconds: int = Field(default=60, ge=1)

    webhook_max_delivery_attempts: int = Field(default=5, ge=1)
    webhook_max_delivery_age_seconds: int = Field(default=86400, ge=1)
    webhook_dispatch_concurrency: int = Field(default=4, ge=1)
    webhook_dispatch_batch_size: int = Field(default=100, ge=1)
    webhook_delivered_retention_days: int = Field(default=7, ge=1)
    webhook_failed_retention_days: int = Field(default=30, ge=1)

    storage_retention_days: int = Field(default=30, ge=0)
    db_dir: str = "/data/db"
    storage_results_dir: str = "/data/results"
    storage_max_results_per_job: int = Field(default=100, ge=0)
    storage_orphan_grace_seconds: int = Field(default=86400, ge=1)
    storage_reconciliation_dry_run: bool = True
    storage_cleanup_interval_seconds: float = Field(default=21600.0, gt=0)
    adaptive_dir: str = "/data/adaptive"
    log_dir: str = "/data/logs"
    browser_debug_enabled: bool = False

    circuit_breaker_max_failures: int = Field(default=3, ge=1)
    circuit_breaker_cooldown_seconds: int = Field(default=300, ge=0)
    proxy_url: str = ""
    log_level: str = "INFO"
    domain_rate_limit_shared: bool = True

    api_keys: str = ""
    api_credentials: str = ""
    encryption_keys: str = Field(default="", repr=False)
    encryption_active_key_id: str = ""
    max_request_bytes: int = Field(default=262144, ge=0)
    health_disk_free_min_mb: int = Field(default=100, ge=0)
    health_include_projects: bool = False
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    metrics_refresh_interval_seconds: float = Field(default=5.0, ge=0, le=60)

    # Destructive release qualification only. A crash point also requires the
    # in-container runner sentinel; there is no remote trigger.
    qualification_mode: bool = False
    qualification_crash_point: str = ""
    qualification_marker_dir: str = "/run/scrapeyard-qualification"

    model_config = {"env_prefix": "SCRAPEYARD_"}

    @field_validator("proxy_url")
    @classmethod
    def _normalize_proxy_url(cls, value: str) -> str:
        if not value.strip():
            return ""
        return normalize_proxy_url(value)

    @model_validator(mode="after")
    def _validate_read_limits(self) -> ServiceSettings:
        if self.admin_read_default_limit > self.admin_read_max_limit:
            raise ValueError("admin_read_default_limit must be <= admin_read_max_limit")
        if (
            self.workers_heartbeat_interval_seconds * 3
            > self.workers_running_heartbeat_timeout_seconds
        ):
            raise ValueError(
                "workers_heartbeat_interval_seconds must be no more than one third "
                "of workers_running_heartbeat_timeout_seconds"
            )
        if self.webhook_dispatch_batch_size < self.webhook_dispatch_concurrency:
            raise ValueError(
                "webhook_dispatch_batch_size must be >= "
                "webhook_dispatch_concurrency"
            )
        if self.qualification_crash_point and not self.qualification_mode:
            raise ValueError(
                "qualification_crash_point requires qualification_mode=true"
            )
        if self.qualification_crash_point:
            from scrapeyard.common.qualification import QUALIFICATION_CRASH_POINTS

            if self.qualification_crash_point not in QUALIFICATION_CRASH_POINTS:
                raise ValueError(
                    "qualification_crash_point must name a supported local checkpoint"
                )
        return self

    def parsed_api_keys(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache(maxsize=1)
def get_settings() -> ServiceSettings:
    """Return a cached singleton :class:`ServiceSettings` instance."""
    return ServiceSettings()
