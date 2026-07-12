from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import init_db
from scrapeyard.storage.job_store import DuplicateJobError, SQLiteJobStore
from scrapeyard.storage.types import ScheduledJobMutationAction


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
async def store(tmp_path):
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


def _job(**overrides) -> Job:
    values = {
        "job_id": "scheduled-1",
        "project": "schedule",
        "name": "daily",
        "config_yaml": "project: schedule\nname: daily",
        "schedule_cron": "0 9 * * *",
        "schedule_timezone": "America/New_York",
        "schedule_enabled": True,
    }
    values.update(overrides)
    return Job(**values)


async def test_update_replaces_future_config_and_timezone(store):
    await store.save_job(_job())

    outcome = await store.update_scheduled_job(
        "scheduled-1",
        project="schedule",
        name="weekday",
        config_yaml="project: schedule\nname: weekday",
        schedule_cron="30 8 * * 1-5",
        schedule_timezone="Europe/London",
        schedule_enabled=False,
        updated_at=NOW,
    )

    assert outcome.action is ScheduledJobMutationAction.updated
    assert outcome.previous is not None
    assert outcome.previous.name == "daily"
    assert outcome.current is not None
    assert outcome.current.name == "weekday"
    assert outcome.current.schedule_timezone == "Europe/London"
    assert outcome.current.schedule_enabled is False


async def test_update_rejects_accepted_or_running_delivery(store):
    for status in (JobStatus.queued, JobStatus.running):
        await store.save_job(
            _job(
                job_id=f"job-{status.value}",
                name=status.value,
                status=status,
                current_run_id=f"run-{status.value}",
            )
        )
        outcome = await store.update_scheduled_job(
            f"job-{status.value}",
            project="schedule",
            name=f"changed-{status.value}",
            config_yaml="changed",
            schedule_cron="0 10 * * *",
            schedule_timezone="UTC",
            schedule_enabled=True,
            updated_at=NOW,
        )
        assert outcome.action is ScheduledJobMutationAction.active_conflict


async def test_update_preserves_unique_project_name_contract(store):
    await store.save_job(_job())
    await store.save_job(_job(job_id="scheduled-2", name="reserved"))

    with pytest.raises(DuplicateJobError):
        await store.update_scheduled_job(
            "scheduled-1",
            project="schedule",
            name="reserved",
            config_yaml="changed",
            schedule_cron="0 10 * * *",
            schedule_timezone="UTC",
            schedule_enabled=True,
            updated_at=NOW,
        )


async def test_pause_resume_and_exact_compensation(store):
    await store.save_job(_job())
    paused = await store.set_schedule_enabled(
        "scheduled-1",
        enabled=False,
        updated_at=NOW,
    )
    assert paused.action is ScheduledJobMutationAction.updated
    assert paused.current is not None
    assert paused.current.schedule_enabled is False

    assert paused.previous is not None
    assert await store.restore_scheduled_job(paused.current, paused.previous)
    restored = await store.get_job("scheduled-1")
    assert restored.schedule_enabled is True


async def test_config_update_cannot_race_future_run_reservation(store):
    original = _job()
    await store.save_job(original)
    updated = await store.update_scheduled_job(
        original.job_id,
        project=original.project,
        name=original.name,
        config_yaml="new-config",
        schedule_cron=original.schedule_cron or "",
        schedule_timezone=original.schedule_timezone,
        schedule_enabled=True,
        updated_at=NOW,
    )
    assert updated.action is ScheduledJobMutationAction.updated

    queued = await store.queue_run(
        original.job_id,
        expected_status=JobStatus.queued.value,
        expected_run_id=None,
        new_run_id="run-old-config",
        new_trigger="scheduled",
        queued_at=NOW,
        expected_config_yaml=original.config_yaml,
    )
    assert queued is False
