"""Tests for validation.on_empty behavior in scrape_task."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import FailStrategy, OnEmptyAction
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ActionTaken, Job, JobStatus
from scrapeyard.queue.worker import scrape_task


def _make_job(job_id: str = "job-1") -> Job:
    return Job(
        job_id=job_id,
        project="test",
        name="test-job",
        config_yaml="",
        status=JobStatus.queued,
    )


def _make_target(url: str) -> MagicMock:
    target = MagicMock(url=url, proxy=None)
    target.fetcher.value = "basic"
    return target


def _make_config(*targets: MagicMock, on_empty: OnEmptyAction):
    cfg = MagicMock()
    cfg.project = "test"
    cfg.name = "test-job"
    cfg.resolved_targets.return_value = list(targets)
    cfg.execution.concurrency = 1
    cfg.execution.delay_between = 0
    cfg.execution.domain_rate_limit = 0
    cfg.execution.fail_strategy = FailStrategy.partial
    cfg.adaptive = False
    cfg.schedule = None
    cfg.retry = MagicMock()
    cfg.validation = MagicMock(required_fields=["title"], min_results=1, on_empty=on_empty)
    cfg.output.group_by = "target"
    cfg.webhook = None
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
async def test_validation_warn_keeps_data_and_completes(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=_make_job())
    job_store.update_job = AsyncMock()

    result = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config(_make_target("http://a.com"), on_empty=OnEmptyAction.warn)
        mock_scrape.return_value = result

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.complete
    result_store.save_result.assert_called_once()
    error = error_store.log_error.call_args[0][0]
    assert error.action_taken == ActionTaken.warn


@pytest.mark.asyncio
async def test_validation_skip_discards_invalid_target_but_keeps_job_complete(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=_make_job())
    job_store.update_job = AsyncMock()

    invalid = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])
    valid = TargetResult(url="http://b.com", status="success", data=[{"title": "ok"}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config(
            _make_target("http://a.com"),
            _make_target("http://b.com"),
            on_empty=OnEmptyAction.skip,
        )
        mock_scrape.side_effect = [invalid, valid]

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.complete
    result_store.save_result.assert_called_once()
    assert result_store.save_result.call_args.kwargs["record_count"] == 1
    error = error_store.log_error.call_args[0][0]
    assert error.action_taken == ActionTaken.skip


@pytest.mark.asyncio
async def test_validation_fail_marks_target_failed(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=_make_job())
    job_store.update_job = AsyncMock()

    invalid = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config(_make_target("http://a.com"), on_empty=OnEmptyAction.fail)
        mock_scrape.return_value = invalid

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.failed
    result_store.save_result.assert_not_called()
    error = error_store.log_error.call_args[0][0]
    assert error.action_taken == ActionTaken.fail


@pytest.mark.asyncio
async def test_validation_retry_rescrapes_and_succeeds(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=_make_job())
    job_store.update_job = AsyncMock()

    invalid = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])
    valid = TargetResult(url="http://a.com", status="success", data=[{"title": "ok"}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive", proxy_url="")
        mock_load.return_value = _make_config(_make_target("http://a.com"), on_empty=OnEmptyAction.retry)
        mock_scrape.side_effect = [invalid, valid]

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.complete
    assert mock_scrape.call_count == 2
    result_store.save_result.assert_called_once()
    error = error_store.log_error.call_args[0][0]
    assert error.action_taken == ActionTaken.retry


@pytest.mark.asyncio
async def test_worker_scopes_adaptive_state_by_project(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=_make_job())
    job_store.update_job = AsyncMock()

    result = TargetResult(url="http://a.com", status="success", data=[{"title": "ok"}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            workers_running_lease_seconds=300,
            proxy_url="",
        )
        mock_load.return_value = _make_config(_make_target("http://a.com"), on_empty=OnEmptyAction.warn)
        mock_scrape.return_value = result

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert mock_scrape.call_args.kwargs["adaptive_dir"] == "/tmp/adaptive/test"
