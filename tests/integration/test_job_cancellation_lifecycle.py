"""HTTP cancellation/deletion lifecycle coverage across local stores and workers."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    get_worker_pool,
)
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import JobStatus
from scrapeyard.models.job import Job
from scrapeyard.queue.cancellation import (
    QueueCancellationOutcome,
    RunCancellationResult,
)
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookDeliveryCreate,
)
from scrapeyard.storage.database import get_db
from tests.integration.conftest import poll_until_ready


def _yaml(*, name: str = "cancel-integration") -> str:
    return f"""project: integ
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


async def _success(*_args, **_kwargs):
    return TargetResult(
        url="https://example.com",
        status="success",
        data=[{"title": "kept"}],
        pages_scraped=1,
    )


async def _submit(client, yaml: str):
    return await client.post(
        "/scrape",
        content=yaml,
        headers={"content-type": "application/x-yaml"},
    )


async def _await_terminal(client, job_id: str):
    return await poll_until_ready(
        lambda: client.get(f"/jobs/{job_id}"),
        lambda response: response.json().get("status")
        in {"complete", "partial", "failed"},
    )


async def test_running_cancel_stops_worker_without_failed_state_or_side_effects(
    client,
    monkeypatch,
):
    started = asyncio.Event()

    async def _blocked(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _blocked)
    response = await _submit(client, _yaml())
    job_id = response.json()["job_id"]
    await asyncio.wait_for(started.wait(), timeout=2)

    cancelled = await client.post(f"/jobs/{job_id}/cancel")

    assert cancelled.status_code == 204
    detail = (await client.get(f"/jobs/{job_id}")).json()
    assert detail["status"] == "cancelled"
    assert detail["schedule_enabled"] is False
    assert detail["runs"][0]["status"] == "cancelled"
    assert detail["runs"][0]["completed_at"] is not None
    await asyncio.sleep(0.05)
    assert (await client.get(f"/results/{job_id}")).status_code == 404
    errors = await client.get(f"/errors?job_id={job_id}")
    assert errors.json() == []
    assert await SQLiteWebhookOutboxStore().list_pending() == []


async def test_cancel_is_idempotent_and_terminal_or_unknown_conflicts(
    client,
    monkeypatch,
):
    started = asyncio.Event()

    async def _blocked(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _blocked)
    response = await _submit(client, _yaml(name="idempotent-cancel"))
    job_id = response.json()["job_id"]
    await asyncio.wait_for(started.wait(), timeout=2)
    assert (await client.post(f"/jobs/{job_id}/cancel")).status_code == 204
    assert (await client.post(f"/jobs/{job_id}/cancel")).status_code == 204
    assert (await client.post("/jobs/missing/cancel")).status_code == 404

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    terminal = await _submit(client, _yaml(name="terminal-cancel"))
    terminal_id = terminal.json()["job_id"]
    await _await_terminal(client, terminal_id)
    conflict = await client.post(f"/jobs/{terminal_id}/cancel")
    assert conflict.status_code == 409


async def test_redis_unavailable_cancel_fails_closed_but_retry_completes(
    client,
    monkeypatch,
):
    started = asyncio.Event()

    async def _blocked(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _blocked)
    response = await _submit(client, _yaml(name="redis-fail-closed"))
    job_id = response.json()["job_id"]
    await asyncio.wait_for(started.wait(), timeout=2)
    pool = get_worker_pool()
    real_cancel = pool.cancel_run
    monkeypatch.setattr(
        pool,
        "cancel_run",
        lambda run_id: asyncio.sleep(
            0,
            result=RunCancellationResult(
                run_id,
                QueueCancellationOutcome.unavailable,
            ),
        ),
    )

    unavailable = await client.post(f"/jobs/{job_id}/cancel")

    assert unavailable.status_code == 503
    assert (await get_job_store().get_job(job_id)).status is JobStatus.cancelled
    monkeypatch.setattr(pool, "cancel_run", real_cancel)
    assert (await client.post(f"/jobs/{job_id}/cancel")).status_code == 204


async def test_queued_cancel_succeeds_when_redis_delivery_is_already_missing(
    client,
):
    await get_job_store().save_job(
        Job(
            job_id="queued-missing",
            project="integ",
            name="queued-missing",
            config_yaml=_yaml(name="queued-missing"),
            status=JobStatus.queued,
            updated_at=datetime.now(timezone.utc),
            current_run_id="run-missing",
        )
    )

    response = await client.post("/jobs/queued-missing/cancel")

    assert response.status_code == 204
    job = await get_job_store().get_job("queued-missing")
    assert job.status is JobStatus.cancelled
    assert job.current_run_id == "run-missing"
    assert (await client.delete("/jobs/queued-missing")).status_code == 204


