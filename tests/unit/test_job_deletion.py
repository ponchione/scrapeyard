"""Resumable jobs.db deletion reservation coverage for audit item 07."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.types import (
    DeletionFinalizationAction,
    DeletionReservationAction,
)
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookDeliveryCreate,
    WebhookFailureReason,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
YAML = "project: test\nname: delete\ntarget:\n  url: https://example.com\n  selectors:\n    title: h1\n"


@pytest.fixture()
async def store(tmp_path) -> SQLiteJobStore:
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


async def _save(store: SQLiteJobStore, status: JobStatus) -> None:
    await store.save_job(
        Job(
            job_id="job-1",
            project="test",
            name="delete",
            config_yaml=YAML,
            status=status,
            updated_at=NOW,
            current_run_id="run-1",
            current_trigger="adhoc",
            schedule_cron="*/5 * * * *",
        )
    )


async def _terminal_run(store: SQLiteJobStore) -> None:
    await _save(store, JobStatus.queued)
    assert await store.claim_run(
        "run-1",
        "job-1",
        "adhoc",
        hashlib.sha256(YAML.encode()).hexdigest(),
        NOW,
    )
    await store.finalize_owned_run(
        "job-1",
        "run-1",
        "complete",
        1,
        0,
        NOW + timedelta(seconds=1),
        NOW - timedelta(seconds=1),
    )


def _delivery(delivery_id: str = "delivery-1") -> WebhookDeliveryCreate:
    return WebhookDeliveryCreate(
        delivery_id=delivery_id,
        job_id="job-1",
        run_id="run-1",
        event="job.complete",
        url="https://hooks.example.com/job",
        headers={},
        timeout_seconds=5,
        payload={"delivery_id": delivery_id},
        next_attempt_at=NOW,
    )


@pytest.mark.parametrize("status", [JobStatus.queued, JobStatus.running])
async def test_active_jobs_cannot_reserve_deletion(store, status):
    await _save(store, status)
    outcome = await store.reserve_job_deletion(
        "job-1",
        delete_results=False,
        requested_at=NOW,
    )
    assert outcome.action is DeletionReservationAction.active_conflict
    assert (await store.get_job("job-1")).status is status


async def test_reservation_persists_policy_and_resumes_idempotently(store):
    await _terminal_run(store)

    first = await store.reserve_job_deletion(
        "job-1",
        delete_results=True,
        requested_at=NOW + timedelta(seconds=2),
    )
    repeated = await store.reserve_job_deletion(
        "job-1",
        delete_results=True,
        requested_at=NOW + timedelta(seconds=3),
    )
    conflict = await store.reserve_job_deletion(
        "job-1",
        delete_results=False,
        requested_at=NOW + timedelta(seconds=4),
    )

    job = await store.get_job("job-1")
    assert first.action is DeletionReservationAction.created
    assert repeated.action is DeletionReservationAction.resumed
    assert conflict.action is DeletionReservationAction.policy_conflict
    assert job.status is JobStatus.deleting
    assert job.schedule_enabled is False
    assert job.delete_results_on_delete is True
    assert job.deletion_requested_at == NOW + timedelta(seconds=2)


async def test_pending_webhook_blocks_reservation_without_mutating_parent(store):
    await _terminal_run(store)
    await SQLiteWebhookOutboxStore().enqueue_delivery(_delivery(), now=NOW)

    outcome = await store.reserve_job_deletion(
        "job-1",
        delete_results=False,
        requested_at=NOW,
    )

    assert outcome.action is DeletionReservationAction.pending_webhook_conflict
    assert (await store.get_job("job-1")).status is JobStatus.complete


@pytest.mark.parametrize("terminal_kind", ["delivered", "failed", "scrubbed"])
async def test_final_deletion_removes_terminal_delivery_run_and_parent(
    store,
    terminal_kind,
):
    await _terminal_run(store)
    outbox = SQLiteWebhookOutboxStore()
    await outbox.enqueue_delivery(_delivery(), now=NOW)
    if terminal_kind == "delivered":
        await outbox.mark_delivered("delivery-1", delivered_at=NOW)
    else:
        await outbox.mark_failed(
            "delivery-1",
            failed_at=NOW,
            reason=WebhookFailureReason.non_retryable_failure,
            last_error="failure",
        )
        if terminal_kind == "scrubbed":
            await outbox.scrub_terminal_deliveries(
                delivered_before=NOW,
                failed_before=NOW,
                scrubbed_at=NOW + timedelta(seconds=1),
                limit=10,
            )
    assert (
        await store.reserve_job_deletion(
            "job-1",
            delete_results=False,
            requested_at=NOW + timedelta(seconds=2),
        )
    ).action is DeletionReservationAction.created

    outcome = await store.finalize_job_deletion(
        "job-1",
        delete_results=False,
    )

    assert outcome.action is DeletionFinalizationAction.deleted
    with pytest.raises(KeyError):
        await store.get_job("job-1")
    assert await outbox.get_delivery("delivery-1") is None
    assert await store.get_job_run("job-1", "run-1") is None


async def test_missing_reservation_and_finalization_are_idempotent(store):
    reservation = await store.reserve_job_deletion(
        "missing",
        delete_results=False,
        requested_at=NOW,
    )
    finalized = await store.finalize_job_deletion(
        "missing",
        delete_results=False,
    )
    assert reservation.action is DeletionReservationAction.missing
    assert finalized.action is DeletionFinalizationAction.missing


async def test_finalization_rechecks_pending_webhook(store):
    await _terminal_run(store)
    await store.reserve_job_deletion(
        "job-1",
        delete_results=False,
        requested_at=NOW,
    )
    await SQLiteWebhookOutboxStore().enqueue_delivery(_delivery(), now=NOW)

    outcome = await store.finalize_job_deletion(
        "job-1",
        delete_results=False,
    )

    assert outcome.action is DeletionFinalizationAction.pending_webhook_conflict
    assert (await store.get_job("job-1")).status is JobStatus.deleting


async def test_deleting_parent_is_not_terminal_reconciliation_candidate(store):
    await _terminal_run(store)
    await store.reserve_job_deletion(
        "job-1",
        delete_results=False,
        requested_at=NOW,
    )
    assert await store.list_terminal_webhook_candidates() == []
    assert not await store.queue_run(
        "job-1",
        expected_status=JobStatus.deleting.value,
        expected_run_id="run-1",
        new_run_id="run-2",
        new_trigger="adhoc",
        queued_at=NOW + timedelta(seconds=1),
    )


async def test_final_deletion_removes_all_jobs_db_rows_in_one_transaction(store):
    await _terminal_run(store)
    await store.reserve_job_deletion(
        "job-1",
        delete_results=False,
        requested_at=NOW,
    )
    await store.finalize_job_deletion("job-1", delete_results=False)
    async with get_db("jobs.db") as db:
        counts = []
        for table in ("jobs", "job_runs", "webhook_deliveries"):
            row = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            counts.append(row[0])
    assert counts == [0, 0, 0]
