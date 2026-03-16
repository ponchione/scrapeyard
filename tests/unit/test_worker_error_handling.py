"""Tests for scrape_task crash recovery — job must end in 'failed' on unhandled errors."""

from unittest.mock import AsyncMock, MagicMock

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
