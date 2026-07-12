import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.models.job import JobStatus
from scrapeyard.runtime.health import (
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
async def test_load_project_summary_returns_empty_when_store_unavailable():
    get_job_store = MagicMock(side_effect=RuntimeError("not ready"))

    summary = await load_project_summary(get_job_store)

    assert summary == {}


@pytest.mark.asyncio
async def test_load_project_summary_uses_store_summary_by_project():
    fake_store = MagicMock(summary_by_project=AsyncMock(return_value=[("proj", JobStatus.complete.value, 1)]))

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
