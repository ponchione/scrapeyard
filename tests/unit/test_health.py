"""Test the /health endpoint."""

import logging
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from scrapeyard.common.logging import setup_logging
from scrapeyard.main import app
from scrapeyard.runtime.health import ProbeResult


def test_setup_logging_is_idempotent(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    logger = logging.Logger("scrapeyard-test")
    with patch("logging.getLogger", return_value=logger):
        setup_logging(str(log_dir))
        first_count = len(logger.handlers)
        setup_logging(str(log_dir))
        assert len(logger.handlers) == first_count


def test_setup_logging_uses_configured_level(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    logger = logging.Logger("scrapeyard-test")
    with patch("logging.getLogger", return_value=logger):
        setup_logging(str(log_dir), "debug")
    assert logger.level == logging.DEBUG


def test_setup_logging_rejects_unknown_level(tmp_path) -> None:
    log_dir = tmp_path / "logs"
    logger = logging.Logger("scrapeyard-test")
    with patch("logging.getLogger", return_value=logger):
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


@pytest.mark.asyncio
async def test_health_returns_200_when_probes_pass(monkeypatch):
    _all_probes_ok(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
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
        response = await client.get("/health")
    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
    assert data["dependencies"]["redis"]["ok"] is False


@pytest.mark.asyncio
async def test_health_response_shape(monkeypatch):
    _all_probes_ok(monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    data = response.json()
    assert "status" in data
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], (int, float))
    assert "workers" in data
    workers = data["workers"]
    assert "max_concurrent" in workers
    assert "active_tasks" in workers
    assert "max_browsers" in workers
    assert "active_browsers" in workers
    assert "projects" in data
    assert isinstance(data["projects"], dict)
    assert "dependencies" in data
    for key in ("redis", "sqlite", "disk"):
        assert key in data["dependencies"]
