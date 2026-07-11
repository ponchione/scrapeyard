"""Real-SQLite coverage for atomic terminal state and webhook intent."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from scrapeyard.config.loader import load_config
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.types import RunOwnershipError, TerminalIntentAction
from scrapeyard.storage.webhook_outbox import SQLiteWebhookOutboxStore
from scrapeyard.webhook.payload import build_terminal_webhook_delivery

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _yaml(*, webhook_on: str | None = "complete") -> str:
    webhook = ""
    if webhook_on is not None:
        webhook = f"""
webhook:
  url: https://hooks.example.com/terminal
  on: [{webhook_on}]
"""
    return f"""project: test
name: atomic-job
target:
  url: https://example.com
  selectors:
    title: h1
{webhook}"""


@pytest.fixture()
async def store(tmp_path) -> SQLiteJobStore:
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


async def _claim(
    store: SQLiteJobStore,
    *,
    config_yaml: str,
    job_id: str = "job-1",
    run_id: str = "run-1",
) -> None:
    await store.save_job(
        Job(
            job_id=job_id,
            project="test",
            name="atomic-job",
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


def _delivery(
    config_yaml: str,
    *,
    status: JobStatus = JobStatus.complete,
    completed_at: datetime = NOW + timedelta(minutes=1),
):
    return build_terminal_webhook_delivery(
        config=load_config(config_yaml),
        job_id="job-1",
        status=status,
        run_id="run-1",
        result_path="/tmp/results/test/atomic-job/run-1",
        result_count=2,
        error_count=1,
        started_at=NOW,
        completed_at=completed_at,
    )


async def test_finalization_atomically_creates_one_required_intent(
    store: SQLiteJobStore,
) -> None:
    config_yaml = _yaml()
    await _claim(store, config_yaml=config_yaml)
    delivery = _delivery(config_yaml)
    assert delivery is not None

    await store.finalize_owned_run(
        "job-1",
        "run-1",
        "complete",
        2,
        1,
        NOW + timedelta(minutes=1),
        NOW - timedelta(seconds=1),
        webhook_delivery=delivery,
    )

    job = await store.get_job("job-1")
    run = await store.get_job_run("job-1", "run-1")
    persisted = await SQLiteWebhookOutboxStore().get_delivery(delivery.delivery_id)
    assert job.status is JobStatus.complete
    assert run is not None and run.status is JobStatus.complete
    assert persisted is not None
    assert persisted.delivery_id == persisted.payload["delivery_id"]
    assert persisted.delivery_id == delivery.delivery_id

    with pytest.raises(RunOwnershipError):
        await store.finalize_owned_run(
            "job-1",
            "run-1",
            "complete",
            2,
            1,
            NOW + timedelta(minutes=2),
            NOW - timedelta(seconds=1),
            webhook_delivery=delivery,
        )
    async with get_db("jobs.db") as db:
        row = await (
            await db.execute("SELECT COUNT(*) FROM webhook_deliveries")
        ).fetchone()
    assert row is not None and row[0] == 1


@pytest.mark.parametrize("webhook_on", [None, "failed"])
async def test_non_applicable_terminal_status_creates_no_intent(
    store: SQLiteJobStore,
    webhook_on: str | None,
) -> None:
    config_yaml = _yaml(webhook_on=webhook_on)
    await _claim(store, config_yaml=config_yaml)
    assert _delivery(config_yaml) is None

    await store.finalize_owned_run(
        "job-1",
        "run-1",
        "complete",
        2,
        0,
        NOW + timedelta(minutes=1),
        NOW - timedelta(seconds=1),
    )

    assert await SQLiteWebhookOutboxStore().list_pending() == []


async def test_ownership_loss_mutates_neither_terminal_state_nor_intent(
    store: SQLiteJobStore,
) -> None:
    config_yaml = _yaml()
    await _claim(store, config_yaml=config_yaml)
    delivery = _delivery(config_yaml, completed_at=NOW + timedelta(minutes=20))
    assert delivery is not None

    with pytest.raises(RunOwnershipError):
        await store.finalize_owned_run(
            "job-1",
            "run-1",
            "complete",
            2,
            0,
            NOW + timedelta(minutes=20),
            NOW + timedelta(minutes=10),
            webhook_delivery=delivery,
        )

    assert (await store.get_job("job-1")).status is JobStatus.running
    run = await store.get_job_run("job-1", "run-1")
    assert run is not None and run.status is JobStatus.running
    assert await SQLiteWebhookOutboxStore().get_delivery(delivery.delivery_id) is None


async def test_webhook_insert_fault_rolls_back_run_job_and_intent(
    store: SQLiteJobStore,
) -> None:
    config_yaml = _yaml()
    await _claim(store, config_yaml=config_yaml)
    delivery = _delivery(config_yaml)
    assert delivery is not None
    async with get_db("jobs.db") as db:
        await db.executescript(
            """CREATE TRIGGER reject_terminal_webhook
               BEFORE INSERT ON webhook_deliveries
               BEGIN
                   SELECT RAISE(ABORT, 'test webhook insert failure');
               END;"""
        )
        await db.commit()

    with pytest.raises(aiosqlite.IntegrityError, match="test webhook insert failure"):
        await store.finalize_owned_run(
            "job-1",
            "run-1",
            "complete",
            2,
            0,
            NOW + timedelta(minutes=1),
            NOW - timedelta(seconds=1),
            webhook_delivery=delivery,
        )

    assert (await store.get_job("job-1")).status is JobStatus.running
    run = await store.get_job_run("job-1", "run-1")
    assert run is not None and run.status is JobStatus.running
    assert await SQLiteWebhookOutboxStore().get_delivery(delivery.delivery_id) is None


async def test_conditional_crash_failure_persists_failed_intent_atomically(
    store: SQLiteJobStore,
) -> None:
    config_yaml = _yaml(webhook_on="failed")
    await _claim(store, config_yaml=config_yaml)
    delivery = _delivery(config_yaml, status=JobStatus.failed)
    assert delivery is not None

    await store.fail_owned_run(
        "job-1",
        "run-1",
        NOW + timedelta(minutes=1),
        error_count=3,
        webhook_delivery=delivery,
    )

    run = await store.get_job_run("job-1", "run-1")
    persisted = await SQLiteWebhookOutboxStore().get_delivery(delivery.delivery_id)
    assert run is not None and run.status is JobStatus.failed
    assert run.record_count == 0
    assert run.error_count == 3
    assert persisted is not None and persisted.event == "job.failed"


async def test_repair_rechecks_terminal_snapshot_before_inserting_intent(
    store: SQLiteJobStore,
) -> None:
    config_yaml = _yaml()
    await _claim(store, config_yaml=config_yaml)
    await store.finalize_owned_run(
        "job-1",
        "run-1",
        "complete",
        2,
        0,
        NOW + timedelta(minutes=1),
        NOW - timedelta(seconds=1),
    )
    candidate = (await store.list_terminal_webhook_candidates())[0]
    delivery = _delivery(config_yaml)
    assert delivery is not None
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE job_runs SET status = 'partial' WHERE run_id = 'run-1'"
        )
        await db.commit()

    outcome = await store.reconcile_terminal_webhook_candidate(
        candidate,
        delivery,
    )

    assert outcome.action is TerminalIntentAction.race_noop
    assert await SQLiteWebhookOutboxStore().get_delivery(delivery.delivery_id) is None
