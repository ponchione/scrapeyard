import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.models.job import JobStatus
from scrapeyard.runtime.health import (
    HealthCache,
    build_project_summary,
    load_project_summary,
    probe_asyncio_task,
    probe_background_service,
    probe_redis,
    probe_result_storage,
)


def test_result_storage_probe_exercises_writable_directory_and_cleans_up(tmp_path):
    result = probe_result_storage(str(tmp_path))

    assert result.ok is True
    assert list(tmp_path.iterdir()) == []


def test_result_storage_probe_fails_for_missing_directory(tmp_path):
    result = probe_result_storage(str(tmp_path / "missing"))

    assert result.ok is False
    assert "FileNotFoundError" in (result.detail or "")


async def test_background_task_probe_reports_exception_type():
    async def _fail() -> None:
        raise RuntimeError("sensitive detail")

    task = asyncio.create_task(_fail())
    await asyncio.sleep(0)

    result = probe_asyncio_task("cleanup", task)

    assert result.ok is False
    assert result.detail == "cleanup task failed: RuntimeError"
    assert "sensitive detail" not in result.detail


def test_background_service_probe_uses_explicit_contract():
    result = probe_background_service(
        "scheduler",
        SimpleNamespace(background_ok=False, background_detail="scheduler stopped"),
    )

    assert result.ok is False
    assert result.detail == "scheduler stopped"


def test_background_service_probe_reports_missing_service():
    result = probe_background_service("scheduler", None)

    assert result.ok is False
    assert result.detail == "scheduler service missing"


@pytest.mark.asyncio
async def test_redis_probe_uses_worker_pool_adapter():
    pool = SimpleNamespace(ping=AsyncMock(return_value=None))

    result = await probe_redis(pool)

    assert result.ok is True
    pool.ping.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_redis_probe_sanitizes_adapter_failure():
    pool = SimpleNamespace(ping=AsyncMock(side_effect=RuntimeError("not connected")))

    result = await probe_redis(pool)

    assert result.ok is False
    assert result.detail == "redis ping failed: not connected"


def test_build_project_summary_classifies_project_statuses():
    rows = [
        ("healthy-project", JobStatus.complete.value, 2),
        ("degraded-project", JobStatus.running.value, 1),
        ("degraded-project", JobStatus.partial.value, 1),
        ("failing-project", JobStatus.failed.value, 1),
    ]

    summary = build_project_summary(rows)

    assert summary["healthy-project"]["status"] == "healthy"
    assert summary["degraded-project"]["status"] == "degraded"
    assert summary["failing-project"]["status"] == "failing"
    assert summary["degraded-project"]["job_count"] == 2


@pytest.mark.asyncio
async def test_load_project_summary_surfaces_store_unavailability():
    get_job_store = MagicMock(side_effect=RuntimeError("not ready"))

    with pytest.raises(RuntimeError, match="not ready"):
        await load_project_summary(get_job_store)


@pytest.mark.asyncio
async def test_load_project_summary_uses_store_summary_by_project():
    fake_store = MagicMock(
        summary_by_project=AsyncMock(return_value=[("proj", JobStatus.complete.value, 1)])
    )

    summary = await load_project_summary(lambda: fake_store)

    assert summary == {
        "proj": {
            "job_count": 1,
            "status": "healthy",
            "status_counts": {
                "queued": 0,
                "running": 0,
                "complete": 1,
                "partial": 0,
                "failed": 0,
                "cancelled": 0,
                "deleting": 0,
            },
        }
    }


@pytest.mark.asyncio
async def test_project_summary_refresh_is_single_flight_and_shielded_from_callers():
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = 0

    async def summary_by_project():
        nonlocal calls
        calls += 1
        refresh_started.set()
        await release_refresh.wait()
        return [("proj", JobStatus.complete.value, 1)]

    cache = HealthCache(
        lambda: SimpleNamespace(summary_by_project=summary_by_project),
        cache_ttl_seconds=60,
    )
    callers = [
        asyncio.create_task(cache.project_summary(timeout=0.01))
        for _ in range(5)
    ]
    await refresh_started.wait()
    outcomes = await asyncio.gather(*callers, return_exceptions=True)

    assert calls == 1
    assert all(isinstance(outcome, asyncio.TimeoutError) for outcome in outcomes)

    assert cache.cached_project_summary == {}

    release_refresh.set()
    summary = await cache.project_summary(timeout=0.5)
    assert summary["proj"]["job_count"] == 1


@pytest.mark.asyncio
async def test_project_summary_refresh_failure_retains_last_successful_cache():
    store = SimpleNamespace(
        summary_by_project=AsyncMock(
            side_effect=[
                [("proj", JobStatus.complete.value, 1)],
                RuntimeError("database busy"),
            ]
        )
    )
    cache = HealthCache(lambda: store, cache_ttl_seconds=60)
    first = await cache.project_summary()
    cache._projects_cache_refreshed_at = 0.0

    with pytest.raises(RuntimeError, match="database busy"):
        await cache.project_summary()

    assert cache.cached_project_summary == first
