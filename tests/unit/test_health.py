"""Test the /health endpoint."""

import pytest
from httpx import ASGITransport, AsyncClient

from scrapeyard.main import app


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
