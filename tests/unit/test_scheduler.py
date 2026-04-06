"""Tests for scrapeyard.scheduler.cron — SchedulerService."""

from unittest.mock import AsyncMock, MagicMock, patch


from scrapeyard.models.job import JobStatus
from scrapeyard.scheduler.cron import SchedulerService


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
    # Should not raise
    svc.remove_job("no-such-job")


def test_remove_job_removes_existing():
    svc = _make_service()
    svc.register_job("job-1", "*/5 * * * *")
    svc.remove_job("job-1")
    assert svc._scheduler.get_job("job-1") is None


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


async def test_trigger_job_skips_if_already_running():
    job_store = AsyncMock()
    mock_job = MagicMock()
    mock_job.status = JobStatus.running
    job_store.get_job.return_value = mock_job

    pool = MagicMock()
    pool.enqueue = AsyncMock()

    svc = _make_service(worker_pool=pool, job_store=job_store)
    await svc._trigger_job("job-1")

    pool.enqueue.assert_not_called()


async def test_trigger_job_removes_deleted_jobs():
    job_store = AsyncMock()
    job_store.get_job.side_effect = KeyError("not found")

    svc = _make_service(job_store=job_store)
    svc.register_job("job-1", "*/5 * * * *")
    assert svc._scheduler.get_job("job-1") is not None

    await svc._trigger_job("job-1")

    # Job should have been removed from scheduler
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
        # cron-b was registered as disabled (paused) — next_run_time is None
        assert job_b.next_run_time is None
    finally:
        svc.shutdown()


async def test_trigger_job_enqueues_queued_job():
    """_trigger_job enqueues a non-running job into the worker pool."""
    job_store = AsyncMock()
    mock_job = MagicMock()
    mock_job.status = JobStatus.queued
    mock_job.job_id = "job-1"
    mock_job.config_yaml = "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1"
    mock_job.model_copy.return_value = mock_job
    job_store.get_job.return_value = mock_job

    pool = MagicMock()
    pool.enqueue = AsyncMock()

    svc = _make_service(worker_pool=pool, job_store=job_store)
    await svc._trigger_job("job-1")

    pool.enqueue.assert_called_once()
    job_store.update_job_status.assert_called_once()


async def test_register_job_disabled_pauses():
    """register_job with enabled=False should pause the job."""
    svc = _make_service()
    svc._scheduler.start()
    try:
        svc.register_job("job-1", "*/5 * * * *", enabled=False)
        aps_job = svc._scheduler.get_job("job-1")
        assert aps_job is not None
        assert aps_job.next_run_time is None  # paused
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
