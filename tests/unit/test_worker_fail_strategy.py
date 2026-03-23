"""Test fail_strategy behavior in scrape_task."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import FailStrategy
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import Job, JobStatus
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
    target = MagicMock(url=url)
    target.fetcher.value = "basic"
    return target


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
async def test_partial_returns_partial_on_mixed(mock_stores):
    """partial: mixed success/failure yields JobStatus.partial."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive")
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [_make_target("http://a.com"), _make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.partial
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.partial


@pytest.mark.asyncio
async def test_all_or_nothing_fails_on_any_failure(mock_stores):
    """all_or_nothing: any failure yields JobStatus.failed, no results saved."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive")
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [_make_target("http://a.com"), _make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.all_or_nothing
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.failed
    result_store.save_result.assert_not_called()


@pytest.mark.asyncio
async def test_continue_completes_even_with_failures(mock_stores):
    """continue: failures don't affect status if data exists."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive")
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [_make_target("http://a.com"), _make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.continue_
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.complete
    result_store.save_result.assert_called_once()


@pytest.mark.asyncio
async def test_worker_passes_record_count_to_save_result(mock_stores):
    """Worker must pass len(flat_data) as record_count to save_result."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}, {"title": "B"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive")
        cfg = mock_load.return_value
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

        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    result_store.save_result.assert_called_once()
    call_kwargs = result_store.save_result.call_args
    assert call_kwargs.kwargs.get("record_count") == 2


@pytest.mark.asyncio
async def test_worker_passes_final_status_to_save_result(mock_stores):
    """Worker must persist the computed final job status with the result metadata."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(adaptive_dir="/tmp/adaptive")
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [_make_target("http://a.com"), _make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.partial
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    result_store.save_result.assert_called_once()
    call_kwargs = result_store.save_result.call_args
    assert call_kwargs.kwargs.get("status") == "partial"
