"""Tests for input validation error responses in API routes."""

from __future__ import annotations

import pytest

from scrapeyard.api.dependencies import get_worker_pool


@pytest.mark.asyncio
async def test_scrape_invalid_yaml_returns_422(client):
    """Malformed YAML should return 422, not 500."""
    response = await client.post(
        "/scrape",
        content="not_a_valid_config: [",
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_scrape_missing_required_field_returns_422(client):
    """Valid YAML but missing required 'project' field should return 422."""
    response = await client.post(
        "/scrape",
        content="name: test\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_jobs_bad_yaml_returns_422(client):
    """Malformed YAML to POST /jobs should return 422."""
    response = await client.post(
        "/jobs",
        content="{{invalid yaml",
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_jobs_rejects_non_yaml_content_type(client):
    response = await client.post(
        "/jobs",
        content="project: integ\nname: nope",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 415
    assert "application/x-yaml" in response.json()["error"]


@pytest.mark.asyncio
async def test_errors_invalid_since_returns_400(client):
    """Invalid ISO date in 'since' param should return 400."""
    response = await client.get("/errors?since=not-a-date")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_errors_invalid_error_type_returns_400(client):
    """Invalid error_type enum value should return 400."""
    response = await client.get("/errors?error_type=bogus")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_jobs_limit_above_max_returns_400(client):
    response = await client.get("/jobs?limit=501")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_errors_limit_above_max_returns_400(client):
    response = await client.get("/errors?limit=501")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_async_scrape_enqueue_memory_error_returns_503_and_removes_job(client, monkeypatch):
    """Async enqueue rejection should return 503 without leaving a stranded job."""
    pool = get_worker_pool()

    async def _reject(*_args, **_kwargs):
        raise MemoryError("over memory limit")

    monkeypatch.setattr(pool, "enqueue", _reject)

    response = await client.post(
        "/scrape",
        content="""
project: integ
name: rejected-job
execution:
  mode: async
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
""",
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 503
    jobs = (await client.get("/jobs?project=integ")).json()
    assert jobs == []
