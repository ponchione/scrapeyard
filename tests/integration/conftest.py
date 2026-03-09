"""Integration test fixtures with a fully wired FastAPI app."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from scrapeyard.api.dependencies import get_scheduler, get_worker_pool
from scrapeyard.common.settings import get_settings
from scrapeyard.main import app
from scrapeyard.storage.database import init_db, reset_db


@pytest.fixture()
async def test_app():
    """Return a fully initialized app with storage/queue/scheduler running."""
    settings = get_settings()
    await init_db(settings.db_dir)

    pool = get_worker_pool()
    scheduler = get_scheduler()
    await pool.start()
    await scheduler.start()

    try:
        yield app
    finally:
        scheduler.shutdown()
        await pool.stop()
        reset_db()


@pytest.fixture()
async def client(test_app) -> AsyncIterator[AsyncClient]:
    """HTTP client against the ASGI app."""
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
