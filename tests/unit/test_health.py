"""Test the /health endpoint."""

import logging
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from scrapeyard.common.logging import setup_logging
from scrapeyard.main import app


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


@pytest.mark.asyncio
async def test_health_returns_200():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_health_response_shape():
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
