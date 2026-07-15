from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.api.scrape_submission import (
    ResultArtifactUnavailableError,
    submit_scrape_job,
    wait_for_persisted_job,
    wait_for_queued_job,
)
from scrapeyard.config.schema import ExecutionMode, FailStrategy, FetcherType
from scrapeyard.models.job import JobStatus
from scrapeyard.storage.job_store import DuplicateJobError
from scrapeyard.storage.types import ResultPayload


@dataclass
class _QueuedJob:
    should_timeout: bool = False

    async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> None:
        if self.should_timeout:
            raise asyncio.TimeoutError


class _CancelledQueuedJob:
    async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> None:
        raise asyncio.CancelledError


class _BlockingQueuedJob:
    async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> None:
        await asyncio.Event().wait()


def _config(
    mode: ExecutionMode,
    fetcher: FetcherType = FetcherType.basic,
    with_pagination: bool = False,
    *,
    name: str = "route-cleanup",
):
    target = MagicMock(fetcher=fetcher, pagination=MagicMock() if with_pagination else None)
    execution = MagicMock(mode=mode, priority=MagicMock(value="normal"))
    execution.fail_strategy = FailStrategy.partial
    config = MagicMock(
        project="integ",
        execution=execution,
        resolved_targets=MagicMock(return_value=[target]),
    )
    config.name = name
    return config


@pytest.mark.asyncio
async def test_wait_for_queued_job_treats_arq_cancellation_as_terminal():
    completed = await wait_for_queued_job(
        _CancelledQueuedJob(),
        timeout_seconds=5,
        poll_delay_seconds=0.1,
    )

    assert completed is False


