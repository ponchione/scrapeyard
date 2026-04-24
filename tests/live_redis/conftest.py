"""Live Redis test fixtures exercising the real queue path."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from arq.connections import RedisSettings, create_pool
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from scrapeyard.api.dependencies import (
    close_webhook_dispatcher,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.main import app
from scrapeyard.storage.database import init_db, reset_db


def _clear_singletons() -> None:
    from scrapeyard.common.settings import get_settings as _get_settings
    from scrapeyard.api.dependencies import (
        get_circuit_breaker as _get_circuit_breaker,
        get_error_store as _get_error_store,
        get_job_store as _get_job_store,
        get_result_store as _get_result_store,
        get_scheduler as _get_scheduler,
        get_webhook_dispatcher as _get_webhook_dispatcher,
        get_worker_pool as _get_worker_pool,
    )

    for cached_fn in [
        _get_settings,
        _get_job_store,
        _get_error_store,
        _get_result_store,
        _get_circuit_breaker,
        _get_webhook_dispatcher,
        _get_worker_pool,
        _get_scheduler,
    ]:
        cached_fn.cache_clear()


@pytest.fixture(autouse=True)
def _live_redis_env(monkeypatch, tmp_path):
    """Point the app at a real Redis instance with isolated local state."""
    queue_name = f"scrapeyard-live-{uuid.uuid4().hex[:8]}"

    monkeypatch.setenv("SCRAPEYARD_DB_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("SCRAPEYARD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SCRAPEYARD_STORAGE_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("SCRAPEYARD_ADAPTIVE_DIR", str(tmp_path / "adaptive"))
    monkeypatch.setenv("SCRAPEYARD_REDIS_DSN", "redis://127.0.0.1:56379/15")
    monkeypatch.setenv("SCRAPEYARD_QUEUE_NAME", queue_name)

    _clear_singletons()
    yield
    _clear_singletons()


@pytest.fixture()
async def live_app() -> AsyncIterator:
    """Return a fully initialized app using the real WorkerPool and Redis."""
    settings = get_settings()
    redis = None
    pool = None
    scheduler = None

    try:
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_dsn))
    except (OSError, RedisConnectionError) as exc:
        pytest.skip(f"Live Redis unavailable at {settings.redis_dsn}: {exc}")

    try:
        await redis.flushdb()
        await init_db(settings.db_dir)

        pool = get_worker_pool()
        scheduler = get_scheduler()
        await pool.start()
        await scheduler.start()

        yield app
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        if pool is not None:
            await pool.stop()
        await close_webhook_dispatcher(
            timeout=settings.workers_shutdown_grace_seconds,
        )
        if redis is not None:
            await redis.flushdb()
            await redis.aclose()
        reset_db()


@pytest.fixture()
async def client(live_app) -> AsyncIterator[AsyncClient]:
    """HTTP client against the ASGI app using the live Redis-backed pool."""
    transport = ASGITransport(app=live_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
