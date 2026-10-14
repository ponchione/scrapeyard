"""Live Redis test fixtures exercising the real queue path."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator
from functools import cache

import pytest
from arq.connections import RedisSettings, create_pool
from httpx import ASGITransport, AsyncClient
from redis.exceptions import ConnectionError as RedisConnectionError

from scrapeyard.api.auth import parse_api_credentials
from scrapeyard.api.dependencies import (
    close_webhook_dispatcher,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.main import app
from scrapeyard.storage.database import close_db, init_db


def _clear_singletons() -> None:
    from scrapeyard.api.dependencies import (
        get_circuit_breaker as _get_circuit_breaker,
        get_error_store as _get_error_store,
        get_job_store as _get_job_store,
        get_result_store as _get_result_store,
        get_scheduler as _get_scheduler,
        get_webhook_dispatcher as _get_webhook_dispatcher,
        get_webhook_outbox_store as _get_webhook_outbox_store,
        get_worker_pool as _get_worker_pool,
    )
    from scrapeyard.common.settings import get_settings as _get_settings

    for cached_fn in [
        _get_settings,
        _get_job_store,
        _get_error_store,
        _get_result_store,
        _get_circuit_breaker,
        _get_webhook_outbox_store,
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
    monkeypatch.setenv(
        "SCRAPEYARD_REDIS_DSN",
        os.environ.get("SCRAPEYARD_REDIS_DSN", "redis://127.0.0.1:56379/15"),
    )
    monkeypatch.setenv("SCRAPEYARD_QUEUE_NAME", queue_name)

    _clear_singletons()
    yield
    _clear_singletons()


@cache
def _unreachable_redis(dsn: str) -> str | None:
    """Probe the live Redis endpoint once per session; return why it is unreachable.

    create_pool retries for about five seconds, which every skipped test paid.
    """
    settings = RedisSettings.from_dsn(dsn)
    try:
        with socket.create_connection((settings.host, settings.port), timeout=1):
            return None
    except OSError as exc:
        return str(exc) or type(exc).__name__


class RedisRelay:
    """A TCP relay to the live Redis that can drop and refuse every connection.

    It stands in for Redis going away under a running application: a crash, an
    out-of-memory kill or ``docker stop``.
    """

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._relay, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def resume(self) -> None:
        """Listen again on the same port, as a Redis that has come back."""
        await self.start()

    async def cut(self) -> None:
        """Stop listening and reset every connection, both sides."""
        assert self._server is not None
        self._server.close()
        for writer in list(self._writers):
            writer.transport.abort()
        self._writers.clear()
        await self._server.wait_closed()
        self._server = None

    async def _relay(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        except OSError:
            writer.transport.abort()
            return
        self._writers.update((writer, upstream_writer))

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            try:
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()
            except OSError:
                pass
            finally:
                sink.transport.abort()

        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
        self._writers.difference_update((writer, upstream_writer))


@pytest.fixture()
async def redis_relay(_live_redis_env, monkeypatch) -> AsyncIterator[RedisRelay]:
    """Point the application at Redis through a relay the test can cut.

    Ask for it before ``live_app`` or ``client`` so that they connect through it.
    """
    dsn = os.environ["SCRAPEYARD_REDIS_DSN"]
    unreachable = _unreachable_redis(dsn)
    if unreachable is not None:
        pytest.skip(f"Live Redis unavailable at {dsn}: {unreachable}")
    target = RedisSettings.from_dsn(dsn)
    relay = RedisRelay(target.host, target.port)
    await relay.start()
    monkeypatch.setenv(
        "SCRAPEYARD_REDIS_DSN",
        f"redis://127.0.0.1:{relay.port}/{target.database}",
    )
    _clear_singletons()
    try:
        yield relay
    finally:
        if relay._server is not None:
            await relay.cut()


@pytest.fixture()
async def live_app(request) -> AsyncIterator:
    """Return a fully initialized app using the real WorkerPool and Redis."""
    settings = get_settings()
    redis = None
    pool = None
    scheduler = None
    runner_exits: list[BaseException | None] = []

    unreachable = _unreachable_redis(settings.redis_dsn)
    if unreachable is not None:
        pytest.skip(f"Live Redis unavailable at {settings.redis_dsn}: {unreachable}")
    try:
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_dsn))
    except (OSError, RedisConnectionError) as exc:
        pytest.skip(f"Live Redis unavailable at {settings.redis_dsn}: {exc}")

    try:
        await redis.flushdb()
        await init_db(settings.db_dir)

        pool = get_worker_pool()
        # Every test here ends in a stop; a worker that ended without one
        # would have asked the process to end.
        pool.on_runner_exit = runner_exits.append
        app.state.live_runner_exits = runner_exits
        scheduler = get_scheduler()
        await pool.start()
        await scheduler.start()

        yield app
    finally:
        if scheduler is not None:
            scheduler.shutdown()
        if pool is not None:
            await pool.stop()
            if "redis_relay" not in request.fixturenames:
                assert runner_exits == []
        await close_webhook_dispatcher(
            timeout=settings.workers_shutdown_grace_seconds,
        )
        if redis is not None:
            await redis.flushdb()
            await redis.aclose()
        await close_db()


@pytest.fixture()
async def client(live_app) -> AsyncIterator[AsyncClient]:
    """HTTP client against the ASGI app using the live Redis-backed pool."""
    transport = ASGITransport(app=live_app)
    settings = get_settings()
    credentials = parse_api_credentials(
        settings.api_credentials,
        legacy_keys=settings.parsed_api_keys(),
    )
    headers = {"X-API-Key": credentials[0].secret} if credentials else None
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers=headers,
    ) as async_client:
        yield async_client
