"""Integration tests for webhook dispatch during async scrape lifecycle."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests.integration.conftest import poll_until_ready
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.common.time import utc_now
from scrapeyard.queue.terminal_reconciliation import reconcile_terminal_webhook_intents
from scrapeyard.storage.cleanup import run_cleanup
from scrapeyard.storage.database import get_db
from scrapeyard.webhook.dispatcher import (
    WebhookDispatchReason,
    WebhookDispatchResult,
    WebhookDispatchStatus,
)


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

    # Patch the singleton dispatcher's one-attempt sender to capture payloads.
    from scrapeyard.api.dependencies import (
        get_webhook_dispatcher,
        get_webhook_outbox_store,
    )
    dispatcher = get_webhook_dispatcher()

    async def _capture_dispatch(config, payload):
        dispatched_payloads.append(payload)
        return WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)

    monkeypatch.setattr(dispatcher, "send_once", _capture_dispatch)

    response = await client.post(
        "/scrape",
        content=_webhook_yaml("https://hooks.example.com/test"),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]

    await poll_until_ready(
        lambda: client.get(f"/jobs/{job_id}"),
        lambda response: response.json()["status"] in ("complete", "partial", "failed"),
        failure_message=f"Timed out waiting for terminal status for {job_id}",
    )

    # Give the create_task webhook a tick to run.
    await asyncio.sleep(0.1)

    assert len(dispatched_payloads) == 1
    payload = dispatched_payloads[0]
    assert payload["job_id"] == job_id
    assert payload["event"] == "job.complete"
    assert payload["project"] == "integ"
    assert payload["run_id"] is not None
    assert payload["results_url"] is not None
    delivery = await get_webhook_outbox_store().get_delivery(payload["delivery_id"])
    assert delivery is not None
    assert delivery.delivery_id == payload["delivery_id"]
    assert delivery.payload == payload


@pytest.mark.asyncio
async def test_retention_cleanup_preserves_reconciliation_dedup_evidence(
    client,
    monkeypatch,
):
    """A scrubbed terminal row cannot be recreated or redelivered."""

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)
    from scrapeyard.api.dependencies import (
        get_job_store,
        get_result_store,
        get_webhook_dispatcher,
        get_webhook_outbox_store,
    )

    dispatcher = get_webhook_dispatcher()
    dispatched_payloads: list[dict] = []

    async def _capture_dispatch(config, payload):
        dispatched_payloads.append(payload)
        return WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)

    monkeypatch.setattr(dispatcher, "send_once", _capture_dispatch)
    response = await client.post(
        "/scrape",
        content=_webhook_yaml("https://hooks.example.com/retention"),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]
    await poll_until_ready(
        lambda: client.get(f"/jobs/{job_id}"),
        lambda result: result.json()["status"] == "complete",
    )
    await poll_until_ready(
        lambda: get_webhook_outbox_store().get_delivery(
            dispatched_payloads[0]["delivery_id"]
        )
        if dispatched_payloads
        else asyncio.sleep(0, result=None),
        lambda delivery: delivery is not None and delivery.status.value == "delivered",
        failure_message="Timed out waiting for webhook delivery",
    )
    delivery_id = dispatched_payloads[0]["delivery_id"]

    await run_cleanup(
        get_result_store(),
        retention_days=30,
        max_results_per_job=100,
        webhook_outbox_store=get_webhook_outbox_store(),
        webhook_delivered_retention_days=1,
        webhook_failed_retention_days=30,
        webhook_cleanup_batch_size=10,
        now=utc_now() + timedelta(days=2),
    )
    tombstone = await get_webhook_outbox_store().get_delivery(delivery_id)
    assert tombstone is not None and tombstone.is_scrubbed
    assert tombstone.payload == {}
    assert tombstone.headers == {}
    assert tombstone.url == ""

    first = await reconcile_terminal_webhook_intents(
        job_store=get_job_store(),
        result_store=get_result_store(),
    )
    second = await reconcile_terminal_webhook_intents(
        job_store=get_job_store(),
        result_store=get_result_store(),
    )
    await asyncio.sleep(0.05)

    assert first.inspected == 0
    assert second.inspected == 0
    assert len(dispatched_payloads) == 1
    async with get_db("jobs.db") as db:
        row = await (
            await db.execute(
                "SELECT COUNT(*) FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
        ).fetchone()
    assert row is not None and row[0] == 1


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
    from scrapeyard.api.dependencies import (
        get_job_store,
        get_webhook_dispatcher,
        get_webhook_outbox_store,
    )
    dispatcher = get_webhook_dispatcher()

    async def _failing_dispatch(config, payload):
        raise Exception("webhook boom")

    monkeypatch.setattr(dispatcher, "send_once", _failing_dispatch)

    response = await client.post(
        "/scrape",
        content=_webhook_yaml("https://hooks.example.com/test"),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]

    job_resp = await poll_until_ready(
        lambda: client.get(f"/jobs/{job_id}"),
        lambda response: response.json()["status"] in ("complete", "partial", "failed"),
        failure_message=f"Timed out waiting for terminal status for {job_id}",
    )
    status = job_resp.json()["status"]

    assert status == "complete"

    persisted_job = await get_job_store().get_job(job_id)
    run_id = persisted_job.current_run_id
    assert run_id is not None
    from scrapeyard.webhook.payload import deterministic_delivery_id

    delivery = await get_webhook_outbox_store().get_delivery(
        deterministic_delivery_id(
            job_id=job_id,
            run_id=run_id,
            event="job.complete",
        )
    )
    assert delivery is not None
    assert delivery.payload["status"] == "complete"

    # Results should still be persisted.
    results_resp = await client.get(f"/results/{job_id}")
    assert results_resp.status_code == 200


@pytest.mark.asyncio
async def test_webhook_attempt_exhaustion_does_not_change_terminal_run(
    client,
    monkeypatch,
):
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)
    from scrapeyard.api.dependencies import (
        get_job_store,
        get_webhook_dispatcher,
        get_webhook_outbox_store,
    )
    from scrapeyard.storage.webhook_outbox import WebhookFailureReason
    from scrapeyard.webhook.payload import deterministic_delivery_id

    dispatcher = get_webhook_dispatcher()
    dispatcher._max_delivery_attempts = 1

    async def _retryable_failure(config, payload):
        return WebhookDispatchResult(
            WebhookDispatchStatus.retryable_failed,
            1,
            "Transport failure: TimeoutException",
            WebhookDispatchReason.transport_failure,
        )

    monkeypatch.setattr(dispatcher, "send_once", _retryable_failure)
    response = await client.post(
        "/scrape",
        content=_webhook_yaml("https://hooks.example.com/exhaustion"),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]
    await poll_until_ready(
        lambda: client.get(f"/jobs/{job_id}"),
        lambda result: result.json()["status"] == "complete",
    )
    job = await get_job_store().get_job(job_id)
    assert job.current_run_id is not None
    delivery_id = deterministic_delivery_id(
        job_id=job_id,
        run_id=job.current_run_id,
        event="job.complete",
    )
    delivery = await poll_until_ready(
        lambda: get_webhook_outbox_store().get_delivery(delivery_id),
        lambda row: row is not None and row.status.value == "failed",
        failure_message="Timed out waiting for webhook attempt exhaustion",
    )

    assert delivery.failure_reason is WebhookFailureReason.attempt_exhausted
    assert (await get_job_store().get_job(job_id)).status.value == "complete"
    run = await get_job_store().get_job_run(job_id, job.current_run_id)
    assert run is not None and run.status.value == "complete"


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

    monkeypatch.setattr(dispatcher, "send_once", _tracking_dispatch)

    response = await client.post(
        "/scrape",
        content=_no_webhook_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code in (200, 202)
    job_id = response.json()["job_id"]

    await poll_until_ready(
        lambda: client.get(f"/jobs/{job_id}"),
        lambda response: response.json()["status"] in ("complete", "partial", "failed"),
        failure_message=f"Timed out waiting for terminal status for {job_id}",
    )

    await asyncio.sleep(0.1)

    assert len(dispatch_calls) == 0
