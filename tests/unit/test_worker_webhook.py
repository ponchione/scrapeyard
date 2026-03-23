"""Tests for webhook dispatch wiring in scrape_task."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import FailStrategy, WebhookConfig, WebhookStatus
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.worker import scrape_task


def _make_job(job_id: str = "job-1") -> Job:
    return Job(
        job_id=job_id, project="test", name="test-job",
        config_yaml="", status=JobStatus.queued,
    )


def _make_target(url: str) -> MagicMock:
    target = MagicMock(url=url, proxy=None)
    target.fetcher.value = "basic"
    return target


def _make_config_mock(*, webhook: WebhookConfig | None = None):
    cfg = MagicMock()
    cfg.project = "test"
    cfg.name = "test-job"
    cfg.resolved_targets.return_value = [_make_target("http://a.com")]
    cfg.execution.concurrency = 1
    cfg.execution.delay_between = 0
    cfg.execution.domain_rate_limit = 0
    cfg.execution.fail_strategy = FailStrategy.partial
    cfg.adaptive = False
    cfg.schedule = None
    cfg.retry = MagicMock()
    cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
    cfg.output.group_by = "target"
    cfg.webhook = webhook
    cfg.proxy = None
    return cfg


@pytest.fixture
def mock_stores():
    job_store = AsyncMock()
    result_store = AsyncMock()
    error_store = AsyncMock()
    circuit_breaker = MagicMock()
    circuit_breaker.check = MagicMock()
    circuit_breaker.record_success = MagicMock()
    circuit_breaker.record_failure = MagicMock()
    return job_store, result_store, error_store, circuit_breaker


@pytest.mark.asyncio
async def test_webhook_dispatched_on_complete(mock_stores):
    """Webhook fires when config has webhook and status matches."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    webhook_dispatcher = AsyncMock()
    webhook_config = WebhookConfig(url="https://hooks.example.com/callback")

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )
        # Let any create_task webhooks run.
        await asyncio.sleep(0)

    webhook_dispatcher.dispatch.assert_called_once()
    call_args = webhook_dispatcher.dispatch.call_args
    assert call_args[0][0] is webhook_config
    payload = call_args[0][1]
    assert payload["event"] == "job.complete"
    assert payload["job_id"] == "job-1"


@pytest.mark.asyncio
async def test_no_webhook_when_not_configured(mock_stores):
    """No webhook attempt when config.webhook is None."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    webhook_dispatcher = AsyncMock()

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config_mock(webhook=None)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )
        await asyncio.sleep(0)

    webhook_dispatcher.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_no_webhook_when_dispatcher_is_none(mock_stores):
    """No webhook attempt when webhook_dispatcher is None (sync path)."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    webhook_config = WebhookConfig(url="https://hooks.example.com/callback")

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        # No webhook_dispatcher passed — should not crash
        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )


@pytest.mark.asyncio
async def test_webhook_status_not_in_on_list(mock_stores):
    """Webhook does NOT fire when status is not in config.on list."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    webhook_dispatcher = AsyncMock()
    # Only fire on "failed", but job will complete
    webhook_config = WebhookConfig(
        url="https://hooks.example.com/callback",
        on=[WebhookStatus.failed],
    )

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )
        await asyncio.sleep(0)

    webhook_dispatcher.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_fires_with_none_meta_on_empty_results(mock_stores):
    """Webhook fires with run_id=None when job has no data."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    webhook_dispatcher = AsyncMock()
    webhook_config = WebhookConfig(
        url="https://hooks.example.com/callback",
        on=[WebhookStatus.failed],
    )

    fail_result = TargetResult(
        url="http://a.com", status="failed", data=[], errors=["boom"]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = fail_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )
        await asyncio.sleep(0)

    webhook_dispatcher.dispatch.assert_called_once()
    payload = webhook_dispatcher.dispatch.call_args[0][1]
    assert payload["run_id"] is None
    assert payload["result_path"] is None
    assert payload["results_url"] is None
    assert payload["result_count"] == 0
    assert payload["event"] == "job.failed"
