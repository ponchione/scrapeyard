"""Test the /health endpoint."""

import asyncio

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import scrapeyard.main as main_module
from scrapeyard.common.logging import setup_logging
from scrapeyard.main import app
from scrapeyard.runtime.health import ProbeResult
from scrapeyard.storage.database import SQLiteProbeError


@pytest.fixture
def isolated_logger():
    logger = logging.Logger("scrapeyard-test")
    yield logger
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()


def test_setup_logging_is_idempotent(tmp_path, isolated_logger) -> None:
    log_dir = tmp_path / "logs"
    with patch("logging.getLogger", return_value=isolated_logger):
        setup_logging(str(log_dir))
        first_count = len(isolated_logger.handlers)
        setup_logging(str(log_dir))
        assert len(isolated_logger.handlers) == first_count


def test_setup_logging_uses_configured_level(tmp_path, isolated_logger) -> None:
    log_dir = tmp_path / "logs"
    with patch("logging.getLogger", return_value=isolated_logger):
        setup_logging(str(log_dir), "debug")
    assert isolated_logger.level == logging.DEBUG


def test_setup_logging_rejects_unknown_level(tmp_path, isolated_logger) -> None:
    log_dir = tmp_path / "logs"
    with patch("logging.getLogger", return_value=isolated_logger):
        with pytest.raises(ValueError, match="SCRAPEYARD_LOG_LEVEL"):
            setup_logging(str(log_dir), "chatty")


