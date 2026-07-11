"""Tests for scrapeyard.scheduler.cron — SchedulerService."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.models.job import Job, JobRun, JobStatus
from scrapeyard.scheduler.cron import SchedulerService


NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
CONFIG_YAML = """
project: test
name: scheduled-job
target:
  url: https://example.com
  selectors:
    title: h1
"""


def _job(
    *,
    status: JobStatus = JobStatus.queued,
    run_id: str | None = None,
    updated_at: datetime | None = NOW,
) -> Job:
    return Job(
        job_id="job-1",
        project="test",
        name="scheduled-job",
        status=status,
        config_yaml=CONFIG_YAML,
        updated_at=updated_at,
        current_run_id=run_id,
    )


def _run(
    *,
    run_id: str,
    heartbeat_at: datetime,
    status: JobStatus = JobStatus.running,
) -> JobRun:
    return JobRun(
        run_id=run_id,
        job_id="job-1",
        status=status,
        trigger="scheduled",
        config_hash="config-hash",
        started_at=heartbeat_at - timedelta(minutes=30),
        heartbeat_at=heartbeat_at,
    )


def _make_service(**overrides):
    defaults = dict(
        worker_pool=MagicMock(),
        job_store=AsyncMock(),
        jitter_max_seconds=0,
    )
    defaults.update(overrides)
    return SchedulerService(**defaults)


def test_register_job_adds_a_job():
    svc = _make_service()
    svc.register_job("job-1", "*/5 * * * *")
    aps_job = svc._scheduler.get_job("job-1")
    assert aps_job is not None


def test_register_job_replaces_existing():
    svc = _make_service()
    svc.register_job("job-1", "*/5 * * * *")
    svc.register_job("job-1", "0 * * * *")
    aps_job = svc._scheduler.get_job("job-1")
    assert aps_job is not None


def test_remove_job_silent_for_nonexistent():
    svc = _make_service()
    svc.remove_job("no-such-job")


def test_remove_job_removes_existing():
    svc = _make_service()
    svc.register_job("job-1", "*/5 * * * *")
    svc.remove_job("job-1")
    assert svc._scheduler.get_job("job-1") is None


def test_shutdown_before_start_is_noop():
    svc = _make_service()
    svc.shutdown()


def test_get_next_run_time_returns_none_for_unknown():
    svc = _make_service()
    assert svc.get_next_run_time("unknown-job") is None


async def test_get_next_run_time_returns_datetime_for_registered():
    svc = _make_service()
    svc._scheduler.start()
    try:
        svc.register_job("job-1", "*/5 * * * *")
        nrt = svc.get_next_run_time("job-1")
        assert nrt is not None
    finally:
        svc._scheduler.shutdown(wait=False)


async def test_trigger_job_skips_if_running_heartbeat_is_fresh():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.running,
        run_id="run-active",
        updated_at=NOW - timedelta(hours=1),
    )
    job_store.get_job_run.return_value = _run(
        run_id="run-active",
        heartbeat_at=NOW - timedelta(seconds=599),
    )
    pool = MagicMock(enqueue=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW):
        await svc._trigger_job("job-1")

    job_store.get_job_run.assert_awaited_once_with("job-1", "run-active")
    job_store.recover_stale_run.assert_not_awaited()
    job_store.queue_run.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


@pytest.mark.parametrize("status", [JobStatus.cancelled, JobStatus.deleting])
async def test_trigger_job_removes_cancelled_or_deleting_schedule(status):
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(status=status, run_id="run-old")
    pool = MagicMock(enqueue=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)
    svc.register_job("job-1", "*/5 * * * *")

    await svc._trigger_job("job-1")

    assert svc.get_next_run_time("job-1") is None
    job_store.queue_run.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


async def test_trigger_job_recovers_and_requeues_stale_running_run():
    job_store = AsyncMock()
    stale_job = _job(status=JobStatus.running, run_id="run-stale")
    recovered_job = _job(status=JobStatus.failed, run_id="run-stale", updated_at=NOW)
    job_store.get_job.side_effect = [stale_job, recovered_job]
    job_store.get_job_run.return_value = _run(
        run_id="run-stale",
        heartbeat_at=NOW - timedelta(seconds=601),
    )
    job_store.recover_stale_run.return_value = True
    job_store.queue_run.return_value = True
    pool = MagicMock(enqueue=AsyncMock())
    result_store = AsyncMock()
    svc = _make_service(
        worker_pool=pool,
        job_store=job_store,
        result_store=result_store,
    )
    terminal_reconciliation = AsyncMock()

    with (
        patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW),
        patch("scrapeyard.scheduler.cron.generate_run_id", return_value="run-new"),
        patch(
            "scrapeyard.scheduler.cron.reconcile_terminal_webhook_intents",
            terminal_reconciliation,
        ),
    ):
        await svc._trigger_job("job-1")

    job_store.recover_stale_run.assert_awaited_once_with(
        "job-1",
        "run-stale",
        NOW - timedelta(seconds=600),
        NOW,
    )
    terminal_reconciliation.assert_awaited_once_with(
        job_store=job_store,
        result_store=result_store,
    )
    job_store.queue_run.assert_awaited_once_with(
        "job-1",
        expected_status=JobStatus.failed.value,
        expected_run_id="run-stale",
        new_run_id="run-new",
        queued_at=NOW,
        stale_before=None,
    )
    pool.enqueue.assert_awaited_once_with(
        "job-1",
        CONFIG_YAML,
        "normal",
        False,
        run_id="run-new",
        trigger="scheduled",
    )


async def test_trigger_job_does_not_queue_when_stale_recovery_loses_race():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.running,
        run_id="run-stale",
    )
    job_store.get_job_run.return_value = _run(
        run_id="run-stale",
        heartbeat_at=NOW - timedelta(seconds=601),
    )
    job_store.recover_stale_run.return_value = False
    pool = MagicMock(enqueue=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW):
        await svc._trigger_job("job-1")

    job_store.recover_stale_run.assert_awaited_once()
    job_store.queue_run.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


async def test_trigger_job_skips_if_run_already_queued():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.queued,
        run_id="run-queued",
        updated_at=NOW - timedelta(seconds=299),
    )
    pool = MagicMock(enqueue=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW):
        await svc._trigger_job("job-1")

    job_store.get_job_run.assert_not_awaited()
    job_store.queue_run.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


async def test_trigger_job_requeues_stale_queued_run_using_queued_timeout():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.queued,
        run_id="run-stale",
        updated_at=NOW - timedelta(seconds=121),
    )
    job_store.queue_run.return_value = True
    pool = MagicMock(enqueue=AsyncMock())
    svc = _make_service(
        worker_pool=pool,
        job_store=job_store,
        queued_claim_timeout_seconds=120,
        running_heartbeat_timeout_seconds=600,
    )

    with (
        patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW),
        patch("scrapeyard.scheduler.cron.generate_run_id", return_value="run-new"),
    ):
        await svc._trigger_job("job-1")

    job_store.get_job_run.assert_not_awaited()
    job_store.recover_stale_run.assert_not_awaited()
    job_store.queue_run.assert_awaited_once_with(
        "job-1",
        expected_status=JobStatus.queued.value,
        expected_run_id="run-stale",
        new_run_id="run-new",
        queued_at=NOW,
        stale_before=NOW - timedelta(seconds=120),
    )
    pool.enqueue.assert_awaited_once()


async def test_trigger_job_stops_when_queue_replacement_loses_race():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(status=JobStatus.failed, run_id="run-old")
    job_store.queue_run.return_value = False
    pool = MagicMock(enqueue=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with (
        patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW),
        patch("scrapeyard.scheduler.cron.generate_run_id", return_value="run-new"),
    ):
        await svc._trigger_job("job-1")

    job_store.queue_run.assert_awaited_once()
    pool.enqueue.assert_not_awaited()


async def test_trigger_job_removes_deleted_jobs():
    job_store = AsyncMock()
    job_store.get_job.side_effect = KeyError("not found")

    svc = _make_service(job_store=job_store)
    svc.register_job("job-1", "*/5 * * * *")
    assert svc._scheduler.get_job("job-1") is not None

    await svc._trigger_job("job-1")

    assert svc._scheduler.get_job("job-1") is None


async def test_start_registers_jobs_from_store():
    """start() loads scheduled jobs from the store and registers them."""
    job_store = AsyncMock()
    job_store.list_scheduled_jobs.return_value = [
        ("cron-a", "*/10 * * * *", True),
        ("cron-b", "0 3 * * *", False),
    ]
    svc = _make_service(job_store=job_store)
    await svc.start()
    try:
        job_a = svc._scheduler.get_job("cron-a")
        job_b = svc._scheduler.get_job("cron-b")
        assert job_a is not None
        assert job_b is not None
        assert job_b.next_run_time is None
    finally:
        svc.shutdown()


async def test_trigger_job_enqueues_queued_job():
    """_trigger_job reserves a run before enqueueing it into the worker pool."""
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(status=JobStatus.queued, run_id=None)
    job_store.queue_run.return_value = True
    pool = MagicMock(enqueue=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with (
        patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW),
        patch("scrapeyard.scheduler.cron.generate_run_id", return_value="run-new"),
    ):
        await svc._trigger_job("job-1")

    job_store.queue_run.assert_awaited_once_with(
        "job-1",
        expected_status=JobStatus.queued.value,
        expected_run_id=None,
        new_run_id="run-new",
        queued_at=NOW,
        stale_before=None,
    )
    job_store.update_job_status.assert_not_awaited()
    pool.enqueue.assert_awaited_once()


async def test_trigger_job_marks_owned_queued_run_failed_when_enqueue_fails():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(status=JobStatus.queued, run_id=None)
    job_store.queue_run.return_value = True
    job_store.fail_queued_run.return_value = True
    pool = MagicMock(enqueue=AsyncMock(side_effect=MemoryError("over limit")))
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with (
        patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW),
        patch("scrapeyard.scheduler.cron.generate_run_id", return_value="run-new"),
    ):
        await svc._trigger_job("job-1")

    pool.enqueue.assert_awaited_once()
    job_store.fail_queued_run.assert_awaited_once_with("job-1", "run-new", NOW)
    job_store.update_job_status.assert_not_awaited()


async def test_register_job_disabled_pauses():
    """register_job with enabled=False should pause the job."""
    svc = _make_service()
    svc._scheduler.start()
    try:
        svc.register_job("job-1", "*/5 * * * *", enabled=False)
        aps_job = svc._scheduler.get_job("job-1")
        assert aps_job is not None
        assert aps_job.next_run_time is None
    finally:
        svc.shutdown()


async def test_shutdown_calls_scheduler_shutdown():
    svc = _make_service()
    svc._scheduler.start()
    try:
        with patch.object(svc._scheduler, "shutdown") as mock_shutdown:
            svc.shutdown()
            mock_shutdown.assert_called_once_with(wait=False)
    finally:
        svc._scheduler.shutdown(wait=False)
