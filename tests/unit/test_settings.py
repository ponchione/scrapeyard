"""Unit tests for ServiceSettings and get_settings()."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from pydantic import ValidationError

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

    def test_basic_fetch_timeout_seconds_default(self):
        settings = ServiceSettings()
        assert settings.basic_fetch_timeout_seconds == 30.0

    def test_workers_redis_connect_timeout_seconds_default(self):
        settings = ServiceSettings()
        assert settings.workers_redis_connect_timeout_seconds == 10.0

    def test_worker_lease_defaults_are_explicit_and_independent(self):
        settings = ServiceSettings()
        assert settings.workers_cancellation_grace_seconds == 10.0
        assert settings.workers_queued_claim_timeout_seconds == 300
        assert settings.workers_queue_payload_ttl_seconds == 604800
        assert settings.workers_queued_reconciliation_interval_seconds == 60.0
        assert settings.workers_queued_reconciliation_batch_size == 100
        assert settings.workers_running_heartbeat_timeout_seconds == 600
        assert settings.workers_heartbeat_interval_seconds == 30

    def test_aggregate_run_budget_defaults(self):
        settings = ServiceSettings()
        assert settings.run_max_duration_seconds == 900.0
        assert settings.run_max_fetched_bytes == 104857600
        assert settings.run_max_extracted_records == 100000
        assert settings.run_max_serialized_result_bytes == 52428800
        assert settings.run_max_browser_debug_bytes == 26214400

    def test_regex_safety_defaults(self):
        settings = ServiceSettings()
        assert settings.transform_regex_timeout_seconds == 0.1
        assert settings.transform_regex_max_pattern_bytes == 2048
        assert settings.transform_max_pipeline_steps == 32
        assert settings.transform_max_value_bytes == 1048576

    def test_scheduler_jitter_max_seconds_default(self):
        settings = ServiceSettings()
        assert settings.scheduler_jitter_max_seconds == 120

    def test_webhook_retry_and_retention_defaults(self):
        settings = ServiceSettings()
        assert settings.webhook_max_delivery_attempts == 5
        assert settings.webhook_max_delivery_age_seconds == 86400
        assert settings.webhook_dispatch_concurrency == 4
        assert settings.webhook_dispatch_batch_size == 100
        assert settings.webhook_client_cache_max_size == 64
        assert settings.webhook_client_cache_idle_ttl_seconds == 300.0
        assert settings.webhook_delivered_retention_days == 7
        assert settings.webhook_failed_retention_days == 30

    def test_admin_read_default_limit_default(self):
        settings = ServiceSettings()
        assert settings.admin_read_default_limit == 100

    def test_admin_read_max_limit_default(self):
        settings = ServiceSettings()
        assert settings.admin_read_max_limit == 500

    def test_rate_limit_requests_default(self):
        settings = ServiceSettings()
        assert settings.rate_limit_requests == 600

    def test_rate_limit_window_seconds_default(self):
        settings = ServiceSettings()
        assert settings.rate_limit_window_seconds == 60

    def test_storage_retention_days_default(self):
        settings = ServiceSettings()
        assert settings.storage_retention_days == 30

    def test_history_retention_defaults(self):
        settings = ServiceSettings()
        assert settings.history_adhoc_job_retention_days == 30
        assert settings.history_scheduled_run_retention_days == 30
        assert settings.history_scheduled_run_retention_count == 100
        assert settings.history_error_retention_days == 30
        assert settings.history_webhook_tombstone_retention_days == 30
        assert settings.history_adhoc_job_cleanup_batch_size == 100
        assert settings.history_scheduled_run_cleanup_batch_size == 500
        assert settings.history_error_cleanup_batch_size == 1000

    def test_storage_results_dir_default(self, monkeypatch):
        settings = self._make_clean_settings(monkeypatch)
        assert settings.storage_results_dir == "/data/results"

    def test_storage_max_results_per_job_default(self):
        settings = ServiceSettings()
        assert settings.storage_max_results_per_job == 100
        assert settings.storage_cleanup_batch_size == 500

    @pytest.mark.parametrize(
        "field",
        ["storage_retention_days", "storage_max_results_per_job"],
    )
    def test_result_retention_settings_reject_zero(self, field):
        with pytest.raises(ValidationError):
            ServiceSettings(**{field: 0})

    def test_storage_reconciliation_defaults(self):
        settings = ServiceSettings()
        assert settings.storage_orphan_grace_seconds == 86400
        assert settings.storage_reconciliation_dry_run is True

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
        assert settings.circuit_breaker_max_domains == 10000
        assert settings.circuit_breaker_inactive_ttl_seconds == 3600.0

    def test_running_reconciliation_defaults(self):
        settings = ServiceSettings()
        assert settings.workers_running_reconciliation_interval_seconds == 60.0
        assert settings.workers_running_reconciliation_batch_size == 100

    def test_health_include_projects_default(self):
        settings = ServiceSettings()
        assert settings.health_include_projects is False


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

    def test_reads_storage_reconciliation_settings(self):
        values = {
            "SCRAPEYARD_STORAGE_ORPHAN_GRACE_SECONDS": "3600",
            "SCRAPEYARD_STORAGE_RECONCILIATION_DRY_RUN": "false",
        }
        with patch.dict(os.environ, values):
            settings = ServiceSettings()
        assert settings.storage_orphan_grace_seconds == 3600
        assert settings.storage_reconciliation_dry_run is False

    def test_rejects_non_positive_storage_orphan_grace(self):
        with pytest.raises(ValidationError):
            ServiceSettings(storage_orphan_grace_seconds=0)

    def test_reads_admin_read_default_limit(self):
        with patch.dict(os.environ, {"SCRAPEYARD_ADMIN_READ_DEFAULT_LIMIT": "25"}):
            settings = ServiceSettings()
        assert settings.admin_read_default_limit == 25

    def test_reads_admin_read_max_limit(self):
        with patch.dict(os.environ, {"SCRAPEYARD_ADMIN_READ_MAX_LIMIT": "250"}):
            settings = ServiceSettings()
        assert settings.admin_read_max_limit == 250

    def test_reads_rate_limit_requests(self):
        with patch.dict(os.environ, {"SCRAPEYARD_RATE_LIMIT_REQUESTS": "42"}):
            settings = ServiceSettings()
        assert settings.rate_limit_requests == 42

    def test_reads_rate_limit_window_seconds(self):
        with patch.dict(os.environ, {"SCRAPEYARD_RATE_LIMIT_WINDOW_SECONDS": "15"}):
            settings = ServiceSettings()
        assert settings.rate_limit_window_seconds == 15

    def test_reads_sync_poll_delay_seconds(self):
        with patch.dict(os.environ, {"SCRAPEYARD_SYNC_POLL_DELAY_SECONDS": "0.25"}):
            settings = ServiceSettings()
        assert settings.sync_poll_delay_seconds == 0.25

    def test_reads_basic_fetch_timeout_seconds(self):
        with patch.dict(os.environ, {"SCRAPEYARD_BASIC_FETCH_TIMEOUT_SECONDS": "12.5"}):
            settings = ServiceSettings()
        assert settings.basic_fetch_timeout_seconds == 12.5

    def test_reads_workers_redis_connect_timeout_seconds(self):
        with patch.dict(os.environ, {"SCRAPEYARD_WORKERS_REDIS_CONNECT_TIMEOUT_SECONDS": "7.0"}):
            settings = ServiceSettings()
        assert settings.workers_redis_connect_timeout_seconds == 7.0

    def test_reads_worker_lease_settings(self):
        values = {
            "SCRAPEYARD_WORKERS_CANCELLATION_GRACE_SECONDS": "7.5",
            "SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS": "45",
            "SCRAPEYARD_WORKERS_QUEUE_PAYLOAD_TTL_SECONDS": "7200",
            "SCRAPEYARD_WORKERS_QUEUED_RECONCILIATION_INTERVAL_SECONDS": "15",
            "SCRAPEYARD_WORKERS_QUEUED_RECONCILIATION_BATCH_SIZE": "25",
            "SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS": "180",
            "SCRAPEYARD_WORKERS_HEARTBEAT_INTERVAL_SECONDS": "20",
        }
        with patch.dict(os.environ, values):
            settings = ServiceSettings()
        assert settings.workers_cancellation_grace_seconds == 7.5
        assert settings.workers_queued_claim_timeout_seconds == 45
        assert settings.workers_queue_payload_ttl_seconds == 7200
        assert settings.workers_queued_reconciliation_interval_seconds == 15
        assert settings.workers_queued_reconciliation_batch_size == 25
        assert settings.workers_running_heartbeat_timeout_seconds == 180
        assert settings.workers_heartbeat_interval_seconds == 20

    def test_reads_aggregate_run_budgets(self):
        values = {
            "SCRAPEYARD_RUN_MAX_DURATION_SECONDS": "45.5",
            "SCRAPEYARD_RUN_MAX_FETCHED_BYTES": "123456",
            "SCRAPEYARD_RUN_MAX_EXTRACTED_RECORDS": "321",
            "SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES": "8192",
            "SCRAPEYARD_RUN_MAX_BROWSER_DEBUG_BYTES": "4096",
        }
        with patch.dict(os.environ, values):
            settings = ServiceSettings()
        assert settings.run_max_duration_seconds == 45.5
        assert settings.run_max_fetched_bytes == 123456
        assert settings.run_max_extracted_records == 321
        assert settings.run_max_serialized_result_bytes == 8192
        assert settings.run_max_browser_debug_bytes == 4096

    def test_reads_regex_safety_settings(self):
        values = {
            "SCRAPEYARD_TRANSFORM_REGEX_TIMEOUT_SECONDS": "0.25",
            "SCRAPEYARD_TRANSFORM_REGEX_MAX_PATTERN_BYTES": "1024",
            "SCRAPEYARD_TRANSFORM_MAX_PIPELINE_STEPS": "12",
            "SCRAPEYARD_TRANSFORM_MAX_VALUE_BYTES": "65536",
        }
        with patch.dict(os.environ, values):
            settings = ServiceSettings()
        assert settings.transform_regex_timeout_seconds == 0.25
        assert settings.transform_regex_max_pattern_bytes == 1024
        assert settings.transform_max_pipeline_steps == 12
        assert settings.transform_max_value_bytes == 65536

    def test_reads_webhook_retry_and_retention_settings(self):
        values = {
            "SCRAPEYARD_WEBHOOK_MAX_DELIVERY_ATTEMPTS": "7",
            "SCRAPEYARD_WEBHOOK_MAX_DELIVERY_AGE_SECONDS": "7200",
            "SCRAPEYARD_WEBHOOK_DISPATCH_CONCURRENCY": "3",
            "SCRAPEYARD_WEBHOOK_DISPATCH_BATCH_SIZE": "25",
            "SCRAPEYARD_WEBHOOK_CLIENT_CACHE_MAX_SIZE": "9",
            "SCRAPEYARD_WEBHOOK_CLIENT_CACHE_IDLE_TTL_SECONDS": "45",
            "SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS": "2",
            "SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS": "14",
        }
        with patch.dict(os.environ, values):
            settings = ServiceSettings()
        assert settings.webhook_max_delivery_attempts == 7
        assert settings.webhook_max_delivery_age_seconds == 7200
        assert settings.webhook_dispatch_concurrency == 3
        assert settings.webhook_dispatch_batch_size == 25
        assert settings.webhook_client_cache_max_size == 9
        assert settings.webhook_client_cache_idle_ttl_seconds == 45.0
        assert settings.webhook_delivered_retention_days == 2
        assert settings.webhook_failed_retention_days == 14

    def test_reads_result_cleanup_batch_size(self):
        with patch.dict(
            os.environ,
            {"SCRAPEYARD_STORAGE_CLEANUP_BATCH_SIZE": "37"},
        ):
            settings = ServiceSettings()

        assert settings.storage_cleanup_batch_size == 37

    def test_reads_cleanup_cycle_budgets(self):
        values = {
            "SCRAPEYARD_STORAGE_CLEANUP_CYCLE_MAX_ITEMS_PER_PHASE": "1234",
            "SCRAPEYARD_STORAGE_CLEANUP_CYCLE_MAX_SECONDS": "45",
            "SCRAPEYARD_STORAGE_CLEANUP_CATCHUP_DELAY_SECONDS": "2.5",
        }
        with patch.dict(os.environ, values):
            settings = ServiceSettings()

        assert settings.storage_cleanup_cycle_max_items_per_phase == 1234
        assert settings.storage_cleanup_cycle_max_seconds == 45.0
        assert settings.storage_cleanup_catchup_delay_seconds == 2.5

    def test_reads_run_thread_worker_capacity(self):
        with patch.dict(
            os.environ,
            {"SCRAPEYARD_RUN_THREAD_MAX_WORKERS": "7"},
        ):
            settings = ServiceSettings()

        assert settings.run_thread_max_workers == 7

    def test_reads_history_retention_settings(self):
        values = {
            "SCRAPEYARD_HISTORY_ADHOC_JOB_RETENTION_DAYS": "11",
            "SCRAPEYARD_HISTORY_SCHEDULED_RUN_RETENTION_DAYS": "12",
            "SCRAPEYARD_HISTORY_SCHEDULED_RUN_RETENTION_COUNT": "13",
            "SCRAPEYARD_HISTORY_ERROR_RETENTION_DAYS": "14",
            "SCRAPEYARD_HISTORY_WEBHOOK_TOMBSTONE_RETENTION_DAYS": "15",
            "SCRAPEYARD_HISTORY_ADHOC_JOB_CLEANUP_BATCH_SIZE": "16",
            "SCRAPEYARD_HISTORY_SCHEDULED_RUN_CLEANUP_BATCH_SIZE": "17",
            "SCRAPEYARD_HISTORY_ERROR_CLEANUP_BATCH_SIZE": "18",
        }

        with patch.dict(os.environ, values):
            settings = ServiceSettings()

        assert settings.history_adhoc_job_retention_days == 11
        assert settings.history_scheduled_run_retention_days == 12
        assert settings.history_scheduled_run_retention_count == 13
        assert settings.history_error_retention_days == 14
        assert settings.history_webhook_tombstone_retention_days == 15
        assert settings.history_adhoc_job_cleanup_batch_size == 16
        assert settings.history_scheduled_run_cleanup_batch_size == 17
        assert settings.history_error_cleanup_batch_size == 18

    def test_reads_circuit_breaker_cooldown_seconds(self):
        with patch.dict(os.environ, {"SCRAPEYARD_CIRCUIT_BREAKER_COOLDOWN_SECONDS": "600"}):
            settings = ServiceSettings()
        assert settings.circuit_breaker_cooldown_seconds == 600

    def test_reads_db_dir(self):
        with patch.dict(os.environ, {"SCRAPEYARD_DB_DIR": "/custom/db"}):
            settings = ServiceSettings()
        assert settings.db_dir == "/custom/db"

    def test_reads_health_include_projects(self):
        with patch.dict(os.environ, {"SCRAPEYARD_HEALTH_INCLUDE_PROJECTS": "true"}):
            settings = ServiceSettings()
        assert settings.health_include_projects is True


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
    monkeypatch.setenv("SCRAPEYARD_PROXY_URL", " http://93.184.216.34:7777 ")
    from scrapeyard.common.settings import ServiceSettings

    settings = ServiceSettings()
    assert settings.proxy_url == "http://93.184.216.34:7777"


def test_proxy_url_from_env_rejects_private_runtime_endpoint(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_PROXY_URL", "http://127.0.0.1:8080")

    with pytest.raises(ValidationError, match="non-public"):
        ServiceSettings()


def test_proxy_url_from_env_rejects_invalid_url(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_PROXY_URL", "file:///tmp/proxy.sock")

    with pytest.raises(ValidationError, match="Proxy URL scheme"):
        ServiceSettings()


@pytest.mark.parametrize(
    ("proxy_url", "message"),
    [
        ("http://gate.example.com/a path", "whitespace"),
        ("http://gate.example.com\\@127.0.0.1:8080", "backslashes"),
        ("http://%31%32%37.0.0.1:8080", "percent escapes"),
    ],
)
def test_proxy_url_from_env_rejects_ambiguous_url_syntax(monkeypatch, proxy_url, message):
    monkeypatch.setenv("SCRAPEYARD_PROXY_URL", proxy_url)

    with pytest.raises(ValidationError, match=message):
        ServiceSettings()


def test_log_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("SCRAPEYARD_LOG_LEVEL", raising=False)
    from scrapeyard.common.settings import ServiceSettings

    settings = ServiceSettings()
    assert settings.log_level == "INFO"


def test_log_level_from_env(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_LOG_LEVEL", "debug")
    from scrapeyard.common.settings import ServiceSettings

    settings = ServiceSettings()
    assert settings.log_level == "debug"


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


def test_admin_read_default_limit_cannot_exceed_max(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_ADMIN_READ_DEFAULT_LIMIT", "50")
    monkeypatch.setenv("SCRAPEYARD_ADMIN_READ_MAX_LIMIT", "25")

    with pytest.raises(ValidationError, match="admin_read_default_limit"):
        ServiceSettings()


@pytest.mark.parametrize(
    "name",
    [
        "WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS",
        "WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS",
        "WORKERS_HEARTBEAT_INTERVAL_SECONDS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_worker_lease_settings_reject_non_positive_values(
    monkeypatch,
    name,
    value,
):
    monkeypatch.setenv(f"SCRAPEYARD_{name}", value)

    with pytest.raises(ValidationError):
        ServiceSettings()


def test_heartbeat_interval_must_be_safely_shorter_than_running_timeout(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_WORKERS_RUNNING_HEARTBEAT_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("SCRAPEYARD_WORKERS_HEARTBEAT_INTERVAL_SECONDS", "31")

    with pytest.raises(ValidationError, match="one third"):
        ServiceSettings()


def test_queue_payload_ttl_must_cover_claim_and_reconciliation_window(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv(
        "SCRAPEYARD_WORKERS_QUEUED_RECONCILIATION_INTERVAL_SECONDS",
        "60",
    )
    monkeypatch.setenv("SCRAPEYARD_WORKERS_QUEUE_PAYLOAD_TTL_SECONDS", "360")

    with pytest.raises(ValidationError, match="payload_ttl"):
        ServiceSettings()


@pytest.mark.parametrize(
    "name",
    [
        "WEBHOOK_MAX_DELIVERY_ATTEMPTS",
        "WEBHOOK_MAX_DELIVERY_AGE_SECONDS",
        "WEBHOOK_DISPATCH_CONCURRENCY",
        "WEBHOOK_DISPATCH_BATCH_SIZE",
        "WEBHOOK_DELIVERED_RETENTION_DAYS",
        "WEBHOOK_FAILED_RETENTION_DAYS",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1"])
def test_webhook_retry_settings_reject_non_positive_values(monkeypatch, name, value):
    monkeypatch.setenv(f"SCRAPEYARD_{name}", value)

    with pytest.raises(ValidationError):
        ServiceSettings()


def test_webhook_batch_size_must_cover_dispatch_concurrency(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_WEBHOOK_DISPATCH_CONCURRENCY", "5")
    monkeypatch.setenv("SCRAPEYARD_WEBHOOK_DISPATCH_BATCH_SIZE", "4")

    with pytest.raises(ValidationError, match="batch_size"):
        ServiceSettings()


@pytest.mark.parametrize(
    "field",
    [
        "history_adhoc_job_retention_days",
        "history_scheduled_run_retention_days",
        "history_scheduled_run_retention_count",
        "history_error_retention_days",
        "history_webhook_tombstone_retention_days",
        "history_adhoc_job_cleanup_batch_size",
        "history_scheduled_run_cleanup_batch_size",
        "history_error_cleanup_batch_size",
    ],
)
def test_history_retention_settings_reject_zero(field):
    with pytest.raises(ValidationError):
        ServiceSettings(**{field: 0})


def test_get_settings_cache_reset_applies_worker_lease_environment(monkeypatch):
    get_settings.cache_clear()
    assert get_settings().workers_queued_claim_timeout_seconds == 300
    monkeypatch.setenv("SCRAPEYARD_WORKERS_QUEUED_CLAIM_TIMEOUT_SECONDS", "12")
    get_settings.cache_clear()

    assert get_settings().workers_queued_claim_timeout_seconds == 12


def test_secret_reference_allowlist_defaults_to_deny_all():
    settings = ServiceSettings(secret_reference_allowlist="")

    assert settings.parsed_secret_reference_allowlist() == {}


def test_secret_reference_allowlist_parses_project_and_shared_names():
    settings = ServiceSettings(
        secret_reference_allowlist=(
            '{"catalog":["SCRAPEYARD_SECRET_PROXY"],"*":["SCRAPEYARD_SECRET_SHARED"]}'
        )
    )

    assert settings.parsed_secret_reference_allowlist() == {
        "catalog": frozenset({"SCRAPEYARD_SECRET_PROXY"}),
        "*": frozenset({"SCRAPEYARD_SECRET_SHARED"}),
    }


@pytest.mark.parametrize(
    "raw",
    [
        "[]",
        '{"catalog":"SCRAPEYARD_SECRET_PROXY"}',
        '{"catalog":["NOT_A_SECRET"]}',
        '{"../catalog":["SCRAPEYARD_SECRET_PROXY"]}',
        '{"catalog":["SCRAPEYARD_SECRET_PROXY","SCRAPEYARD_SECRET_PROXY"]}',
    ],
)
def test_secret_reference_allowlist_rejects_invalid_policies(raw):
    with pytest.raises(ValidationError, match="SECRET_REFERENCE_ALLOWLIST|secret-reference"):
        ServiceSettings(secret_reference_allowlist=raw)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RUN_MAX_DURATION_SECONDS", "0"),
        ("RUN_MAX_DURATION_SECONDS", "-1"),
        ("RUN_MAX_FETCHED_BYTES", "0"),
        ("RUN_MAX_EXTRACTED_RECORDS", "0"),
        ("RUN_MAX_SERIALIZED_RESULT_BYTES", "4095"),
        ("RUN_MAX_BROWSER_DEBUG_BYTES", "0"),
        ("TRANSFORM_REGEX_TIMEOUT_SECONDS", "0"),
        ("TRANSFORM_REGEX_MAX_PATTERN_BYTES", "0"),
        ("TRANSFORM_MAX_PIPELINE_STEPS", "0"),
        ("TRANSFORM_MAX_VALUE_BYTES", "1023"),
    ],
)
def test_aggregate_run_budgets_reject_nonsensical_values(monkeypatch, name, value):
    monkeypatch.setenv(f"SCRAPEYARD_{name}", value)

    with pytest.raises(ValidationError):
        ServiceSettings()


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
