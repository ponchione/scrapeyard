"""Tests for webhook dispatch wiring in scrape_task."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import WebhookConfig, WebhookStatus
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.queue.worker import scrape_task
from tests.unit.worker_helpers import make_job, make_config_mock


@pytest.mark.asyncio
async def test_webhook_dispatched_on_complete(mock_stores):
    """Webhook fires when config has webhook and status matches."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job_status = AsyncMock()

    webhook_dispatcher = AsyncMock()
    webhook_config = WebhookConfig(url="https://hooks.example.com/callback")

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.submit.assert_awaited_once()
    call_args = webhook_dispatcher.submit.call_args
    assert call_args[0][0] is webhook_config
    payload = call_args[0][1]
    assert payload["event"] == "job.complete"
    assert payload["job_id"] == "job-1"


@pytest.mark.asyncio
async def test_no_webhook_when_not_configured(mock_stores):
    """No webhook attempt when config.webhook is None."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job_status = AsyncMock()

    webhook_dispatcher = AsyncMock()

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = make_config_mock(webhook=None)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.submit.assert_not_called()


@pytest.mark.asyncio
async def test_no_webhook_when_dispatcher_is_none(mock_stores):
    """No webhook attempt when webhook_dispatcher is None (sync path)."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job_status = AsyncMock()

    webhook_config = WebhookConfig(url="https://hooks.example.com/callback")

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = make_config_mock(webhook=webhook_config)
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
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job_status = AsyncMock()

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
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.submit.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_fires_with_save_meta_on_failed_results(mock_stores):
    """Webhook fires with save_meta fields when job fails (0 records)."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job_status = AsyncMock()

    # Worker always calls save_result, even on failure.  Configure
    # the mock so webhook payload assertions can check real values.
    result_store.save_result.return_value = MagicMock(
        run_id="fail-run-1", file_path="/tmp/results/fail", record_count=0,
    )

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
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = fail_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.submit.assert_awaited_once()
    payload = webhook_dispatcher.submit.call_args[0][1]
    assert payload["run_id"] == "fail-run-1"
    assert payload["result_path"] == "/tmp/results/fail"
    assert payload["result_count"] == 0
    assert payload["event"] == "job.failed"
