from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.api.scrape_submission import submit_scrape_job
from scrapeyard.config.schema import ExecutionMode, FailStrategy, FetcherType
from scrapeyard.models.job import JobStatus
from scrapeyard.storage.types import ResultPayload


@dataclass
class _QueuedJob:
    should_timeout: bool = False

    async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> None:
        if self.should_timeout:
            raise asyncio.TimeoutError


def _config(mode: ExecutionMode, fetcher: FetcherType = FetcherType.basic, with_pagination: bool = False):
    target = MagicMock(fetcher=fetcher, pagination=MagicMock() if with_pagination else None)
    execution = MagicMock(mode=mode, priority=MagicMock(value="normal"))
    execution.fail_strategy = FailStrategy.partial
    return MagicMock(
        project="integ",
        name="route-cleanup",
        execution=execution,
        resolved_targets=MagicMock(return_value=[target]),
    )


@pytest.mark.asyncio
async def test_submit_scrape_job_returns_terminal_payload_for_sync_completion():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    result_store.get_result.return_value = ResultPayload(run_id="run-1", data={"status": "complete"})
    saved_job = None

    async def _save_job(job):
        nonlocal saved_job
        saved_job = job

    async def _get_job(job_id: str):
        return saved_job.model_copy(update={"status": JobStatus.complete})

    job_store.save_job.side_effect = _save_job
    job_store.get_job.side_effect = _get_job
    worker_pool.enqueue.return_value = _QueuedJob()

    submission = await submit_scrape_job(
        config_yaml="project: integ",
        config=_config(ExecutionMode.sync),
        job_store=job_store,
        result_store=result_store,
        worker_pool=worker_pool,
        sync_timeout_seconds=5,
        sync_poll_delay_seconds=0.1,
    )

    assert submission.completed is True
    assert submission.status == "complete"
    assert submission.results == {"status": "complete"}


@pytest.mark.asyncio
async def test_submit_scrape_job_returns_queued_when_sync_wait_times_out():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    worker_pool.enqueue.return_value = _QueuedJob(should_timeout=True)

    submission = await submit_scrape_job(
        config_yaml="project: integ",
        config=_config(ExecutionMode.sync),
        job_store=job_store,
        result_store=result_store,
        worker_pool=worker_pool,
        sync_timeout_seconds=0,
        sync_poll_delay_seconds=0.25,
    )

    assert submission.completed is False
    assert submission.status == "queued"
    assert submission.results is None


@pytest.mark.asyncio
async def test_submit_scrape_job_prefers_async_response_for_non_basic_targets():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    worker_pool.enqueue.return_value = _QueuedJob()

    submission = await submit_scrape_job(
        config_yaml="project: integ",
        config=_config(ExecutionMode.auto, fetcher=FetcherType.dynamic),
        job_store=job_store,
        result_store=result_store,
        worker_pool=worker_pool,
        sync_timeout_seconds=5,
        sync_poll_delay_seconds=0.1,
    )

    assert submission.completed is False
    assert submission.status == "queued"
    result_store.get_result.assert_not_called()
