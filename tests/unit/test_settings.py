"""Unit tests for ServiceSettings and get_settings()."""

from __future__ import annotations

import os
from unittest.mock import patch

from scrapeyard.common.settings import ServiceSettings, get_settings


class TestServiceSettingsDefaults:
    """Verify all default values when no environment variables are set."""

    @staticmethod
    def _make_clean_settings(monkeypatch):
        """Create a ServiceSettings with directory env vars removed."""
        for key in (
            "SCRAPEYARD_DB_DIR",
            "SCRAPEYARD_LOG_DIR",
            "SCRAPEYARD_STORAGE_RESULTS_DIR",
            "SCRAPEYARD_ADAPTIVE_DIR",
        ):
            monkeypatch.delenv(key, raising=False)
        return ServiceSettings()

    def test_workers_max_concurrent_default(self):
        settings = ServiceSettings()
        assert settings.workers_max_concurrent == 4

    def test_workers_max_browsers_default(self):
        settings = ServiceSettings()
        assert settings.workers_max_browsers == 2

    def test_workers_memory_limit_mb_default(self):
        settings = ServiceSettings()
        assert settings.workers_memory_limit_mb == 4096

    def test_sync_poll_delay_seconds_default(self):
        settings = ServiceSettings()
        assert settings.sync_poll_delay_seconds == 0.5

    def test_scheduler_jitter_max_seconds_default(self):
        settings = ServiceSettings()
        assert settings.scheduler_jitter_max_seconds == 120

    def test_storage_retention_days_default(self):
        settings = ServiceSettings()
        assert settings.storage_retention_days == 30

    def test_storage_results_dir_default(self, monkeypatch):
        settings = self._make_clean_settings(monkeypatch)
        assert settings.storage_results_dir == "/data/results"

    def test_storage_max_results_per_job_default(self):
        settings = ServiceSettings()
        assert settings.storage_max_results_per_job == 100

    def test_db_dir_default(self, monkeypatch):
        settings = self._make_clean_settings(monkeypatch)
        assert settings.db_dir == "/data/db"

    def test_adaptive_dir_default(self, monkeypatch):
        settings = self._make_clean_settings(monkeypatch)
        assert settings.adaptive_dir == "/data/adaptive"

    def test_log_dir_default(self, monkeypatch):
        settings = self._make_clean_settings(monkeypatch)
        assert settings.log_dir == "/data/logs"

    def test_circuit_breaker_max_failures_default(self):
        settings = ServiceSettings()
        assert settings.circuit_breaker_max_failures == 3

    def test_circuit_breaker_cooldown_seconds_default(self):
        settings = ServiceSettings()
        assert settings.circuit_breaker_cooldown_seconds == 300


class TestServiceSettingsFromEnv:
    """Verify that environment variables override defaults."""

    def test_reads_workers_max_concurrent(self):
        with patch.dict(os.environ, {"SCRAPEYARD_WORKERS_MAX_CONCURRENT": "8"}):
            settings = ServiceSettings()
        assert settings.workers_max_concurrent == 8

    def test_reads_workers_max_browsers(self):
        with patch.dict(os.environ, {"SCRAPEYARD_WORKERS_MAX_BROWSERS": "5"}):
            settings = ServiceSettings()
        assert settings.workers_max_browsers == 5

    def test_reads_storage_results_dir(self):
        with patch.dict(os.environ, {"SCRAPEYARD_STORAGE_RESULTS_DIR": "/tmp/results"}):
            settings = ServiceSettings()
        assert settings.storage_results_dir == "/tmp/results"

    def test_reads_sync_poll_delay_seconds(self):
        with patch.dict(os.environ, {"SCRAPEYARD_SYNC_POLL_DELAY_SECONDS": "0.25"}):
            settings = ServiceSettings()
        assert settings.sync_poll_delay_seconds == 0.25

    def test_reads_circuit_breaker_cooldown_seconds(self):
        with patch.dict(os.environ, {"SCRAPEYARD_CIRCUIT_BREAKER_COOLDOWN_SECONDS": "600"}):
            settings = ServiceSettings()
        assert settings.circuit_breaker_cooldown_seconds == 600

    def test_reads_db_dir(self):
        with patch.dict(os.environ, {"SCRAPEYARD_DB_DIR": "/custom/db"}):
            settings = ServiceSettings()
        assert settings.db_dir == "/custom/db"


class TestGetSettings:
    """Verify singleton caching behavior of get_settings()."""

    def test_returns_service_settings_instance(self):
        get_settings.cache_clear()
        settings = get_settings()
        assert isinstance(settings, ServiceSettings)

    def test_returns_same_instance(self):
        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()
        assert first is second

    def test_cache_clear_creates_new_instance(self):
        get_settings.cache_clear()
        first = get_settings()
        get_settings.cache_clear()
        second = get_settings()
        assert first is not second


def test_proxy_url_defaults_to_empty(monkeypatch):
    """proxy_url defaults to empty string (no proxy)."""
    monkeypatch.delenv("SCRAPEYARD_PROXY_URL", raising=False)
    from scrapeyard.common.settings import ServiceSettings
    settings = ServiceSettings()
    assert settings.proxy_url == ""


def test_proxy_url_from_env(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_PROXY_URL", "http://gate.example.com:7777")
    from scrapeyard.common.settings import ServiceSettings
    settings = ServiceSettings()
    assert settings.proxy_url == "http://gate.example.com:7777"


def test_domain_rate_limit_shared_defaults_true(monkeypatch):
    monkeypatch.delenv("SCRAPEYARD_DOMAIN_RATE_LIMIT_SHARED", raising=False)
    from scrapeyard.common.settings import ServiceSettings
    settings = ServiceSettings()
    assert settings.domain_rate_limit_shared is True


def test_domain_rate_limit_shared_from_env(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_DOMAIN_RATE_LIMIT_SHARED", "false")
    from scrapeyard.common.settings import ServiceSettings
    settings = ServiceSettings()
    assert settings.domain_rate_limit_shared is False


class TestInitRateLimiter:
    """Verify init_rate_limiter selects the right implementation."""

    def test_returns_local_when_redis_is_none(self):
        from scrapeyard.api.dependencies import init_rate_limiter, reset_rate_limiter
        from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
        try:
            limiter = init_rate_limiter(redis=None)
            assert isinstance(limiter, LocalDomainRateLimiter)
        finally:
            reset_rate_limiter()

    def test_returns_local_when_shared_disabled(self, monkeypatch):
        monkeypatch.setenv("SCRAPEYARD_DOMAIN_RATE_LIMIT_SHARED", "false")
        from scrapeyard.api.dependencies import init_rate_limiter, reset_rate_limiter
        from scrapeyard.common.settings import get_settings
        from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
        from unittest.mock import MagicMock
        get_settings.cache_clear()
        try:
            limiter = init_rate_limiter(redis=MagicMock())
            assert isinstance(limiter, LocalDomainRateLimiter)
        finally:
            reset_rate_limiter()
            get_settings.cache_clear()

    def test_returns_redis_when_shared_and_redis_provided(self):
        from scrapeyard.api.dependencies import init_rate_limiter, reset_rate_limiter
        from scrapeyard.engine.rate_limiter import RedisDomainRateLimiter
        from unittest.mock import MagicMock
        try:
            limiter = init_rate_limiter(redis=MagicMock())
            assert isinstance(limiter, RedisDomainRateLimiter)
        finally:
            reset_rate_limiter()
