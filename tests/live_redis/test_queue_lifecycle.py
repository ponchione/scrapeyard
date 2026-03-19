"""Live Redis integration tests for the real queue and embedded arq worker."""

from __future__ import annotations

import asyncio
import json

import pytest

from scrapeyard.engine.scraper import TargetResult


def _async_scrape_yaml() -> str:
    return """
project: live-redis
name: async-scrape
execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


def _sync_scrape_yaml() -> str:
    return """
project: live-redis
name: sync-scrape
execution:
  mode: sync
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


async def _await_terminal_status(client, job_id: str) -> str:
    for _ in range(60):
        response = await client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
        status = response.json()["status"]
        if status in {"complete", "partial", "failed"}:
            return status
        await asyncio.sleep(0.05)
    pytest.fail(f"Timed out waiting for terminal job status for {job_id}")


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_async_scrape_lifecycle_uses_real_redis_queue(client, monkeypatch):
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello from Redis"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_async_scrape_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202

    job_id = response.json()["job_id"]
    status = await _await_terminal_status(client, job_id)
    assert status == "complete"

    results_response = await client.get(f"/results/{job_id}")
    assert results_response.status_code == 200
    payload = results_response.json()
    assert payload["job_id"] == job_id
    assert "Hello from Redis" in json.dumps(payload["results"])


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_sync_scrape_waits_for_real_redis_completion(client, monkeypatch):
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Sync Redis"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_sync_scrape_yaml(),
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "complete"
    assert "Sync Redis" in json.dumps(payload["results"])


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_failed_scrape_records_errors_through_real_redis_queue(client, monkeypatch):
    async def _failing_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="failed",
            data=[],
            errors=["redis lane boom"],
            pages_scraped=0,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _failing_scrape_target)

    response = await client.post(
        "/scrape",
        content=_async_scrape_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202

    job_id = response.json()["job_id"]
    status = await _await_terminal_status(client, job_id)
    assert status == "failed"

    errors_response = await client.get(f"/errors?job_id={job_id}")
    assert errors_response.status_code == 200
    errors = errors_response.json()
    assert any(error["error_message"] == "redis lane boom" for error in errors)
