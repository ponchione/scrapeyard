"""Live Redis integration tests for the real queue and embedded arq worker."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from arq.constants import abort_jobs_ss
from arq.connections import RedisSettings

from scrapeyard.api.dependencies import (
    get_job_store,
    get_webhook_dispatcher,
    get_webhook_outbox_store,
    get_worker_pool,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.pool import QueueDeliveryState, WorkerPool
from scrapeyard.queue.reconciliation import reconcile_stale_queued_jobs
from scrapeyard.webhook.payload import deterministic_delivery_id


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


def _webhook_scrape_yaml() -> str:
    return """
project: live-redis
name: webhook-scrape
execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
webhook:
  url: https://hooks.example.com/live-redis
  on: [complete]
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


def _scheduled_scrape_yaml() -> str:
    return """
project: live-redis
name: manual-schedule
schedule:
  cron: "0 9 * * *"
  timezone: "America/New_York"
  enabled: false
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
        if isinstance(status, str) and status in {"complete", "partial", "failed"}:
            return status
        await asyncio.sleep(0.05)
    pytest.fail(f"Timed out waiting for terminal job status for {job_id}")


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_concurrent_idempotent_submissions_enqueue_one_real_redis_run(
    client,
    monkeypatch,
):
    pool = get_worker_pool()
    assert pool._worker is not None
    original_enqueue = pool.enqueue
    enqueue_calls = 0

    async def _counted_enqueue(*args, **kwargs):
        nonlocal enqueue_calls
        enqueue_calls += 1
        return await original_enqueue(*args, **kwargs)

    monkeypatch.setattr(pool, "enqueue", _counted_enqueue)
    pool._worker.allow_pick_jobs = False
    try:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/scrape",
                    content=_async_scrape_yaml(),
                    headers={
                        "content-type": "application/x-yaml",
                        "Idempotency-Key": "live-concurrent-key",
                    },
                )
                for _ in range(20)
            )
        )
        payloads = [response.json() for response in responses]
        assert {response.status_code for response in responses} == {202}
        assert len({payload["job_id"] for payload in payloads}) == 1
        assert len({payload["run_id"] for payload in payloads}) == 1
        assert enqueue_calls == 1
        assert await pool.queue_depths() == {"high": 0, "normal": 1, "low": 0}
    finally:
        pool._worker.allow_pick_jobs = True


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_manual_scheduled_trigger_uses_real_redis_and_explicit_trigger(
    client,
    monkeypatch,
):
    async def _success(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "manual"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    created = await client.post(
        "/jobs",
        content=_scheduled_scrape_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert created.status_code == 201
    job_id = created.json()["job_id"]

    triggered = await client.post(f"/jobs/{job_id}/trigger")
    assert triggered.status_code == 202
    assert triggered.json()["trigger"] == "manual"
    await _await_terminal_status(client, job_id)
    detail = await client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert detail.json()["runs"][0]["trigger"] == "manual"
    assert detail.json()["runs"][0]["config_hash"] == triggered.json()["config_hash"]


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_real_redis_priority_backlog_is_weighted_fifo_and_non_preemptive(
    live_app,
):
    """Exercise real Redis admission without making any network scrape."""

    del live_app
    settings = get_settings()
    started = asyncio.Event()
    release_running = asyncio.Event()
    seen: list[str] = []
    active = 0
    max_active = 0

    async def _record(job_id, *_args, **_kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        seen.append(job_id)
        try:
            if job_id == "normal-running":
                started.set()
                await release_running.wait()
        finally:
            active -= 1

    pool = WorkerPool(
        max_concurrent=1,
        max_browsers=1,
        memory_limit_mb=0,
        redis_settings=RedisSettings.from_dsn(settings.redis_dsn),
        queue_name=f"{settings.queue_name}:backlog",
        task_handler=_record,
    )
    await pool.start()
    assert pool._worker is not None
    pool._worker.poll_delay_s = 0.01
    pool._worker.allow_pick_jobs = False
    try:
        running_handle = await pool.enqueue(
            "normal-running",
            "config: test",
            "normal",
            run_id="priority-run-running",
        )
        pool._worker.allow_pick_jobs = True
        await asyncio.wait_for(started.wait(), timeout=2)

        handles = []
        run_ids: dict[str, str] = {}
        # Freeze the enqueue millisecond and reverse the run-id ordering so the
        # assertion proves FIFO scores rather than Redis's lexical tie-break.
        with patch(
            "scrapeyard.queue.pool.timestamp_ms",
            return_value=int(utc_now().timestamp() * 1000),
        ):
            for priority, names in (
                ("normal", ["normal-1", "normal-2", "normal-3"]),
                ("low", ["low-1", "low-2"]),
                ("high", [f"high-{index}" for index in range(8)]),
            ):
                for index, name in enumerate(names):
                    run_id = f"priority-run-{priority}-{99 - index:02d}"
                    run_ids[name] = run_id
                    handle = await pool.enqueue(
                        name,
                        "config: test",
                        priority,
                        run_id=run_id,
                    )
                    handles.append(handle)

            duplicate = await pool.enqueue(
                "must-not-run-duplicate",
                "config: duplicate",
                "low",
                run_id=run_ids["high-0"],
            )
        assert await pool.queue_depths() == {"high": 8, "normal": 3, "low": 2}
        for priority in ("high", "normal", "low"):
            assert (
                await pool.inspect_delivery(run_ids[f"{priority}-1"])
                is QueueDeliveryState.queued
            )

        # A later high cannot interrupt the handler already in progress.
        await asyncio.sleep(0.05)
        assert seen == ["normal-running"]
        release_running.set()

        await asyncio.gather(
            running_handle.result(timeout=10, poll_delay=0.01),
            duplicate.result(timeout=10, poll_delay=0.01),
            *(handle.result(timeout=10, poll_delay=0.01) for handle in handles),
        )

        assert seen[:7] == [
            "normal-running",
            "high-0",
            "high-1",
            "high-2",
            "normal-1",
            "normal-2",
            "low-1",
        ]
        assert [name for name in seen if name.startswith("high-")] == [
            f"high-{index}" for index in range(8)
        ]
        assert [name for name in seen if name.startswith("normal-")] == [
            "normal-running",
            "normal-1",
            "normal-2",
            "normal-3",
        ]
        assert [name for name in seen if name.startswith("low-")] == [
            "low-1",
            "low-2",
        ]
        assert "must-not-run-duplicate" not in seen
        assert max_active == 1
        assert await pool.queue_depths() == {"high": 0, "normal": 0, "low": 0}
    finally:
        pool._worker.allow_pick_jobs = True
        release_running.set()
        await pool.stop()


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_queued_cancellation_prevents_real_redis_handler_execution(
    client,
    monkeypatch,
):
    calls = 0

    async def _must_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "unexpected"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _must_not_run)
    pool = get_worker_pool()
    assert pool._worker is not None
    assert pool.redis is not None
    pool._worker.allow_pick_jobs = False
    try:
        response = await client.post(
            "/scrape",
            content=_async_scrape_yaml(),
            headers={"content-type": "application/x-yaml"},
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        job = await get_job_store().get_job(job_id)
        assert job.current_run_id is not None

        cancellation = asyncio.create_task(
            client.post(f"/jobs/{job_id}/cancel")
        )
        for _ in range(100):
            if await pool.redis.zscore(abort_jobs_ss, job.current_run_id) is not None:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Timed out waiting for the arq abort marker")
        pool._worker.allow_pick_jobs = True
        cancelled = await asyncio.wait_for(cancellation, timeout=3)

        assert cancelled.status_code == 204
        assert calls == 0
        stored = await get_job_store().get_job(job_id)
        assert stored.status is JobStatus.cancelled
        assert stored.current_run_id == job.current_run_id
        assert await pool.inspect_delivery(job.current_run_id) is QueueDeliveryState.complete
    finally:
        pool._worker.allow_pick_jobs = True


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
async def test_real_arq_worker_finalization_creates_deterministic_intent(
    client,
    monkeypatch,
):
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Durable Redis intent"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)
    pool = get_worker_pool()
    assert pool.redis is not None
    assert pool._worker is not None
    # Keep this lane focused on real arq finalization rather than external HTTP.
    get_webhook_dispatcher()._accepting_tasks = False

    response = await client.post(
        "/scrape",
        content=_webhook_scrape_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert await _await_terminal_status(client, job_id) == "complete"
    job = await get_job_store().get_job(job_id)
    assert job.current_run_id is not None

    delivery_id = deterministic_delivery_id(
        job_id=job_id,
        run_id=job.current_run_id,
        event="job.complete",
    )
    delivery = await get_webhook_outbox_store().get_delivery(delivery_id)
    assert delivery is not None
    assert delivery.payload["delivery_id"] == delivery_id
    assert delivery.run_id == job.current_run_id


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


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_delivery_inspection_reports_real_arq_present_and_missing(live_app):
    pool = get_worker_pool()
    redis = pool.redis
    assert redis is not None
    assert pool._worker is not None
    settings = get_settings()
    run_id = "run-live-inspection"

    queued = await redis.enqueue_job(
        "scrape_job",
        "job-live-inspection",
        _async_scrape_yaml(),
        run_id,
        _job_id=run_id,
        _queue_name=settings.queue_name,
        _defer_by=60,
    )
    assert queued is not None
    assert await pool.inspect_delivery(run_id) is QueueDeliveryState.deferred

    await redis.delete(f"arq:job:{run_id}")
    await redis.zrem(settings.queue_name, run_id)

    assert await pool.inspect_delivery(run_id) is QueueDeliveryState.missing


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_missing_delivery_recovery_runs_through_real_arq_worker(
    live_app,
    monkeypatch,
):
    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Recovered through Redis"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)
    pool = get_worker_pool()
    assert pool.redis is not None
    assert pool._worker is not None
    store = get_job_store()
    now = utc_now()
    run_id = "run-live-recovered"
    job_id = "job-live-recovered"
    await store.save_job(
        Job(
            job_id=job_id,
            project="live-redis",
            name="recovered-delivery",
            status=JobStatus.queued,
            config_yaml=_async_scrape_yaml(),
            updated_at=now - timedelta(seconds=2),
            current_run_id=run_id,
        )
    )
    # A sorted-set orphan without its arq payload is non-executable. Recovery
    # must replace it with the original run ID instead of treating membership
    # alone as duplicate delivery evidence.
    await pool.redis.zadd(
        f"{get_settings().queue_name}:priority:normal",
        {run_id: 1},
    )
    assert await pool.inspect_delivery(run_id) is QueueDeliveryState.missing

    summary = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=1,
        now=now,
    )

    assert summary.recovered == 1
    for _ in range(60):
        job = await store.get_job(job_id)
        if job.status in {JobStatus.complete, JobStatus.partial, JobStatus.failed}:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Recovered delivery did not finish through the real arq worker")

    assert job.status == JobStatus.complete
    assert job.current_run_id == run_id
    runs = await store.get_job_runs(job_id)
    assert [run.run_id for run in runs] == [run_id]
    assert await pool.inspect_delivery(run_id) is QueueDeliveryState.complete

    repeated = await reconcile_stale_queued_jobs(
        job_store=store,
        worker_pool=pool,
        queued_claim_timeout_seconds=1,
        now=now,
    )
    assert repeated.inspected == 0
