"""Tests for scrapeyard.scheduler.cron — SchedulerService."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from scrapeyard.common.settings import get_settings
from scrapeyard.models.job import Job, JobRun, JobStatus
from scrapeyard.queue.pool import QueueDeliveryState
from scrapeyard.scheduler.cron import SchedulerService, SchedulerUnavailableError
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore


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
        schedule_cron="*/5 * * * *",
        current_run_id=run_id,
        current_trigger="scheduled" if run_id is not None else None,
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


def test_yaml_parser_errors_have_a_bounded_config_failure_code():
    assert (
        SchedulerService._failure_code(yaml.YAMLError("sensitive parser detail"))
        == "persisted_config_invalid"
    )


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


def test_register_job_uses_explicit_timezone_and_bounded_misfire_policy():
    svc = _make_service(misfire_grace_seconds=45)
    svc.register_job(
        "job-1",
        "30 2 * * *",
        timezone_name="America/New_York",
    )
    aps_job = svc._scheduler.get_job("job-1")
    assert aps_job is not None
    assert str(aps_job.trigger.timezone) == "America/New_York"
    assert aps_job.coalesce is True
    assert aps_job.max_instances == 1
    assert aps_job.misfire_grace_time == 45


def test_cron_timezone_has_defined_spring_forward_and_fall_back_behavior():
    svc = _make_service()
    svc.register_job(
        "spring",
        "30 2 * * *",
        timezone_name="America/New_York",
    )
    spring = svc._scheduler.get_job("spring").trigger
    ny = ZoneInfo("America/New_York")
    utc = ZoneInfo("UTC")
    spring_fire = spring.get_next_fire_time(
        None,
        datetime(2026, 3, 7, 3, 0, tzinfo=ny),
    )
    # The nonexistent 02:30 wall time resolves to the next real instant,
    # 03:30 EDT / 07:30 UTC, exactly as APScheduler 3 defines it.
    assert spring_fire.astimezone(utc) == datetime(2026, 3, 8, 7, 30, tzinfo=utc)

    svc.register_job(
        "fall",
        "30 1 * * *",
        timezone_name="America/New_York",
    )
    fall = svc._scheduler.get_job("fall").trigger
    first = fall.get_next_fire_time(None, datetime(2026, 10, 31, 3, 0, tzinfo=ny))
    second = fall.get_next_fire_time(first, first)
    assert first.fold == 0
    assert second.fold == 1
    assert first.astimezone(utc) == datetime(2026, 11, 1, 5, 30, tzinfo=utc)
    assert second.astimezone(utc) == datetime(2026, 11, 1, 6, 30, tzinfo=utc)


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
    pool = MagicMock(enqueue=AsyncMock(), inspect_delivery=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW):
        await svc._run_scheduled_callback("job-1")

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
        new_trigger="scheduled",
        queued_at=NOW,
        stale_before=None,
        expected_config_yaml=CONFIG_YAML,
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
    pool = MagicMock(enqueue=AsyncMock(), inspect_delivery=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW):
        await svc._trigger_job("job-1")

    job_store.get_job_run.assert_not_awaited()
    job_store.queue_run.assert_not_awaited()
    pool.inspect_delivery.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


@pytest.mark.parametrize(
    "delivery_state",
    [
        QueueDeliveryState.queued,
        QueueDeliveryState.deferred,
        QueueDeliveryState.in_progress,
    ],
)
async def test_trigger_job_preserves_stale_queued_run_present_in_redis(
    delivery_state,
):
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.queued,
        run_id="run-stale",
        updated_at=NOW - timedelta(seconds=121),
    )
    pool = MagicMock(
        enqueue=AsyncMock(),
        inspect_delivery=AsyncMock(return_value=delivery_state),
    )
    svc = _make_service(
        worker_pool=pool,
        job_store=job_store,
        queued_claim_timeout_seconds=120,
        running_heartbeat_timeout_seconds=600,
    )

    with patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW):
        await svc._trigger_job("job-1")

    pool.inspect_delivery.assert_awaited_once_with("run-stale")
    job_store.reserve_queued_run_recovery.assert_not_awaited()
    job_store.queue_run.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


async def test_trigger_job_recovers_missing_delivery_with_original_run_id():
    queued_at = NOW - timedelta(seconds=121)
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.queued,
        run_id="run-stale",
        updated_at=queued_at,
    )
    job_store.reserve_queued_run_recovery.return_value = True
    pool = MagicMock(
        enqueue=AsyncMock(),
        inspect_delivery=AsyncMock(return_value=QueueDeliveryState.missing),
    )
    svc = _make_service(
        worker_pool=pool,
        job_store=job_store,
        queued_claim_timeout_seconds=120,
        running_heartbeat_timeout_seconds=600,
    )

    with patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW):
        await svc._trigger_job("job-1")

    job_store.reserve_queued_run_recovery.assert_awaited_once_with(
        "job-1",
        "run-stale",
        expected_queued_at=queued_at,
        stale_before=NOW - timedelta(seconds=120),
        reserved_at=NOW,
    )
    pool.enqueue.assert_awaited_once_with(
        "job-1",
        CONFIG_YAML,
        "normal",
        False,
        run_id="run-stale",
        trigger="scheduled",
    )
    job_store.queue_run.assert_not_awaited()


async def test_scheduled_trigger_fails_closed_and_records_redis_inspection_error():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.queued,
        run_id="run-stale",
        updated_at=NOW - timedelta(seconds=301),
    )
    pool = MagicMock(
        enqueue=AsyncMock(),
        inspect_delivery=AsyncMock(side_effect=ConnectionError("redis unavailable")),
    )
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with (
        patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW),
        patch("scrapeyard.scheduler.cron.generate_run_id", return_value="failed-fire"),
    ):
        assert await svc._run_scheduled_callback("job-1") is None

    assert svc.background_ok is False
    assert svc.background_detail == (
        "scheduled callback failures: count=1 "
        "job_id=job-1 code=queue_state_unavailable"
    )
    job_store.record_scheduled_trigger_failure.assert_awaited_once_with(
        "job-1",
        "failed-fire",
        failed_at=NOW,
        failure_code="queue_state_unavailable",
    )
    job_store.queue_run.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


async def test_manual_trigger_raises_unavailable_when_redis_inspection_fails():
    job_store = AsyncMock()
    job_store.get_job.return_value = _job(
        status=JobStatus.queued,
        run_id="run-stale",
        updated_at=NOW - timedelta(seconds=301),
    )
    pool = MagicMock(
        enqueue=AsyncMock(),
        inspect_delivery=AsyncMock(side_effect=ConnectionError("redis unavailable")),
    )
    svc = _make_service(worker_pool=pool, job_store=job_store)

    with (
        patch("scrapeyard.scheduler.cron.utc_now", return_value=NOW),
        pytest.raises(SchedulerUnavailableError, match="temporarily unavailable"),
    ):
        await svc.trigger_job_now("job-1")

    job_store.queue_run.assert_not_awaited()
    pool.enqueue.assert_not_awaited()


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
        ("cron-a", "*/10 * * * *", "UTC", True),
        ("cron-b", "0 3 * * *", "America/New_York", False),
    ]
    job_store.list_scheduled_trigger_failures.return_value = []
    svc = _make_service(job_store=job_store)
    await svc.start()
    try:
        job_a = svc._scheduler.get_job("cron-a")
        job_b = svc._scheduler.get_job("cron-b")
        assert job_a is not None
        assert job_b is not None
        assert job_b.next_run_time is None
        assert str(job_b.trigger.timezone) == "America/New_York"
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
        await svc._run_scheduled_callback("job-1")

    job_store.queue_run.assert_awaited_once_with(
        "job-1",
        expected_status=JobStatus.queued.value,
        expected_run_id=None,
        new_run_id="run-new",
        new_trigger="scheduled",
        queued_at=NOW,
        stale_before=None,
        expected_config_yaml=CONFIG_YAML,
    )
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
        await svc._run_scheduled_callback("job-1")

    pool.enqueue.assert_awaited_once()
    job_store.fail_queued_run.assert_awaited_once_with("job-1", "run-new", NOW)
    job_store.record_scheduled_trigger_failure.assert_awaited_once_with(
        "job-1",
        "run-new",
        failed_at=NOW,
        failure_code="enqueue_resource_exhausted",
    )


async def test_scheduled_config_failure_is_durable_and_not_masked_by_other_success(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
        '{"test":["SCRAPEYARD_SECRET_MISSING_TOKEN"]}',
    )
    monkeypatch.delenv("SCRAPEYARD_SECRET_MISSING_TOKEN", raising=False)
    get_settings.cache_clear()
    await init_db(str(tmp_path / "db"))
    store = SQLiteJobStore()
    broken_yaml = """project: test