def _all_probes_ok(monkeypatch) -> None:
    async def _ok_async(*_args, **_kwargs):
        return ProbeResult(True)

    def _ok_sync(*_args, **_kwargs):
        return ProbeResult(True)

    monkeypatch.setattr("scrapeyard.main.probe_redis", _ok_async)
    monkeypatch.setattr("scrapeyard.main.probe_sqlite", _ok_async)
    monkeypatch.setattr("scrapeyard.main.probe_disk", _ok_sync)
    monkeypatch.setattr("scrapeyard.main.probe_result_storage", _ok_sync)
    monkeypatch.setattr(
        "scrapeyard.main._background_probes",
        lambda: {
            name: ProbeResult(True)
            for name in ("worker", "scheduler", "cleanup", "webhook")
        },
    )
    monkeypatch.setattr(
        "scrapeyard.queue.pool.WorkerPool.queue_depths",
        AsyncMock(return_value={"high": 2, "normal": 3, "low": 5}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/health", "/health/live"])
async def test_public_liveness_is_minimal_and_does_not_run_probes(monkeypatch, path):
    monkeypatch.setattr(
        "scrapeyard.main.probe_redis",
        AsyncMock(side_effect=AssertionError("liveness must not probe Redis")),
    )
    monkeypatch.setattr(
        "scrapeyard.main.probe_sqlite",
        AsyncMock(side_effect=AssertionError("liveness must not probe SQLite")),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_health_returns_200_when_probes_pass(monkeypatch):
    _all_probes_ok(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["dependencies"]["redis"]["ok"] is True
    assert data["dependencies"]["sqlite"]["ok"] is True
    assert data["dependencies"]["disk"]["ok"] is True


@pytest.mark.asyncio
async def test_health_returns_503_when_redis_unreachable(monkeypatch):
    _all_probes_ok(monkeypatch)

    async def _failing_redis(*_args, **_kwargs):
        return ProbeResult(False, "redis down")

    monkeypatch.setattr("scrapeyard.main.probe_redis", _failing_redis)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["dependencies"]["redis"]["ok"] is False


@pytest.mark.asyncio
async def test_readiness_probes_every_database_and_fails_on_one(monkeypatch):
    _all_probes_ok(monkeypatch)
    seen: list[str] = []

    async def _sqlite(db_name: str):
        seen.append(db_name)
        return ProbeResult(db_name != "errors.db", "errors unavailable")

    monkeypatch.setattr(main_module, "probe_sqlite", _sqlite)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert set(seen) == {"jobs.db", "errors.db", "results_meta.db"}
    assert response.json()["dependencies"]["sqlite_errors"]["ok"] is False


@pytest.mark.asyncio
async def test_sqlite_readiness_reports_sanitized_operation_and_error(monkeypatch):
    async def _failed_probe(_db_name: str) -> None:
        raise SQLiteProbeError(
            "jobs.db",
            "quick_check(1)",
            "DatabaseError (SQLITE_CORRUPT)",
        )

    monkeypatch.setattr("scrapeyard.runtime.health.probe_db", _failed_probe)

    result = await main_module.probe_sqlite("jobs.db")

    assert result == ProbeResult(
        False,
        "jobs.db SQLite quick_check(1) failed: DatabaseError (SQLITE_CORRUPT)",
    )


@pytest.mark.asyncio
async def test_readiness_fails_when_required_background_task_stops(monkeypatch):
    _all_probes_ok(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "_background_probes",
        lambda: {
            "worker": ProbeResult(True),
            "scheduler": ProbeResult(True),
            "cleanup": ProbeResult(False, "cleanup task failed: RuntimeError"),
            "webhook": ProbeResult(True),
        },
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["background_tasks"]["cleanup"] == {
        "ok": False,
        "detail": "cleanup task failed: RuntimeError",
    }


@pytest.mark.asyncio
async def test_timed_probe_has_explicit_timeout():
    async def _blocked():
        await asyncio.Event().wait()
        return ProbeResult(True)

    result = await main_module._timed_async_probe("blocked", _blocked(), 0.01)

    assert result == ProbeResult(False, "blocked probe timed out after 0.01s")


@pytest.mark.asyncio
async def test_health_response_shape(monkeypatch):
    _all_probes_ok(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")
    data = response.json()
    assert "status" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], int | float)
    assert "workers" in data
    workers = data["workers"]
    assert "max_concurrent" in workers
    assert "active_tasks" in workers
    assert "max_browsers" in workers
    assert "active_browsers" in workers
    assert workers["queue_depths"] == {"high": 2, "normal": 3, "low": 5}
    assert "projects" in data
    assert isinstance(data["projects"], dict)
    assert "dependencies" in data
    for key in ("redis", "sqlite", "disk"):
        assert key in data["dependencies"]


@pytest.mark.asyncio
async def test_health_queue_depth_failure_is_stable_and_marks_redis_unhealthy(
    monkeypatch,
):
    _all_probes_ok(monkeypatch)
    monkeypatch.setattr(
        "scrapeyard.queue.pool.WorkerPool.queue_depths",
        AsyncMock(side_effect=ConnectionError("depth unavailable")),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["workers"]["queue_depths"] == {
        "high": None,
        "normal": None,
        "low": None,
    }
    assert response.json()["dependencies"]["redis"] == {
        "ok": False,
        "detail": "redis queue depth probe failed: ConnectionError",
    }
    assert "depth unavailable" not in response.text


@pytest.mark.asyncio
async def test_health_omits_project_summary_by_default(monkeypatch):
    _all_probes_ok(monkeypatch)
    project_summary = AsyncMock(side_effect=AssertionError("project summary should not load"))
    monkeypatch.setattr(main_module._health, "project_summary", project_summary)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.json()["projects"] == {}
    project_summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_includes_project_summary_when_enabled(monkeypatch):
    _all_probes_ok(monkeypatch)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            health_include_projects=True,
            storage_results_dir="/tmp",
            health_disk_free_min_mb=0,
            health_probe_timeout_seconds=2.0,
        ),
    )
    project_summary = AsyncMock(return_value={"private-project": {"job_count": 1}})
    monkeypatch.setattr(main_module._health, "project_summary", project_summary)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.json()["projects"] == {"private-project": {"job_count": 1}}
    project_summary.assert_awaited_once()
