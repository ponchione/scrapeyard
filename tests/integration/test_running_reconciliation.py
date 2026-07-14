"""In-process recovery of terminalization failures without an app restart."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.run_lifecycle import handle_crash
from scrapeyard.queue.running_reconciliation import reconcile_stale_running_jobs
from scrapeyard.storage.database import init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.webhook_outbox import SQLiteWebhookOutboxStore


CONFIG_YAML = """
project: recovery
name: failed-terminalization
webhook:
  url: https://hooks.example.com/recovery
  on: [failed]
target:
  url: https://example.com
  selectors:
    title: h1
"""


async def test_periodic_reconciliation_repairs_failed_crash_finalization_once(
    tmp_path,
) -> None:
    await init_db(str(tmp_path / "db"))
    store = SQLiteJobStore()
    run_id = "run-failed-terminalization"
    job_id = "job-failed-terminalization"
    started_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    await store.save_job(
        Job(
            job_id=job_id,
            project="recovery",
            name="failed-terminalization",
            status=JobStatus.queued,
            config_yaml=CONFIG_YAML,
            updated_at=started_at,
            current_run_id=run_id,
            current_trigger="adhoc",
        )
    )
    assert await store.claim_run(
        run_id,
        job_id,
        "adhoc",
        hashlib.sha256(CONFIG_YAML.encode()).hexdigest(),
        started_at,
    )

    unavailable_store = AsyncMock()
    unavailable_store.fail_owned_run.side_effect = RuntimeError("jobs database unavailable")
    finalized = await handle_crash(
        job_id,
        run_id,
        unavailable_store,
        failed_at=started_at + timedelta(seconds=1),
    )
    assert finalized is False
    assert (await store.get_job(job_id)).status is JobStatus.running

    result_store = AsyncMock()
    result_store.get_result_metadata.return_value = None
    notifier = AsyncMock()
    recovered_at = started_at + timedelta(seconds=601)
    first = await reconcile_stale_running_jobs(
        job_store=store,
        result_store=result_store,
        heartbeat_timeout_seconds=600,
        batch_size=10,
        webhook_notifier=notifier,
        now=recovered_at,
    )
    second = await reconcile_stale_running_jobs(
        job_store=store,
        result_store=result_store,
        heartbeat_timeout_seconds=600,
        batch_size=10,
        webhook_notifier=notifier,
        now=recovered_at + timedelta(seconds=1),
    )

    assert first.recovered == 1
    assert first.terminal_intents.repaired == 1
    assert second.recovered == 0
    assert second.terminal_intents.inspected == 0
    assert (await store.get_job(job_id)).status is JobStatus.failed
    runs = await store.get_job_runs(job_id)
    assert len(runs) == 1
    assert runs[0].status is JobStatus.failed
    deliveries = await SQLiteWebhookOutboxStore().list_pending()
    matching = [delivery for delivery in deliveries if delivery.run_id == run_id]
    assert len(matching) == 1
    notifier.notify.assert_awaited_once_with()
