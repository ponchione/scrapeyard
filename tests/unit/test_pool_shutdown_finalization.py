"""Worker-pool shutdown records a cancelled run's terminal state before returning."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arq.connections import RedisSettings

from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.pool import WorkerPool
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.database import init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from tests.unit.worker_helpers import SIMPLE_YAML, make_config_mock, make_settings_mock


@pytest.mark.asyncio
async def test_stop_during_a_run_fails_the_run_before_returning_within_grace(
    tmp_path,
    monkeypatch,
):
    await init_db(str(tmp_path / "db"))
    job_store = SQLiteJobStore()
    await job_store.save_job(
        Job(
            job_id="job-1",
            project="test",
            name="test-job",
            config_yaml=SIMPLE_YAML,
            current_run_id="run-1",
        )
    )
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0
    result_store = AsyncMock()
    result_store.delete_result.return_value = False
    target_started = asyncio.Event()

    async def _scrape_until_cancelled(*_args, **_kwargs):
        target_started.set()
        await asyncio.Event().wait()

    async def _task_handler(
        job_id: str,
        *,
        run_id: str,
        trigger: str = "adhoc",
        browser_limiter: BrowserExecutionLimiter,
    ) -> None:
        await scrape_task(
            job_id,
            SIMPLE_YAML,
            run_id=run_id,
            trigger=trigger,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
            browser_limiter=browser_limiter,
        )

    pool = WorkerPool(
        max_concurrent=1,
        max_browsers=1,
        memory_limit_mb=0,
        redis_settings=RedisSettings(host="localhost"),
        queue_name="test-queue",
        task_handler=_task_handler,
    )
    grace_seconds = 2.0
    monkeypatch.setattr(
        "scrapeyard.queue.pool.get_settings",
        lambda: MagicMock(workers_shutdown_grace_seconds=grace_seconds),
    )
    with patch(
        "scrapeyard.queue.worker.load_config",
        return_value=make_config_mock(),
    ), patch(
        "scrapeyard.queue.worker.scrape_target",
        side_effect=_scrape_until_cancelled,
    ), patch(
        "scrapeyard.queue.worker.get_settings",
        return_value=make_settings_mock(),
    ):
        job_task = asyncio.create_task(
            pool._run_job({}, "job-1", "run-1", run_id="run-1")
        )
        await asyncio.wait_for(target_started.wait(), timeout=5)
        running = await job_store.get_job_run("job-1", "run-1")
        assert running is not None
        assert running.status is JobStatus.running

        async def _close() -> None:
            return None

        pool._started = True
        pool._worker = SimpleNamespace(
            allow_pick_jobs=True,
            tasks={"run-1": job_task},
            main_task=None,
            close=_close,
        )
        pool._runner_task = asyncio.create_task(asyncio.sleep(0))
        loop = asyncio.get_running_loop()
        started = loop.time()

        await pool.stop()

        elapsed = loop.time() - started

    assert elapsed < grace_seconds
    assert job_task.cancelled()
    assert pool.shutdown_pending is False
    run = await job_store.get_job_run("job-1", "run-1")
    assert run is not None
    assert run.status is JobStatus.failed
    assert run.completed_at is not None
    job = await job_store.get_job("job-1")
    assert job.status is JobStatus.failed
