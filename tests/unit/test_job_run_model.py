"""Tests for JobRun model and SchedulerService.get_next_run_time."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from scrapeyard.models.job import JobRun, JobStatus
from scrapeyard.scheduler.cron import SchedulerService


def _make_job_run(**overrides) -> JobRun:
    defaults = {
        "run_id": "20260321-100000-abcd1234",
        "job_id": "j-1",
        "trigger": "adhoc",
        "config_hash": "abc123def456",
    }
    defaults.update(overrides)
    return JobRun(**defaults)


# -- JobRun default field values --------------------------------------------------


def test_status_defaults_to_running():
    run = _make_job_run()
    assert run.status == JobStatus.running


def test_error_count_defaults_to_zero():
    run = _make_job_run()
    assert run.error_count == 0


def test_completed_at_defaults_to_none():
    run = _make_job_run()
    assert run.completed_at is None


def test_record_count_defaults_to_none():
    run = _make_job_run()
    assert run.record_count is None


# -- Required fields ---------------------------------------------------------------


def test_missing_run_id_raises():
    with pytest.raises(ValidationError):
        JobRun(job_id="j-1", trigger="adhoc", config_hash="abc")


def test_missing_job_id_raises():
    with pytest.raises(ValidationError):
        JobRun(run_id="r-1", trigger="adhoc", config_hash="abc")


def test_missing_trigger_raises():
    with pytest.raises(ValidationError):
        JobRun(run_id="r-1", job_id="j-1", config_hash="abc")


def test_missing_config_hash_raises():
    with pytest.raises(ValidationError):
        JobRun(run_id="r-1", job_id="j-1", trigger="adhoc")


# -- Trigger values ----------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["adhoc", "scheduled"])
def test_valid_trigger_values(trigger):
    run = _make_job_run(trigger=trigger)
    assert run.trigger == trigger


# -- started_at auto-populates ----------------------------------------------------


def test_started_at_auto_populates():
    before = datetime.now(timezone.utc)
    run = _make_job_run()
    after = datetime.now(timezone.utc)
    assert before <= run.started_at <= after


def test_started_at_is_utc():
    run = _make_job_run()
    assert run.started_at.tzinfo is not None


# -- model_copy with update --------------------------------------------------------


def test_model_copy_update_status():
    run = _make_job_run()
    updated = run.model_copy(update={"status": JobStatus.complete})
    assert updated.status == JobStatus.complete
    assert run.status == JobStatus.running  # original unchanged


def test_model_copy_update_completed_at():
    run = _make_job_run()
    now = datetime.now(timezone.utc)
    updated = run.model_copy(update={
        "completed_at": now,
        "record_count": 42,
        "error_count": 3,
    })
    assert updated.completed_at == now
    assert updated.record_count == 42
    assert updated.error_count == 3
    assert updated.run_id == run.run_id  # unchanged fields preserved


# -- SchedulerService.get_next_run_time -------------------------------------------


def test_get_next_run_time_returns_none_when_job_missing():
    svc = SchedulerService.__new__(SchedulerService)
    svc._scheduler = MagicMock()
    svc._scheduler.get_job.return_value = None

    assert svc.get_next_run_time("no-such-job") is None
    svc._scheduler.get_job.assert_called_once_with("no-such-job")


def test_get_next_run_time_returns_next_run_time():
    expected = datetime(2026, 3, 22, 0, 0, 0, tzinfo=timezone.utc)

    aps_job = MagicMock()
    aps_job.next_run_time = expected

    svc = SchedulerService.__new__(SchedulerService)
    svc._scheduler = MagicMock()
    svc._scheduler.get_job.return_value = aps_job

    result = svc.get_next_run_time("j-1")
    assert result == expected
    svc._scheduler.get_job.assert_called_once_with("j-1")