async def test_delete_active_conflict_then_cancelled_delete_is_idempotent(
    client,
    monkeypatch,
):
    started = asyncio.Event()

    async def _blocked(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _blocked)
    response = await _submit(client, _yaml(name="delete-active"))
    job_id = response.json()["job_id"]
    await asyncio.wait_for(started.wait(), timeout=2)
    conflict = await client.delete(f"/jobs/{job_id}")
    assert conflict.status_code == 409

    assert (await client.post(f"/jobs/{job_id}/cancel")).status_code == 204
    assert (await client.delete(f"/jobs/{job_id}")).status_code == 204
    assert (await client.delete(f"/jobs/{job_id}")).status_code == 204
    assert (await client.get(f"/jobs/{job_id}")).status_code == 404


async def test_redis_unavailable_deletion_retains_reservation_for_retry(
    client,
    monkeypatch,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="delete-redis-fail"))
    job_id = response.json()["job_id"]
    await _await_terminal(client, job_id)
    pool = get_worker_pool()
    real_inspect = pool.inspect_delivery
    monkeypatch.setattr(
        pool,
        "inspect_delivery",
        AsyncMock(side_effect=ConnectionError("redis unavailable")),
    )

    unavailable = await client.delete(f"/jobs/{job_id}")

    assert unavailable.status_code == 503
    assert (await get_job_store().get_job(job_id)).status is JobStatus.deleting
    monkeypatch.setattr(pool, "inspect_delivery", real_inspect)
    assert (await client.delete(f"/jobs/{job_id}")).status_code == 204


async def test_delete_results_false_preserves_latest_and_explicit_result_access(
    client,
    monkeypatch,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="preserve-results"))
    job_id = response.json()["job_id"]
    result = await poll_until_ready(
        lambda: client.get(f"/results/{job_id}"),
        lambda response: response.status_code == 200,
    )
    run_id = result.json()["run_id"]

    deleted = await client.delete(f"/jobs/{job_id}?delete_results=false")

    assert deleted.status_code == 204
    assert (await client.get(f"/jobs/{job_id}")).status_code == 404
    latest = await client.get(f"/results/{job_id}")
    explicit = await client.get(f"/results/{job_id}?latest=false&run_id={run_id}")
    assert latest.status_code == 200
    assert explicit.status_code == 200
    assert latest.json() == explicit.json()


async def test_delete_results_true_removes_metadata_and_artifact(client, monkeypatch):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="remove-results"))
    job_id = response.json()["job_id"]
    result = await poll_until_ready(
        lambda: client.get(f"/results/{job_id}"),
        lambda response: response.status_code == 200,
    )
    run_id = result.json()["run_id"]
    metadata = await get_result_store().get_result_metadata(job_id, run_id)
    assert metadata is not None

    assert (
        await client.delete(f"/jobs/{job_id}?delete_results=true")
    ).status_code == 204
    assert (await client.get(f"/results/{job_id}")).status_code == 404
    assert await get_result_store().get_result_metadata(job_id, run_id) is None


async def test_delete_results_true_removes_artifact_without_metadata(client, monkeypatch):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="remove-unindexed-artifacts"))
    job_id = response.json()["job_id"]
    result = await poll_until_ready(
        lambda: client.get(f"/results/{job_id}"),
        lambda response: response.status_code == 200,
    )
    run_id = result.json()["run_id"]
    metadata = await get_result_store().get_result_metadata(job_id, run_id)
    assert metadata is not None
    run_dir = Path(metadata.file_path)
    (run_dir / "artifacts").mkdir(exist_ok=True)
    (run_dir / "artifacts" / "page.png").write_bytes(b"debug")
    async with get_db("results_meta.db") as db:
        await db.execute(
            "DELETE FROM results_meta WHERE job_id = ? AND run_id = ?",
            (job_id, run_id),
        )
        await db.commit()

    assert (await client.delete(f"/jobs/{job_id}?delete_results=true")).status_code == 204
    assert not run_dir.exists()


async def test_pending_webhook_blocks_delete_until_terminal(client, monkeypatch):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="pending-block"))
    job_id = response.json()["job_id"]
    detail = await _await_terminal(client, job_id)
    run_id = detail.json()["runs"][0]["run_id"]
    delivery = WebhookDeliveryCreate(
        delivery_id="manual-pending",
        job_id=job_id,
        run_id=run_id,
        event="job.complete",
        url="https://hooks.example.com/job",
        headers={},
        timeout_seconds=5,
        payload={"delivery_id": "manual-pending"},
        next_attempt_at=datetime.now(timezone.utc),
    )
    outbox = SQLiteWebhookOutboxStore()
    await outbox.enqueue_delivery(delivery)

    conflict = await client.delete(f"/jobs/{job_id}")

    assert conflict.status_code == 409
    assert (await get_job_store().get_job(job_id)).status is JobStatus.complete
    await outbox.mark_delivered("manual-pending", delivered_at=datetime.now(timezone.utc))
    assert (await client.delete(f"/jobs/{job_id}")).status_code == 204
    assert await outbox.get_delivery("manual-pending") is None


