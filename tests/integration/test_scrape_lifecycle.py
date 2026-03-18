"""Integration tests for on-demand scrape lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from scrapeyard.engine.scraper import TargetResult


def _async_scrape_yaml() -> str:
    return """
project: integ
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


@pytest.mark.asyncio
async def test_scrape_lifecycle_eventually_returns_results(client, monkeypatch):
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_async_scrape_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)

    payload = response.json()
    job_id = payload["job_id"]

    for _ in range(40):
        results_response = await client.get(f"/results/{job_id}")
        if results_response.status_code == 200:
            results_payload = results_response.json()
            assert results_payload["job_id"] == job_id
            assert "results" in results_payload
            return
        assert results_response.status_code == 202
        await asyncio.sleep(0.05)

    pytest.fail("Timed out waiting for /results/{job_id} to return 200")


@pytest.mark.asyncio
async def test_errors_are_recorded_on_failed_scrape(client, monkeypatch):
    async def _failing_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="failed",
            data=[],
            errors=["boom"],
            pages_scraped=0,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _failing_scrape_target)

    response = await client.post(
        "/scrape",
        content=_async_scrape_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]

    for _ in range(40):
        job_response = await client.get(f"/jobs/{job_id}")
        assert job_response.status_code == 200
        status = job_response.json()["status"]
        if status in {"failed", "partial", "complete"}:
            break
        await asyncio.sleep(0.05)

    errors_response = await client.get(f"/errors?job_id={job_id}")
    assert errors_response.status_code == 200
    errors = errors_response.json()
    assert len(errors) >= 1
    assert all(e["job_id"] == job_id for e in errors)
    assert any(e["error_message"] == "boom" for e in errors)


@pytest.mark.asyncio
async def test_duplicate_adhoc_scrape_does_not_collide(client, monkeypatch):
    """Submitting the same ad-hoc config twice must not hit UNIQUE constraint."""
    import re

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    yaml_config = _async_scrape_yaml()

    resp1 = await client.post(
        "/scrape",
        content=yaml_config,
        headers={"content-type": "application/x-yaml"},
    )
    assert resp1.status_code in (200, 202), f"First submission failed: {resp1.status_code}"

    resp2 = await client.post(
        "/scrape",
        content=yaml_config,
        headers={"content-type": "application/x-yaml"},
    )
    assert resp2.status_code in (200, 202), f"Second submission failed: {resp2.status_code}"

    job_id_1 = resp1.json()["job_id"]
    job_id_2 = resp2.json()["job_id"]
    assert job_id_1 != job_id_2, "Two submissions should produce different job IDs"

    # Verify each job has a suffixed name
    job1_resp = await client.get(f"/jobs/{job_id_1}")
    job2_resp = await client.get(f"/jobs/{job_id_2}")
    assert job1_resp.status_code == 200
    assert job2_resp.status_code == 200

    name1 = job1_resp.json()["name"]
    name2 = job2_resp.json()["name"]
    assert name1 != name2, "Two ad-hoc jobs from same config must have different names"

    suffix_pattern = re.compile(r"^async-scrape-[0-9a-f]{8}$")
    assert suffix_pattern.match(name1), f"Name {name1!r} doesn't match expected suffix pattern"
    assert suffix_pattern.match(name2), f"Name {name2!r} doesn't match expected suffix pattern"
