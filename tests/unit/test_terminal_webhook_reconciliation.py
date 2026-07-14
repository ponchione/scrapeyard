"""Startup repair coverage for terminal webhook intent and parent convergence."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.terminal_reconciliation import (
    TerminalIntentReconciliationError,
    reconcile_terminal_webhook_intents,
)
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.types import ResultMetadata, ScheduledJobMutationAction
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookFailureReason,
    WebhookDeliveryStatus,
)
from scrapeyard.webhook.payload import deterministic_delivery_id

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _yaml(
    *,
    webhook_on: str | None = "complete",
    target: str = "example.com",
    webhook_url: str = "https://hooks.example.com/recovery",
) -> str:
    webhook = ""
    if webhook_on is not None:
        webhook = f"""
webhook:
  url: {webhook_url}
  on: [{webhook_on}]
"""
    return f"""project: test
name: recovery-job
target:
  url: https://{target}
  selectors:
    title: h1
{webhook}"""


@pytest.fixture()
async def store(tmp_path) -> SQLiteJobStore:
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


async def _claim(
    store: SQLiteJobStore,
    config_yaml: str,
    *,
    job_id: str = "job-1",
    run_id: str = "run-1",
) -> None:
    await store.save_job(
        Job(
            job_id=job_id,
            project="test",
            name=f"recovery-job-{job_id}",
            config_yaml=config_yaml,
            status=JobStatus.queued,
            updated_at=NOW,
            current_run_id=run_id,
        )
    )
    assert await store.claim_run(
        run_id,
        job_id,
        "adhoc",
        hashlib.sha256(config_yaml.encode()).hexdigest(),
        NOW,
    )


async def _terminal_without_intent(
    store: SQLiteJobStore,
    config_yaml: str,
    *,
    status: JobStatus = JobStatus.complete,
    record_count: int = 4,
    error_count: int = 2,
    job_id: str = "job-1",
    run_id: str = "run-1",
) -> None:
    await _claim(store, config_yaml, job_id=job_id, run_id=run_id)
    await store.finalize_owned_run(
        job_id,
        run_id,
        status.value,
        record_count,
        error_count,
        NOW + timedelta(minutes=1),
        NOW - timedelta(seconds=1),
    )
    # Simulate a pre-marker terminal row or a crash-recovered run whose terminal
    # webhook decision was not committed atomically.
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE job_runs SET webhook_reconciled_at = NULL WHERE run_id = ?",
            (run_id,),
        )
        await db.commit()


def _result_store(metadata: ResultMetadata | None = None) -> AsyncMock:
    store = AsyncMock()
    store.get_result_metadata.return_value = metadata
    return store


async def test_periodic_terminal_reconciliation_applies_batch_limit() -> None:
    store = AsyncMock()
    store.list_terminal_webhook_candidates.return_value = []

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=AsyncMock(),
        batch_size=17,
    )

    assert summary.inspected == 0
    store.list_terminal_webhook_candidates.assert_awaited_once_with(limit=17)


async def test_startup_repairs_missing_intent_and_is_idempotent(
    store: SQLiteJobStore,
) -> None:
    config_yaml = _yaml()
    await _terminal_without_intent(store, config_yaml)
    metadata = ResultMetadata(
        job_id="job-1",
        project="test",
        run_id="run-1",
        status="complete",
        record_count=4,
        file_path="/tmp/results/test/recovery-job/run-1",
        created_at=NOW,
    )
    results = _result_store(metadata)

    first = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=results,
    )
    second = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=results,
    )

    delivery_id = deterministic_delivery_id(
        job_id="job-1",
        run_id="run-1",
        event="job.complete",
    )
    delivery = await SQLiteWebhookOutboxStore().get_delivery(delivery_id)
    assert first.repaired == 1
    assert second.inspected == 0
    assert delivery is not None
    assert delivery.payload["delivery_id"] == delivery_id
    assert delivery.payload["result_path"] == metadata.file_path
    async with get_db("jobs.db") as db:
        row = await (await db.execute("SELECT COUNT(*) FROM webhook_deliveries")).fetchone()
    assert row is not None and row[0] == 1


@pytest.mark.parametrize(
    "persistent_status",
    [
        WebhookDeliveryStatus.pending,
        WebhookDeliveryStatus.delivered,
        WebhookDeliveryStatus.failed,
    ],
)
async def test_reconciliation_leaves_existing_delivery_state_unchanged(
    store: SQLiteJobStore,
    persistent_status: WebhookDeliveryStatus,
) -> None:
    await _terminal_without_intent(store, _yaml())
    results = _result_store()
    await reconcile_terminal_webhook_intents(job_store=store, result_store=results)
    delivery_id = deterministic_delivery_id(
        job_id="job-1",
        run_id="run-1",
        event="job.complete",
    )
    outbox = SQLiteWebhookOutboxStore()
    if persistent_status is WebhookDeliveryStatus.delivered:
        await outbox.mark_delivered(
            delivery_id,
            delivered_at=NOW + timedelta(minutes=2),
            attempts=2,
        )
    elif persistent_status is WebhookDeliveryStatus.failed:
        await outbox.mark_failed(
            delivery_id,
            failed_at=NOW + timedelta(minutes=2),
            reason=WebhookFailureReason.non_retryable_failure,
            last_error="HTTP 400",
            attempts=3,
        )
    before = await outbox.get_delivery(delivery_id)

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=results,
    )
    after = await outbox.get_delivery(delivery_id)

    assert summary.inspected == 0
    assert before is not None and after is not None
    assert after.status is persistent_status
    assert after.attempts == before.attempts
    assert after.updated_at == before.updated_at


async def test_missing_result_metadata_uses_jobs_db_snapshot(
    store: SQLiteJobStore,
    caplog,
) -> None:
    await _terminal_without_intent(store, _yaml(), record_count=7, error_count=3)

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(None),
    )

    delivery_id = deterministic_delivery_id(
        job_id="job-1",
        run_id="run-1",
        event="job.complete",
    )
    delivery = await SQLiteWebhookOutboxStore().get_delivery(delivery_id)
    assert summary.metadata_missing == 1
    assert summary.repaired == 1
    assert delivery is not None
    assert delivery.payload["result_path"] is None
    assert delivery.payload["result_count"] == 7
    assert delivery.payload["error_count"] == 3
    assert "result metadata missing" in caplog.text


async def test_result_metadata_database_failure_still_repairs_intent(
    store: SQLiteJobStore,
) -> None:
    await _terminal_without_intent(store, _yaml())
    results = _result_store()
    results.get_result_metadata.side_effect = RuntimeError("metadata DB unavailable")

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=results,
    )

    assert summary.metadata_missing == 1
    assert summary.repaired == 1


@pytest.mark.parametrize("webhook_on", [None, "failed"])
async def test_reconciliation_creates_no_intent_when_not_applicable(
    store: SQLiteJobStore,
    webhook_on: str | None,
) -> None:
    await _terminal_without_intent(store, _yaml(webhook_on=webhook_on))

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(),
    )

    assert summary.not_required == 1
    assert await SQLiteWebhookOutboxStore().list_pending() == []


async def test_reconciliation_converges_running_parent_to_terminal_run(
    store: SQLiteJobStore,
) -> None:
    await _terminal_without_intent(store, _yaml())
    async with get_db("jobs.db") as db:
        await db.execute("UPDATE jobs SET status = 'running' WHERE job_id = 'job-1'")
        await db.commit()

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(),
    )

    assert summary.parent_converged == 1
    assert (await store.get_job("job-1")).status is JobStatus.complete


async def test_stale_running_recovery_eventually_repairs_failed_intent(
    store: SQLiteJobStore,
) -> None:
    config_yaml = _yaml(webhook_on="failed")
    await _claim(store, config_yaml)
    recoveries = await store.recover_stale_running_jobs(
        NOW + timedelta(seconds=1),
        NOW + timedelta(minutes=1),
    )
    assert len(recoveries) == 1

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(),
    )

    delivery_id = deterministic_delivery_id(
        job_id="job-1",
        run_id="run-1",
        event="job.failed",
    )
    assert summary.repaired == 1
    assert await SQLiteWebhookOutboxStore().get_delivery(delivery_id) is not None


async def test_stale_scheduled_run_uses_snapshot_after_future_config_update(
    store: SQLiteJobStore,
) -> None:
    old_config = _yaml(webhook_on="failed")
    new_config = _yaml(
        webhook_on="complete",
        target="future.example.com",
        webhook_url="https://hooks.example.com/future",
    )
    await store.save_job(
        Job(
            job_id="job-1",
            project="test",
            name="recovery-job",
            config_yaml=old_config,
            status=JobStatus.queued,
            updated_at=NOW,
            schedule_cron="*/5 * * * *",
            current_run_id="run-1",
        )
    )
    assert await store.claim_run(
        "run-1",
        "job-1",
        "scheduled",
        hashlib.sha256(old_config.encode()).hexdigest(),
        NOW,
    )
    assert await store.recover_stale_run(
        "job-1",
        "run-1",
        NOW + timedelta(seconds=1),
        NOW + timedelta(minutes=1),
    )
    updated = await store.update_scheduled_job(
        "job-1",
        project="test",
        name="recovery-job",
        config_yaml=new_config,
        schedule_cron="*/10 * * * *",
        schedule_timezone="UTC",
        schedule_enabled=True,
        updated_at=NOW + timedelta(minutes=2),
    )

    first = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(),
    )
    second = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(),
    )

    delivery_id = deterministic_delivery_id(
        job_id="job-1",
        run_id="run-1",
        event="job.failed",
    )
    delivery = await SQLiteWebhookOutboxStore().get_delivery(delivery_id)
    assert updated.action is ScheduledJobMutationAction.updated
    assert first.repaired == 1
    assert second.inspected == 0
    assert delivery is not None
    assert delivery.url == "https://hooks.example.com/recovery"
    assert (await store.get_job("job-1")).config_yaml == new_config


async def test_parent_config_change_does_not_replace_run_snapshot(
    store: SQLiteJobStore,
) -> None:
    await _terminal_without_intent(store, _yaml())
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE jobs SET config_yaml = ? WHERE job_id = 'job-1'",
            (_yaml(target="changed.example.com"),),
        )
        await db.commit()

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(),
    )

    assert summary.repaired == 1
    pending = await SQLiteWebhookOutboxStore().list_pending()
    assert len(pending) == 1
    assert pending[0].url == "https://hooks.example.com/recovery"


async def test_bad_candidate_does_not_block_later_candidate_in_same_pass(
    store: SQLiteJobStore,
) -> None:
    await _terminal_without_intent(
        store,
        _yaml(),
        job_id="job-a",
        run_id="run-a",
    )
    await _terminal_without_intent(
        store,
        _yaml(),
        job_id="job-b",
        run_id="run-b",
    )
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE job_runs SET config_yaml = 'invalid: yaml: value' "
            "WHERE run_id = 'run-a'"
        )
        await db.commit()

    with pytest.raises(TerminalIntentReconciliationError):
        await reconcile_terminal_webhook_intents(
            job_store=store,
            result_store=_result_store(),
        )

    repaired_id = deterministic_delivery_id(
        job_id="job-b",
        run_id="run-b",
        event="job.complete",
    )
    assert await SQLiteWebhookOutboxStore().get_delivery(repaired_id) is not None


async def test_bad_candidate_is_rotated_behind_later_bounded_batch(
    store: SQLiteJobStore,
) -> None:
    await _terminal_without_intent(
        store,
        _yaml(),
        job_id="job-a",
        run_id="run-a",
    )
    await _terminal_without_intent(
        store,
        _yaml(),
        job_id="job-b",
        run_id="run-b",
    )
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE job_runs SET config_yaml = 'invalid: yaml: value' "
            "WHERE run_id = 'run-a'"
        )
        await db.commit()

    with pytest.raises(TerminalIntentReconciliationError):
        await reconcile_terminal_webhook_intents(
            job_store=store,
            result_store=_result_store(),
            batch_size=1,
        )

    summary = await reconcile_terminal_webhook_intents(
        job_store=store,
        result_store=_result_store(),
        batch_size=1,
    )

    assert summary.repaired == 1
    repaired_id = deterministic_delivery_id(
        job_id="job-b",
        run_id="run-b",
        event="job.complete",
    )
    assert await SQLiteWebhookOutboxStore().get_delivery(repaired_id) is not None
