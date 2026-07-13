"""Tests for validation.on_empty behavior in scrape_task."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scrapeyard.config.schema import OnEmptyAction
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ActionTaken, JobStatus
from scrapeyard.queue.worker import scrape_task
from tests.unit.worker_helpers import (
    finalized_status,
    make_config_mock,
    make_job,
    make_settings_mock,
    make_target,
)


def _first_logged_error(error_store: AsyncMock):
    return error_store.log_errors.call_args[0][0][0]


@pytest.mark.asyncio
async def test_validation_warn_keeps_data_and_completes(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())

    result = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(
            targets=[make_target("http://a.com")],
            validation_overrides={"required_fields": ["title"], "min_results": 1, "on_empty": OnEmptyAction.warn},
        )
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

    assert finalized_status(job_store) == JobStatus.complete
    result_store.save_result.assert_called_once()
    error = _first_logged_error(error_store)
    assert error.action_taken == ActionTaken.warn


@pytest.mark.asyncio
async def test_validation_skip_discards_invalid_target_but_keeps_job_complete(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())

    invalid = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])
    valid = TargetResult(url="http://b.com", status="success", data=[{"title": "ok"}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(
            targets=[make_target("http://a.com"), make_target("http://b.com")],
            validation_overrides={"required_fields": ["title"], "min_results": 1, "on_empty": OnEmptyAction.skip},
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

    assert finalized_status(job_store) == JobStatus.complete
    result_store.save_result.assert_called_once()
    assert result_store.save_result.call_args.kwargs["record_count"] == 1
    error = _first_logged_error(error_store)
    assert error.action_taken == ActionTaken.skip


@pytest.mark.asyncio
async def test_validation_fail_marks_target_failed(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())

    invalid = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(
            targets=[make_target("http://a.com")],
            validation_overrides={"required_fields": ["title"], "min_results": 1, "on_empty": OnEmptyAction.fail},
        )
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

    assert finalized_status(job_store) == JobStatus.failed
    # Worker persists result metadata (with 0 records) even on failure
    # for observability — the key assertion is status + record_count.
    result_store.save_result.assert_called_once()
    call_kwargs = result_store.save_result.call_args
    assert call_kwargs.kwargs["record_count"] == 0
    assert call_kwargs.kwargs["status"] == "failed"
    error = _first_logged_error(error_store)
    assert error.action_taken == ActionTaken.fail


@pytest.mark.asyncio
async def test_validation_retry_rescrapes_and_succeeds(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())

    invalid = TargetResult(url="http://a.com", status="success", data=[{"title": ""}])
    valid = TargetResult(url="http://a.com", status="success", data=[{"title": "ok"}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(
            targets=[make_target("http://a.com")],
            validation_overrides={"required_fields": ["title"], "min_results": 1, "on_empty": OnEmptyAction.retry},
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

    assert finalized_status(job_store) == JobStatus.complete
    assert mock_scrape.call_count == 2
    result_store.save_result.assert_called_once()
    error = _first_logged_error(error_store)
    assert error.action_taken == ActionTaken.retry
    assert error.resolved is True


@pytest.mark.asyncio
async def test_required_price_keeps_map_listing_after_validation(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())

    map_priced = TargetResult(
        url="http://a.com",
        status="success",
        data=[{"title": "ok", "price": None, "pricing_visibility": "map"}],
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(
            targets=[make_target("http://a.com")],
            validation_overrides={"required_fields": ["price"], "min_results": 1, "on_empty": OnEmptyAction.skip},
        )
        mock_scrape.return_value = map_priced

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert finalized_status(job_store) == JobStatus.complete
    result_store.save_result.assert_called_once()
    assert result_store.save_result.call_args.kwargs["record_count"] == 1
    error_store.log_errors.assert_not_called()


@pytest.mark.asyncio
async def test_worker_scopes_adaptive_state_by_project(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())

    result = TargetResult(url="http://a.com", status="success", data=[{"title": "ok"}])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        mock_load.return_value = make_config_mock(
            targets=[make_target("http://a.com")],
            validation_overrides={"required_fields": ["title"], "min_results": 1, "on_empty": OnEmptyAction.warn},
        )
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
