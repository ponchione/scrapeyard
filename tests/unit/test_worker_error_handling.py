"""Tests for scrape_task crash recovery and duplicate-delivery guards."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.worker import scrape_task


def _make_job(job_id="test-job-1", status=JobStatus.queued):
    return Job(
        job_id=job_id,
        project="test",
        name="crash-test",
        config_yaml="",
        status=status,
    )


@pytest.mark.asyncio
async def test_scrape_task_marks_job_failed_on_bad_yaml():
    """If load_config raises, the job should end in 'failed' status."""
    job = _make_job()
    job_store = AsyncMock()
    job_store.get_job.return_value = job

    updated_jobs = []

    async def capture_update(j):
        updated_jobs.append(j)

    job_store.update_job.side_effect = capture_update

    await scrape_task(
        job.job_id,
        "not: valid: yaml: config: missing: project",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
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
    )


@pytest.mark.asyncio
async def test_scrape_task_skips_completed_duplicate_run():
    job = _make_job(status=JobStatus.complete).model_copy(
        update={
            "current_run_id": "run-1",
            "last_run_at": datetime.now(timezone.utc),
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
    )

    job_store.update_job.assert_not_called()
    result_store.save_result.assert_not_called()
    error_store.log_error.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_task_skips_recent_running_duplicate():
    running_job = _make_job(status=JobStatus.running).model_copy(
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
    )

    job_store.update_job.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_task_reclaims_stale_running_job():
    stale_job = _make_job(status=JobStatus.running).model_copy(
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

    job_store.update_job.side_effect = capture_update

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=MagicMock(status="failed", data=[], errors=["boom"], pages_scraped=0, error_type=None, http_status=None, error_detail="boom"))):
        await scrape_task(
            stale_job.job_id,
            "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
            run_id="run-3",
            job_store=job_store,
            result_store=AsyncMock(),
            error_store=AsyncMock(),
            circuit_breaker=MagicMock(),
        )

    assert updated_jobs
    assert updated_jobs[0].status == JobStatus.running
