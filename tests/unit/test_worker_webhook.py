"""Tests for webhook dispatch wiring in scrape_task."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import WebhookConfig, WebhookStatus
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.worker import scrape_task
from tests.unit.worker_helpers import (
    finalized_status,
    make_config_mock,
    make_job,
    make_settings_mock,
)


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
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.notify.assert_awaited_once_with()
    durable = job_store.finalize_owned_run.await_args.kwargs["webhook_delivery"]
    payload = durable.payload
    assert payload["event"] == "job.complete"
    assert payload["job_id"] == "job-1"
    assert durable is not None
    assert durable.delivery_id == payload["delivery_id"]
    assert durable.payload == payload


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
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(webhook=None)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.notify.assert_not_called()
    assert job_store.finalize_owned_run.await_args.kwargs["webhook_delivery"] is None


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
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        # No webhook_dispatcher passed — should not crash
        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    durable = job_store.finalize_owned_run.await_args.kwargs["webhook_delivery"]
    assert durable is not None
    assert durable.event == "job.complete"


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
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.notify.assert_not_called()
    assert job_store.finalize_owned_run.await_args.kwargs["webhook_delivery"] is None


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
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = fail_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    webhook_dispatcher.notify.assert_awaited_once_with()
    finalization = job_store.finalize_owned_run.await_args
    assert finalization is not None
    payload = finalization.kwargs["webhook_delivery"].payload
    assert payload["run_id"] == finalization.args[1]
    assert payload["result_path"] == "/tmp/results/fail"
    assert payload["result_count"] == 0
    assert payload["event"] == "job.failed"


@pytest.mark.asyncio
async def test_webhook_notify_failure_does_not_fail_successful_job(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job_status = AsyncMock()
    result_store.save_result.return_value = MagicMock(
        run_id="run-1", file_path="/tmp/results/run-1", record_count=1,
    )

    webhook_dispatcher = AsyncMock()
    webhook_dispatcher.notify.side_effect = RuntimeError("dispatcher wake failed")
    webhook_config = WebhookConfig(url="https://hooks.example.com/callback")
    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    assert finalized_status(job_store) == JobStatus.complete
    durable = job_store.finalize_owned_run.await_args.kwargs["webhook_delivery"]
    assert durable is not None
    assert durable.payload["delivery_id"] == durable.delivery_id


@pytest.mark.asyncio
async def test_fault_after_result_save_uses_atomic_crash_intent(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job.return_value = make_job(current_run_id="run-1")
    job_store.finalize_owned_run.side_effect = RuntimeError("terminal transaction fault")
    error_store.count_errors_for_run.return_value = 2
    result_store.save_result.return_value = MagicMock(
        run_id="run-1",
        file_path="/tmp/results/run-1",
        record_count=1,
    )
    webhook_dispatcher = AsyncMock()
    webhook_config = WebhookConfig(
        url="https://hooks.example.com/callback",
        on=[WebhookStatus.complete, WebhookStatus.failed],
    )
    success_result = TargetResult(
        url="http://a.com",
        status="success",
        data=[{"title": "A"}],
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, patch(
        "scrapeyard.queue.worker.scrape_target"
    ) as mock_scrape, patch(
        "scrapeyard.queue.worker.get_settings"
    ) as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(webhook=webhook_config)
        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1",
            "yaml",
            run_id="run-1",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=webhook_dispatcher,
        )

    result_store.save_result.assert_awaited_once()
    result_store.delete_result.assert_awaited_once_with("job-1", "run-1")
    job_store.finalize_owned_run.assert_awaited_once()
    complete_intent = job_store.finalize_owned_run.await_args.kwargs[
        "webhook_delivery"
    ]
    assert complete_intent.event == "job.complete"
    job_store.fail_owned_run.assert_awaited_once()
    failed_intent = job_store.fail_owned_run.await_args.kwargs["webhook_delivery"]
    assert failed_intent.event == "job.failed"
    assert failed_intent.payload["error_count"] == 2
    webhook_dispatcher.notify.assert_awaited_once_with()