name: broken-schedule
target:
  url: https://${SCRAPEYARD_SECRET_MISSING_TOKEN}
  selectors:
    title: h1
"""
    await store.save_job(
        Job(
            job_id="broken-job",
            project="test",
            name="broken-schedule",
            config_yaml=broken_yaml,
            schedule_cron="*/5 * * * *",
        )
    )
    pool = MagicMock(enqueue=AsyncMock(), inspect_delivery=AsyncMock())
    svc = _make_service(worker_pool=pool, job_store=store)
    await svc.start()
    try:
        with patch("scrapeyard.scheduler.cron.generate_run_id", return_value="broken-fire"):
            await svc._run_scheduled_callback("broken-job")

        broken = await store.get_job("broken-job")
        runs = await store.get_job_runs("broken-job")
        assert broken.status is JobStatus.failed
        assert broken.schedule_failure_code == "persisted_config_invalid"
        assert broken.schedule_consecutive_failures == 1
        assert runs[0].failure_code == "persisted_config_invalid"
        assert "SCRAPEYARD_SECRET_MISSING_TOKEN" not in (svc.background_detail or "")

        await store.save_job(
            Job(
                job_id="healthy-job",
                project="test",
                name="healthy-schedule",
                config_yaml=CONFIG_YAML.replace("scheduled-job", "healthy-schedule"),
                schedule_cron="*/10 * * * *",
            )
        )
        svc.register_job("healthy-job", "*/10 * * * *")
        with patch("scrapeyard.scheduler.cron.generate_run_id", return_value="healthy-fire"):
            await svc._run_scheduled_callback("healthy-job")

        assert pool.enqueue.await_count == 1
        assert svc.background_ok is False
        assert "job_id=broken-job" in (svc.background_detail or "")
    finally:
        svc.shutdown()

    restarted = _make_service(worker_pool=pool, job_store=store)
    await restarted.start()
    try:
        assert restarted.background_ok is False
        assert "job_id=broken-job" in (restarted.background_detail or "")
    finally:
        restarted.shutdown()


async def test_job_store_failure_before_queue_degrades_scheduled_callback_health():
    job_store = AsyncMock()
    job_store.get_job.side_effect = RuntimeError("secret-looking store detail")
    svc = _make_service(job_store=job_store)

    with patch("scrapeyard.scheduler.cron.generate_run_id", return_value="failed-fire"):
        await svc._run_scheduled_callback("job-1")

    assert svc.background_detail == (
        "scheduled callback failures: count=1 "
        "job_id=job-1 code=scheduled_callback_failed"
    )
    assert "secret-looking" not in (svc.background_detail or "")


async def test_decrypt_failure_persists_sanitized_schedule_health(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteJobStore()
    await store.save_job(
        Job(
            job_id="decrypt-job",
            project="test",
            name="decrypt-schedule",
            config_yaml=CONFIG_YAML.replace("scheduled-job", "decrypt-schedule"),
            schedule_cron="*/5 * * * *",
        )
    )
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE jobs SET config_yaml = 'syenc:v1:test-v1:invalid' "
            "WHERE job_id = 'decrypt-job'"
        )
        await db.commit()
    svc = _make_service(worker_pool=MagicMock(enqueue=AsyncMock()), job_store=store)
    await svc.start()
    try:
        with patch("scrapeyard.scheduler.cron.generate_run_id", return_value="decrypt-fire"):
            await svc._run_scheduled_callback("decrypt-job")
        async with get_db("jobs.db") as db:
            health = await (
                await db.execute(
                    "SELECT schedule_failure_code, schedule_consecutive_failures "
                    "FROM jobs WHERE job_id = 'decrypt-job'"
                )
            ).fetchone()
            run = await (
                await db.execute(
                    "SELECT failure_code FROM job_runs WHERE run_id = 'decrypt-fire'"
                )
            ).fetchone()
        assert tuple(health) == ("persisted_config_unavailable", 1)
        assert run["failure_code"] == "persisted_config_unavailable"
        assert "invalid" not in (svc.background_detail or "")
    finally:
        svc.shutdown()


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


async def test_paused_failure_is_hidden_and_resume_restores_degraded_health():
    svc = _make_service()
    svc._scheduler.start()
    try:
        svc.register_job(
            "job-1",
            "*/5 * * * *",
            enabled=False,
            failure_code="persisted_config_invalid",
        )
        assert svc.background_ok is True

        svc.register_job(
            "job-1",
            "*/5 * * * *",
            enabled=True,
            failure_code="persisted_config_invalid",
        )
        assert svc.background_ok is False
        assert "job_id=job-1" in (svc.background_detail or "")
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
