"""Integration test fixtures with a fully wired FastAPI app."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from scrapeyard.api.dependencies import (
    close_webhook_dispatcher,
    get_circuit_breaker,
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_webhook_dispatcher,
    get_worker_pool,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.main import app
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.database import close_db, init_db


class _FakeQueuedJob:
    def __init__(self, task: asyncio.Task[None]) -> None:
        self._task = task

    async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> None:
        del poll_delay
        await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)


@pytest.fixture()
async def test_app(monkeypatch):
    """Return a fully initialized app with storage/queue/scheduler running."""
    settings = get_settings()
    await init_db(settings.db_dir)

    pool = get_worker_pool()
    job_store = get_job_store()
    result_store = get_result_store()
    error_store = get_error_store()
    circuit_breaker = get_circuit_breaker()
    webhook_dispatcher = get_webhook_dispatcher()
    background_tasks: set[asyncio.Task[None]] = set()

    async def _fake_start() -> None:
        return None

    async def _fake_stop() -> None:
        if background_tasks:
            for task in list(background_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            background_tasks.clear()

    async def _fake_enqueue(
        job_id: str,
        config_yaml: str,
        priority: str = "normal",
        needs_browser: bool = False,
        *,
        run_id: str | None = None,
        trigger: str = "adhoc",
    ) -> _FakeQueuedJob:
        del priority, needs_browser
        task = asyncio.create_task(
            scrape_task(
                job_id,
                config_yaml,
                run_id=run_id,
                trigger=trigger,
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                circuit_breaker=circuit_breaker,
                rate_limiter=LocalDomainRateLimiter(),
                webhook_dispatcher=webhook_dispatcher,
            )
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return _FakeQueuedJob(task)

    monkeypatch.setattr(pool, "start", _fake_start)
    monkeypatch.setattr(pool, "stop", _fake_stop)
    monkeypatch.setattr(pool, "enqueue", _fake_enqueue)

    scheduler = get_scheduler()
    await pool.start()
    await scheduler.start()

    try:
        yield app
    finally:
        scheduler.shutdown()
        await pool.stop()
        await close_webhook_dispatcher(
            timeout=settings.workers_shutdown_grace_seconds,
        )
        await close_db()


@pytest.fixture()
async def client(test_app) -> AsyncIterator[AsyncClient]:
    """HTTP client against the ASGI app."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
