"""Tests for main.py health cache, lifespan orchestration, and health status."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

import scrapeyard.main as main_module


@pytest.mark.asyncio
async def test_health_cache_project_summary_classifies_statuses():
    cache = main_module.HealthCache(cache_ttl_seconds=60)
    fake_store = MagicMock(summary_by_project=AsyncMock(return_value=[
        ("healthy-project", "complete", 2),
        ("degraded-project", "running", 1),
        ("degraded-project", "partial", 1),
        ("failing-project", "failed", 1),
    ]))

    original_get_job_store = main_module.get_job_store
    main_module.get_job_store = lambda: fake_store
    try:
        summary = await cache.project_summary()
    finally:
        main_module.get_job_store = original_get_job_store

    assert summary["healthy-project"]["status"] == "healthy"
    assert summary["degraded-project"]["status"] == "degraded"
    assert summary["failing-project"]["status"] == "failing"
    assert summary["degraded-project"]["job_count"] == 2


@pytest.mark.asyncio
async def test_health_cache_uses_cached_summary_until_ttl_expires():
    cache = main_module.HealthCache(cache_ttl_seconds=60)
    fake_store = MagicMock(summary_by_project=AsyncMock(return_value=[("proj", "complete", 1)]))

    original_get_job_store = main_module.get_job_store
    main_module.get_job_store = lambda: fake_store
    try:
        first = await cache.project_summary()
        second = await cache.project_summary()
    finally:
        main_module.get_job_store = original_get_job_store

    assert first == second
    fake_store.summary_by_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_cache_returns_empty_summary_when_store_unavailable():
    cache = main_module.HealthCache(cache_ttl_seconds=60)

    original_get_job_store = main_module.get_job_store
    main_module.get_job_store = lambda: (_ for _ in ()).throw(RuntimeError("not ready"))
    try:
        summary = await cache.project_summary()
    finally:
        main_module.get_job_store = original_get_job_store

    assert summary == {}


@pytest.mark.asyncio
async def test_lifespan_initializes_and_shuts_down_dependencies(monkeypatch):
    app = FastAPI()
    settings = SimpleNamespace(
        log_dir="/tmp/logs",
        log_level="DEBUG",
        db_dir="/tmp/db",
        workers_shutdown_grace_seconds=7,
    )
    pool = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), redis=object())
    scheduler = SimpleNamespace(start=AsyncMock(), shutdown=MagicMock())
    webhook_dispatcher = SimpleNamespace(startup=AsyncMock())
    class _CleanupTask:
        def __init__(self) -> None:
            self.cancel = MagicMock()
            self.awaited = False

        def __await__(self):
            async def _wait():
                self.awaited = True
                raise asyncio.CancelledError
            return _wait().__await__()

    cleanup_task = _CleanupTask()

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "setup_logging", MagicMock())
    monkeypatch.setattr(main_module, "init_db", AsyncMock())
    monkeypatch.setattr(main_module, "get_job_store", lambda: "job-store")
    monkeypatch.setattr(main_module, "get_error_store", lambda: "error-store")
    monkeypatch.setattr(main_module, "get_result_store", lambda: "result-store")
    monkeypatch.setattr(main_module, "get_webhook_dispatcher", lambda: webhook_dispatcher)
    monkeypatch.setattr(main_module, "get_worker_pool", lambda: pool)
    monkeypatch.setattr(main_module, "init_rate_limiter", MagicMock())
    monkeypatch.setattr(main_module, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(main_module, "start_cleanup_loop", lambda _store: cleanup_task)
    monkeypatch.setattr(main_module, "close_webhook_dispatcher", AsyncMock())
    monkeypatch.setattr(main_module, "close_db", AsyncMock())

    async with main_module.lifespan(app):
        assert app.state.job_store == "job-store"
        assert app.state.error_store == "error-store"
        assert app.state.result_store == "result-store"
        assert app.state.worker_pool is pool
        assert app.state.scheduler is scheduler
        assert app.state.cleanup_task is cleanup_task

    main_module.setup_logging.assert_called_once_with("/tmp/logs", "DEBUG")
    main_module.init_db.assert_awaited_once_with("/tmp/db")
    webhook_dispatcher.startup.assert_awaited_once()
    pool.start.assert_awaited_once()
    main_module.init_rate_limiter.assert_called_once_with(redis=pool.redis)
    scheduler.start.assert_awaited_once()
    cleanup_task.cancel.assert_called_once()
    scheduler.shutdown.assert_called_once()
    pool.stop.assert_awaited_once()
    main_module.close_webhook_dispatcher.assert_awaited_once_with(timeout=7)
    main_module.close_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_returns_degraded_when_pool_is_saturated(monkeypatch):
    pool = SimpleNamespace(max_concurrent=2, active_tasks=2, max_browsers=1, active_browsers=1)
    monkeypatch.setattr(main_module, "get_worker_pool", lambda: pool)
    monkeypatch.setattr(main_module._health, "project_summary", AsyncMock(return_value={"proj": {"status": "healthy"}}))
    monkeypatch.setattr(main_module._health, "start_time", 1.0)
    monkeypatch.setattr(main_module.time, "monotonic", lambda: 13.3)

    result = await main_module.health()

    assert result["status"] == "degraded"
    assert result["workers"]["active_tasks"] == 2
    assert result["projects"] == {"proj": {"status": "healthy"}}
