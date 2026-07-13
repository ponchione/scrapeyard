"""Tests for input validation error responses in API routes."""

from __future__ import annotations

import pytest

from scrapeyard.common.yaml import MAX_YAML_NESTING

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


def _pathological_nested_config() -> str:
    lines = [
        "project: test",
        "name: nested",
        "password: must-not-be-echoed",
        "pathological:",
    ]
    lines.extend(
        f"{'  ' * (depth + 1)}level_{depth}:"
        for depth in range(MAX_YAML_NESTING)
    )
    lines.append(f"{'  ' * (MAX_YAML_NESTING + 1)}value: leaf")
    return "\n".join(lines)


async def test_scrape_excessive_yaml_nesting_returns_sanitized_422(client):
    response = await client.post(
        "/scrape",
        content=_pathological_nested_config(),
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 422
    body = response.text
    assert f"YAML nesting exceeds {MAX_YAML_NESTING} levels" in body
    assert "must-not-be-echoed" not in body
    assert "RecursionError" not in body
    assert "maximum recursion" not in body


async def test_scrape_defensively_translates_parser_recursion_error(
    client,
    monkeypatch,
):
    def _raise_recursion(_text: str) -> str:
        raise RecursionError("interpreter detail must not escape")

    monkeypatch.setattr("scrapeyard.api.routes.load_config_project", _raise_recursion)
    response = await client.post(
        "/scrape",
        content="project: test\n",
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 422
    assert f"YAML nesting exceeds {MAX_YAML_NESTING} levels" in response.text
    assert "interpreter detail" not in response.text


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
async def test_scrape_validation_error_redacts_secret_inputs(client):
    response = await client.post(
        "/scrape",
        content="""
project: integ
name: redacted-validation
proxy:
  url: http://user:pass@127.0.0.1:8080
target:
  url: https://example.com
  selectors:
    title: h1
""",
        headers={"content-type": "application/x-yaml"},
    )

    error = response.json()["error"]
    assert response.status_code == 422
    assert "user:pass" not in error
    assert "input_value" not in error
    assert "proxy.url" in error


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
async def test_jobs_invalid_cron_returns_422_without_saving_job(client):
    response = await client.post(
        "/jobs",
        content="""
project: integ
name: bad-cron
schedule:
  cron: "not a cron"
target:
  url: https://example.com
  selectors:
    title: h1
""",
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 422
    jobs = (await client.get("/jobs?project=integ")).json()
    assert jobs == []


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