@pytest.mark.asyncio
async def test_wait_for_queued_job_propagates_request_cancellation():
    waiter = asyncio.create_task(
        wait_for_queued_job(
            _BlockingQueuedJob(),
            timeout_seconds=5,
            poll_delay_seconds=0.1,
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [JobStatus.cancelled, JobStatus.deleting])
async def test_wait_for_persisted_job_stops_for_non_result_terminal_status(status):
    job_store = AsyncMock()
    job_store.get_job.return_value = MagicMock(status=status)

    completed = await wait_for_persisted_job(
        "job-1",
        job_store=job_store,
        timeout_seconds=5,
        poll_delay_seconds=0.1,
    )

    assert completed is False
    job_store.get_job.assert_awaited_once_with("job-1")


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
async def test_submit_scrape_job_rejects_durable_failed_state_without_artifact():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    saved_job = None

    async def _save_job(job):
        nonlocal saved_job
        saved_job = job

    async def _get_job(_job_id: str):
        return saved_job.model_copy(update={"status": JobStatus.failed})

    job_store.save_job.side_effect = _save_job
    job_store.get_job.side_effect = _get_job
    result_store.get_result.side_effect = KeyError("no preclaim artifact")
    worker_pool.enqueue.return_value = _QueuedJob()

    with pytest.raises(ResultArtifactUnavailableError):
        await submit_scrape_job(
            config_yaml="project: integ",
            config=_config(ExecutionMode.sync),
            job_store=job_store,
            result_store=result_store,
            worker_pool=worker_pool,
            sync_timeout_seconds=5,
            sync_poll_delay_seconds=0.1,
        )

    result_store.get_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_scrape_job_rejects_terminal_run_with_missing_artifact():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    result_store.get_result.side_effect = KeyError("missing")
    saved_job = None

    async def _save_job(job):
        nonlocal saved_job
        saved_job = job

    async def _get_job(_job_id: str):
        return saved_job.model_copy(update={"status": JobStatus.complete})

    job_store.save_job.side_effect = _save_job
    job_store.get_job.side_effect = _get_job
    worker_pool.enqueue.return_value = _QueuedJob()

    with pytest.raises(ResultArtifactUnavailableError):
        await submit_scrape_job(
            config_yaml="project: integ",
            config=_config(ExecutionMode.sync),
            job_store=job_store,
            result_store=result_store,
            worker_pool=worker_pool,
            sync_timeout_seconds=5,
            sync_poll_delay_seconds=0.1,
        )


@pytest.mark.asyncio
async def test_submit_scrape_job_returns_queued_when_sync_wait_times_out():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    worker_pool.enqueue.return_value = _QueuedJob(should_timeout=True)
    saved_job = None

    async def _save_job(job):
        nonlocal saved_job
        saved_job = job

    async def _get_job(_job_id: str):
        return saved_job

    job_store.save_job.side_effect = _save_job
    job_store.get_job.side_effect = _get_job

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
    assert submission.run_id == saved_job.current_run_id
    assert submission.results is None


@pytest.mark.asyncio
async def test_sync_timeout_reports_current_persisted_running_status():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    worker_pool.enqueue.return_value = _QueuedJob(should_timeout=True)
    saved_job = None

    async def _save_job(job):
        nonlocal saved_job
        saved_job = job

    async def _get_job(_job_id: str):
        return saved_job.model_copy(update={"status": JobStatus.running})

    job_store.save_job.side_effect = _save_job
    job_store.get_job.side_effect = _get_job

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
    assert submission.status == "running"


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


@pytest.mark.asyncio
async def test_submit_scrape_job_removes_job_when_enqueue_fails():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    worker_pool.enqueue.side_effect = RuntimeError("redis unavailable")

    with pytest.raises(RuntimeError, match="redis unavailable"):
        await submit_scrape_job(
            config_yaml="project: integ",
            config=_config(ExecutionMode.async_),
            job_store=job_store,
            result_store=result_store,
            worker_pool=worker_pool,
            sync_timeout_seconds=5,
            sync_poll_delay_seconds=0.1,
        )

    saved_job = job_store.save_job.call_args.args[0]
    job_store.rollback_queued_submission.assert_awaited_once_with(
        saved_job.job_id,
        saved_job.current_run_id,
    )


@pytest.mark.asyncio
async def test_submit_scrape_job_retries_ad_hoc_name_collision(monkeypatch):
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    worker_pool.enqueue.return_value = _QueuedJob()
    generated_names = iter(["duplicate", "unique"])

    async def _save_job(job):
        if job.name == "duplicate":
            raise DuplicateJobError(job.project, job.name)

    monkeypatch.setattr(
        "scrapeyard.api.scrape_submission._adhoc_job_name",
        lambda _config_name: next(generated_names),
    )
    job_store.save_job.side_effect = _save_job

    submission = await submit_scrape_job(
        config_yaml="project: integ",
        config=_config(ExecutionMode.async_),
        job_store=job_store,
        result_store=result_store,
        worker_pool=worker_pool,
        sync_timeout_seconds=5,
        sync_poll_delay_seconds=0.1,
    )

    saved_jobs = [call.args[0] for call in job_store.save_job.await_args_list]
    assert [job.name for job in saved_jobs] == ["duplicate", "unique"]
    assert submission.job_id == saved_jobs[1].job_id
    worker_pool.enqueue.assert_awaited_once()
    assert worker_pool.enqueue.await_args.args[0] == saved_jobs[1].job_id


@pytest.mark.asyncio
async def test_submit_scrape_job_keeps_generated_name_within_path_limit():
    job_store = AsyncMock()
    result_store = AsyncMock()
    worker_pool = AsyncMock()
    worker_pool.enqueue.return_value = _QueuedJob()

    await submit_scrape_job(
        config_yaml="project: integ",
        config=_config(ExecutionMode.async_, name="x" * 255),
        job_store=job_store,
        result_store=result_store,
        worker_pool=worker_pool,
        sync_timeout_seconds=5,
        sync_poll_delay_seconds=0.1,
    )

    saved_job = job_store.save_job.call_args.args[0]
    assert len(saved_job.name.encode("utf-8")) <= 255
    assert saved_job.name.startswith("x" * 246)
    assert saved_job.name[246] == "-"