async def test_deleting_resumes_after_error_cleanup_fault_and_policy_conflicts(
    client,
    monkeypatch,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="resume-delete"))
    job_id = response.json()["job_id"]
    await _await_terminal(client, job_id)
    error_store = get_error_store()
    real_delete = error_store.delete_errors_for_job
    calls = 0

    async def _fail_once(target_job_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected errors.db fault")
        await real_delete(target_job_id)

    monkeypatch.setattr(error_store, "delete_errors_for_job", _fail_once)
    failed = await client.delete(f"/jobs/{job_id}?delete_results=false")
    assert failed.status_code == 500
    assert (await get_job_store().get_job(job_id)).status is JobStatus.deleting
    conflict = await client.delete(f"/jobs/{job_id}?delete_results=true")
    assert conflict.status_code == 409
    resumed = await client.delete(f"/jobs/{job_id}?delete_results=false")
    assert resumed.status_code == 204


async def test_deleting_resumes_after_filesystem_cleanup_fault(
    client,
    monkeypatch,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="filesystem-fault"))
    job_id = response.json()["job_id"]
    result = await poll_until_ready(
        lambda: client.get(f"/results/{job_id}"),
        lambda response: response.status_code == 200,
    )
    run_id = result.json()["run_id"]
    metadata = await get_result_store().get_result_metadata(job_id, run_id)
    assert metadata is not None
    artifact = Path(metadata.file_path) / "results.json"
    assert artifact.is_file()
    from scrapeyard.storage import result_store as result_store_module

    real_remove = result_store_module.remove_directories

    def _fail_filesystem(_paths) -> None:
        raise PermissionError("injected filesystem fault")

    monkeypatch.setattr(result_store_module, "remove_directories", _fail_filesystem)
    failed = await client.delete(f"/jobs/{job_id}?delete_results=true")
    assert failed.status_code == 500
    assert artifact.is_file()
    assert await get_result_store().get_result_metadata(job_id, run_id) is not None

    monkeypatch.setattr(result_store_module, "remove_directories", real_remove)
    assert (
        await client.delete(f"/jobs/{job_id}?delete_results=true")
    ).status_code == 204
    assert not artifact.exists()


async def test_deleting_resumes_after_results_metadata_delete_fault(
    client,
    monkeypatch,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="results-db-fault"))
    job_id = response.json()["job_id"]
    result = await poll_until_ready(
        lambda: client.get(f"/results/{job_id}"),
        lambda response: response.status_code == 200,
    )
    run_id = result.json()["run_id"]
    async with get_db("results_meta.db") as db:
        await db.executescript(
            """CREATE TRIGGER reject_result_delete
               BEFORE DELETE ON results_meta
               BEGIN
                   SELECT RAISE(ABORT, 'injected results_meta.db fault');
               END;"""
        )
        await db.commit()

    failed = await client.delete(f"/jobs/{job_id}?delete_results=true")
    assert failed.status_code == 500
    assert await get_result_store().get_result_metadata(job_id, run_id) is not None
    async with get_db("results_meta.db") as db:
        await db.execute("DROP TRIGGER reject_result_delete")
        await db.commit()

    assert (
        await client.delete(f"/jobs/{job_id}?delete_results=true")
    ).status_code == 204
    assert await get_result_store().get_result_metadata(job_id, run_id) is None


async def test_deleting_resumes_after_final_jobs_db_fault(client, monkeypatch):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    response = await _submit(client, _yaml(name="final-jobs-fault"))
    job_id = response.json()["job_id"]
    await _await_terminal(client, job_id)
    job_store = get_job_store()
    real_finalize = job_store.finalize_job_deletion
    calls = 0

    async def _fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected final jobs.db fault")
        return await real_finalize(*args, **kwargs)

    monkeypatch.setattr(job_store, "finalize_job_deletion", _fail_once)
    failed = await client.delete(f"/jobs/{job_id}?delete_results=false")
    assert failed.status_code == 500
    assert (await job_store.get_job(job_id)).status is JobStatus.deleting
    assert (
        await client.delete(f"/jobs/{job_id}?delete_results=false")
    ).status_code == 204
