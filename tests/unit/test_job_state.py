from __future__ import annotations

from datetime import datetime, timezone

from scrapeyard.models.job import JobStatus
from scrapeyard.queue.job_state import build_completed_job, build_failed_job, build_running_job
from tests.unit.worker_helpers import make_job


def test_build_running_job_updates_only_running_fields():
    started_at = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
    job = make_job(status=JobStatus.queued, current_run_id="run-1")

    updated = build_running_job(job, started_at=started_at)

    assert updated.status == JobStatus.running
    assert updated.updated_at == started_at
    assert updated.current_run_id == "run-1"
    assert job.status == JobStatus.queued


def test_build_completed_job_sets_terminal_status_and_run_id():
    completed_at = datetime(2026, 4, 9, 12, 5, tzinfo=timezone.utc)
    job = make_job(status=JobStatus.running, current_run_id="run-1")

    updated = build_completed_job(
        job,
        final_status=JobStatus.partial,
        completed_at=completed_at,
        run_id="run-2",
    )

    assert updated.status == JobStatus.partial
    assert updated.updated_at == completed_at
    assert updated.current_run_id == "run-2"


def test_build_failed_job_sets_failed_status_and_timestamp():
    failed_at = datetime(2026, 4, 9, 12, 10, tzinfo=timezone.utc)
    job = make_job(status=JobStatus.running)

    updated = build_failed_job(job, failed_at=failed_at)

    assert updated.status == JobStatus.failed
    assert updated.updated_at == failed_at
