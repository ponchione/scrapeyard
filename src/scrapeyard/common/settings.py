"""Service-level settings read from environment variables via Pydantic BaseSettings."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class ServiceSettings(BaseSettings):
    """Central service configuration populated from environment variables.

    All variables are prefixed with ``SCRAPEYARD_`` and grouped by subsystem.
    """

    workers_max_concurrent: int = 4
    workers_max_browsers: int = 2
    workers_memory_limit_mb: int = 4096
    sync_timeout_seconds: int = 15
    sync_poll_delay_seconds: float = 0.5
    workers_shutdown_grace_seconds: int = 30
    workers_running_lease_seconds: int = 300

    redis_dsn: str = "redis://redis:6379/0"
    queue_name: str = "scrapeyard"

    admin_read_default_limit: int = 100
    admin_read_max_limit: int = 500

    scheduler_jitter_max_seconds: int = 120

    storage_retention_days: int = 30
    db_dir: str = "/data/db"
    storage_results_dir: str = "/data/results"
    storage_max_results_per_job: int = 100
    adaptive_dir: str = "/data/adaptive"
    log_dir: str = "/data/logs"
    browser_debug_enabled: bool = False
    browser_debug_artifacts_dir: str = "/data/browser-debug"

    circuit_breaker_max_failures: int = 3
    circuit_breaker_cooldown_seconds: int = 300
    proxy_url: str = ""
    log_level: str = "INFO"
    domain_rate_limit_shared: bool = True

    api_keys: str = ""
    max_request_bytes: int = 262144
    health_disk_free_min_mb: int = 100

    model_config = {"env_prefix": "SCRAPEYARD_"}

    def parsed_api_keys(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache(maxsize=1)
def get_settings() -> ServiceSettings:
    """Return a cached singleton :class:`ServiceSettings` instance."""
    return ServiceSettings()
