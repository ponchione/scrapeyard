from __future__ import annotations

import asyncio

import pytest

from scrapeyard.api.dependencies import get_worker_pool
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.storage.database import get_db


def _yaml(*, name: str = "idempotent") -> str:
    return f"""
project: idem-integration
name: {name}
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


def _headers(key: str | None = None) -> dict[str, str]:
    headers = {"content-type": "application/x-yaml"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.mark.asyncio
async def test_matching_key_replays_same_job_and_run(client, monkeypatch):
    release = asyncio.Event()

    async def _slow_target(*_args, **_kwargs):
        await release.wait()
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "ok"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _slow_target)
    try:
        first = await client.post(
            "/scrape", content=_yaml(), headers=_headers("stable-key")
        )
        replay = await client.post(
            "/scrape", content=_yaml(), headers=_headers("stable-key")
        )
    finally:
        release.set()

    assert first.status_code == replay.status_code == 202
    assert first.json()["job_id"] == replay.json()["job_id"]
    assert first.json()["run_id"] == replay.json()["run_id"]
    assert "Idempotency-Replayed" not in first.headers
    assert replay.headers["Idempotency-Replayed"] == "true"


@pytest.mark.asyncio
async def test_conflicting_yaml_returns_409_without_leaking_key(client, caplog):
    key = "plaintext-key-must-not-be-logged"
    first = await client.post(
        "/scrape", content=_yaml(), headers=_headers(key)
    )
    conflict = await client.post(
        "/scrape", content=_yaml(name="changed"), headers=_headers(key)
    )

    assert first.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json() == {
        "error": "Idempotency-Key was already used with different request content",
        "code": "conflict",
        "status_code": 409,
        "details": [],
    }
    assert key not in caplog.text


@pytest.mark.asyncio
async def test_missing_key_keeps_non_idempotent_behavior(client):
    first = await client.post("/scrape", content=_yaml(), headers=_headers())
    second = await client.post("/scrape", content=_yaml(), headers=_headers())

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]
    assert first.json()["run_id"] != second.json()["run_id"]


@pytest.mark.asyncio
async def test_expired_key_creates_a_new_submission(client):
    first = await client.post(
        "/scrape", content=_yaml(), headers=_headers("expiring-key")
    )
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE scrape_idempotency SET expires_at = '2000-01-01T00:00:00+00:00'"
        )
        await db.commit()
    second = await client.post(
        "/scrape", content=_yaml(), headers=_headers("expiring-key")
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] != second.json()["job_id"]


@pytest.mark.asyncio
async def test_concurrent_matching_requests_call_enqueue_once(client, monkeypatch):
    pool = get_worker_pool()
    original_enqueue = pool.enqueue
    enqueue_calls = 0

    async def _counted_enqueue(*args, **kwargs):
        nonlocal enqueue_calls
        enqueue_calls += 1
        return await original_enqueue(*args, **kwargs)

    monkeypatch.setattr(pool, "enqueue", _counted_enqueue)
    responses = await asyncio.gather(
        *(
            client.post(
                "/scrape",
                content=_yaml(),
                headers=_headers("concurrent-key"),
            )
            for _ in range(20)
        )
    )

    assert {response.status_code for response in responses} == {202}
    assert len({response.json()["job_id"] for response in responses}) == 1
    assert len({response.json()["run_id"] for response in responses}) == 1
    assert enqueue_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    ["", "contains space", "x" * 129],
)
async def test_invalid_key_is_rejected(client, key):
    response = await client.post(
        "/scrape", content=_yaml(), headers=_headers(key)
    )
    assert response.status_code == 400
