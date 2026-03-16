"""Integration tests for webhook dispatch during async scrape lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from scrapeyard.engine.scraper import TargetResult


def _webhook_yaml(webhook_url: str) -> str:
    return f"""
project: integ
name: webhook-test
execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
webhook:
  url: "{webhook_url}"
  on: [complete, partial, failed]
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


def _no_webhook_yaml() -> str:
    return """
project: integ
name: no-webhook-test
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


@pytest.mark.asyncio
async def test_webhook_received_after_async_job(client, monkeypatch):
    """Mock HTTP server receives webhook payload after async job completes."""
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com", status="success",
            data=[{"title": "Hello"}], pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    dispatched_payloads: list[dict] = []

    # Patch the singleton dispatcher's dispatch method to capture payloads.
    from scrapeyard.api.dependencies import get_webhook_dispatcher
    dispatcher = get_webhook_dispatcher()

    async def _capture_dispatch(config, payload):
        dispatched_payloads.append(payload)

    monkeypatch.setattr(dispatcher, "dispatch", _capture_dispatch)

    response = await client.post(
        "/scrape",
        content=_webhook_yaml("https://hooks.example.com/test"),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]

    # Wait for the async job to complete.
    for _ in range(40):
        job_resp = await client.get(f"/jobs/{job_id}")
        if job_resp.json()["status"] in ("complete", "partial", "failed"):
            break
        await asyncio.sleep(0.05)

    # Give the create_task webhook a tick to run.
    await asyncio.sleep(0.1)

    assert len(dispatched_payloads) == 1
    payload = dispatched_payloads[0]
    assert payload["job_id"] == job_id
    assert payload["event"] == "job.complete"
    assert payload["project"] == "integ"
    assert payload["run_id"] is not None
    assert payload["results_url"] is not None


@pytest.mark.asyncio
async def test_webhook_failure_does_not_affect_job(client, monkeypatch):
    """Webhook dispatch exception does not affect job status or results."""
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com", status="success",
            data=[{"title": "Hello"}], pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    # Patch dispatcher to raise (simulating failure).
    from scrapeyard.api.dependencies import get_webhook_dispatcher
    dispatcher = get_webhook_dispatcher()

    async def _failing_dispatch(config, payload):
        raise Exception("webhook boom")

    monkeypatch.setattr(dispatcher, "dispatch", _failing_dispatch)

    response = await client.post(
        "/scrape",
        content=_webhook_yaml("https://hooks.example.com/test"),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]

    # Wait for job completion.
    for _ in range(40):
        job_resp = await client.get(f"/jobs/{job_id}")
        status = job_resp.json()["status"]
        if status in ("complete", "partial", "failed"):
            break
        await asyncio.sleep(0.05)

    assert status == "complete"

    # Results should still be persisted.
    results_resp = await client.get(f"/results/{job_id}")
    assert results_resp.status_code == 200


@pytest.mark.asyncio
async def test_no_webhook_block_completes_normally(client, monkeypatch):
    """Job without webhook config completes normally with no webhook attempt."""
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com", status="success",
            data=[{"title": "Hello"}], pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    dispatch_calls: list = []

    from scrapeyard.api.dependencies import get_webhook_dispatcher
    dispatcher = get_webhook_dispatcher()

    async def _tracking_dispatch(config, payload):
        dispatch_calls.append(payload)

    monkeypatch.setattr(dispatcher, "dispatch", _tracking_dispatch)

    response = await client.post(
        "/scrape",
        content=_no_webhook_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]

    for _ in range(40):
        job_resp = await client.get(f"/jobs/{job_id}")
        if job_resp.json()["status"] in ("complete", "partial", "failed"):
            break
        await asyncio.sleep(0.05)

    await asyncio.sleep(0.1)

    assert len(dispatch_calls) == 0
