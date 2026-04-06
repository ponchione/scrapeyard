"""Tests for scrape_task crash recovery and duplicate-delivery guards."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ErrorType, JobStatus
from scrapeyard.queue.worker import _finalize_run, scrape_task
from tests.unit.worker_helpers import make_job


@pytest.mark.asyncio
async def test_scrape_task_marks_job_failed_on_bad_yaml():
    """If load_config raises, the job should end in 'failed' status."""
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job

    updated_jobs = []

    async def capture_update(j):
        updated_jobs.append(j)

    job_store.update_job_status.side_effect = capture_update

    await scrape_task(
        job.job_id,
        "not: valid: yaml: config: missing: project",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )

    assert len(updated_jobs) > 0
    assert updated_jobs[-1].status == JobStatus.failed


@pytest.mark.asyncio
async def test_scrape_task_marks_job_failed_on_missing_job():
    """If job_store.get_job raises KeyError, job should still be marked failed."""
    job_store = AsyncMock()
    job_store.get_job.side_effect = KeyError("no such job")

    # scrape_task should not raise — it should catch and log.
    await scrape_task(
        "nonexistent-job",
        "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )


@pytest.mark.asyncio
async def test_scrape_task_skips_completed_duplicate_run():
    job = make_job(job_id="test-job-1", name="crash-test", status=JobStatus.complete).model_copy(
        update={
            "current_run_id": "run-1",
        }
    )
    job_store = AsyncMock()
    job_store.get_job.return_value = job

    result_store = AsyncMock()
    error_store = AsyncMock()

    await scrape_task(
        job.job_id,
        "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
        run_id="run-1",
        job_store=job_store,
        result_store=result_store,
        error_store=error_store,
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )

    job_store.update_job_status.assert_not_called()
    result_store.save_result.assert_not_called()
    error_store.log_errors.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_task_skips_recent_running_duplicate():
    running_job = make_job(job_id="test-job-1", name="crash-test", status=JobStatus.running).model_copy(
        update={
            "current_run_id": "run-2",
            "updated_at": datetime.now(timezone.utc),
        }
    )
    job_store = AsyncMock()
    job_store.get_job.return_value = running_job

    await scrape_task(
        running_job.job_id,
        "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
        run_id="run-2",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )

    job_store.update_job_status.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_task_reclaims_stale_running_job():
    stale_job = make_job(job_id="test-job-1", name="crash-test", status=JobStatus.running).model_copy(
        update={
            "current_run_id": "run-3",
            "updated_at": datetime.now(timezone.utc) - timedelta(seconds=600),
        }
    )
    job_store = AsyncMock()
    job_store.get_job.side_effect = [stale_job, stale_job, stale_job]

    updated_jobs = []

    async def capture_update(job):
        updated_jobs.append(job)

    job_store.update_job_status.side_effect = capture_update

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=MagicMock(status="failed", data=[], errors=["boom"], pages_scraped=0, error_type=None, http_status=None, error_detail="boom"))):
        await scrape_task(
            stale_job.job_id,
            "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
            run_id="run-3",
            job_store=job_store,
            result_store=AsyncMock(),
            error_store=AsyncMock(),
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert updated_jobs
    assert updated_jobs[0].status == JobStatus.running


@pytest.mark.asyncio
async def test_scrape_task_batches_multiple_target_errors():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.update_job_status = AsyncMock()

    error_store = AsyncMock()

    fail_result = TargetResult(
        url="http://example.com",
        status="failed",
        data=[],
        errors=["timeout", "proxy refused"],
        pages_scraped=0,
        error_type=ErrorType.timeout,
    )

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=fail_result)), \
         patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            workers_running_lease_seconds=300,
            proxy_url="",
        )
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "crash-test"
        cfg.resolved_targets.return_value = [MagicMock(url="http://example.com", fetcher=MagicMock(value="basic"), proxy=None)]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = MagicMock(value="partial")
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.webhook = None
        cfg.proxy = None

        await scrape_task(
            job.job_id,
            "project: test\nname: crash-test\ntarget:\n  url: http://example.com\n  selectors:\n    title: h1",
            job_store=job_store,
            result_store=AsyncMock(),
            error_store=error_store,
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
        )

    error_store.log_errors.assert_called_once()
    logged_errors = error_store.log_errors.call_args[0][0]
    assert len(logged_errors) == 2
    assert [record.error_message for record in logged_errors] == ["timeout", "proxy refused"]


# ---------------------------------------------------------------------------
# D2: _finalize_run cross-DB error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_run_skips_when_no_run_id():
    """_finalize_run is a no-op when run_id is None."""
    job_store = AsyncMock()
    error_store = AsyncMock()
    await _finalize_run(None, JobStatus.complete, 5, job_store, error_store)
    error_store.count_errors_for_run.assert_not_called()
    job_store.finalize_run.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_run_happy_path():
    """Normal finalization calls count_errors then finalize_run."""
    job_store = AsyncMock()
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 3
    await _finalize_run("run-1", JobStatus.complete, 10, job_store, error_store)
    error_store.count_errors_for_run.assert_awaited_once_with("run-1")
    job_store.finalize_run.assert_awaited_once_with("run-1", "complete", 10, 3)


@pytest.mark.asyncio
async def test_finalize_run_falls_back_to_fail_run_on_error():
    """If finalize_run raises, _finalize_run falls back to fail_run."""
    job_store = AsyncMock()
    job_store.finalize_run.side_effect = RuntimeError("DB write failed")
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0

    # Should not raise — catches internally.
    await _finalize_run("run-2", JobStatus.complete, 5, job_store, error_store)

    job_store.finalize_run.assert_awaited_once()
    job_store.fail_run.assert_awaited_once_with("run-2")


@pytest.mark.asyncio
async def test_finalize_run_survives_both_failures():
    """If both finalize_run and fail_run raise, _finalize_run still doesn't crash."""
    job_store = AsyncMock()
    job_store.finalize_run.side_effect = RuntimeError("DB write failed")
    job_store.fail_run.side_effect = RuntimeError("Fallback also failed")
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0

    # Must not raise.
    await _finalize_run("run-3", JobStatus.partial, 2, job_store, error_store)

    job_store.finalize_run.assert_awaited_once()
    job_store.fail_run.assert_awaited_once_with("run-3")
