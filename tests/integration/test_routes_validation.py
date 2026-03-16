"""Tests for input validation error responses in API routes."""

from __future__ import annotations

import pytest


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
async def test_errors_invalid_since_returns_400(client):
    """Invalid ISO date in 'since' param should return 400."""
    response = await client.get("/errors?since=not-a-date")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_errors_invalid_error_type_returns_400(client):
    """Invalid error_type enum value should return 400."""
    response = await client.get("/errors?error_type=bogus")
    assert response.status_code == 400
