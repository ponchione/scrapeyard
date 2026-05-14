from __future__ import annotations

from datetime import datetime, timezone

from scrapeyard.api.serializers import (
    serialize_error_record,
    serialize_job_detail,
    serialize_job_summary,
    serialize_job_run,
)
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType, JobRun, JobStatus
from tests.unit.worker_helpers import make_job


def test_serialize_job_summary_includes_run_stats_and_schedule_fields():
    created_at = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
    updated_at = datetime(2026, 4, 9, 12, 5, tzinfo=timezone.utc)
    last_run_at = datetime(2026, 4, 9, 12, 10, tzinfo=timezone.utc)
    job = make_job(
        job_id="job-1",
        project="integ",
        name="job-name",
        status=JobStatus.partial,
        created_at=created_at,
        updated_at=updated_at,
        schedule_cron="*/5 * * * *",
        schedule_enabled=False,
    )

    payload = serialize_job_summary(job, run_count=7, last_run_at=last_run_at)

    assert payload == {
        "job_id": "job-1",
        "project": "integ",
        "name": "job-name",
        "status": "partial",
        "created_at": created_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "schedule_cron": "*/5 * * * *",
        "schedule_enabled": False,
        "run_count": 7,
        "last_run_at": last_run_at.isoformat(),
    }


def test_serialize_job_detail_embeds_serialized_runs():
    started_at = datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc)
    completed_at = datetime(2026, 4, 9, 12, 1, tzinfo=timezone.utc)
    next_run_at = datetime(2026, 4, 9, 13, 0, tzinfo=timezone.utc)
    job = make_job(job_id="job-1", config_yaml="project: integ", updated_at=completed_at)
    run = JobRun(
        run_id="run-1",
        job_id=job.job_id,
        status=JobStatus.complete,
        trigger="adhoc",
        config_hash="abc123",
        started_at=started_at,
        completed_at=completed_at,
        record_count=4,
        error_count=1,
    )

    payload = serialize_job_detail(
        job,
        runs=[run],
        run_count=1,
        last_run_at=completed_at,
        next_run_at=next_run_at,
    )

    assert payload["job_id"] == "job-1"
    assert payload["runs"] == [serialize_job_run(run)]
    assert payload["next_run_at"] == next_run_at.isoformat()
    assert payload["last_run_at"] == completed_at.isoformat()


def test_serialize_job_detail_redacts_config_secrets():
    job = make_job(
        job_id="job-1",
        config_yaml="""
project: integ
name: secret-job
proxy:
  url: http://user:pass@gate.example.com:7777
webhook:
  url: https://example.com/hook
  headers:
    Authorization: Bearer webhook-secret
target:
  url: https://example.com
  browser:
    extra_headers:
      X-API-Key: browser-secret
  selectors:
    title: h1
""",
    )

    payload = serialize_job_detail(
        job,
        runs=[],
        run_count=0,
        last_run_at=None,
        next_run_at=None,
    )

    assert "webhook-secret" not in payload["config_yaml"]
    assert "browser-secret" not in payload["config_yaml"]
    assert "user:pass" not in payload["config_yaml"]
    assert payload["config_yaml"].count("<redacted>") == 2


def test_serialize_error_record_formats_enum_and_datetime_fields():
    error = ErrorRecord(
        job_id="job-1",
        run_id="run-1",
        project="integ",
        target_url="https://example.com",
        attempt=2,
        timestamp=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        error_type=ErrorType.http_error,
        http_status=500,
        fetcher_used="basic",
        error_message="boom",
        selectors_matched={"title": 0},
        action_taken=ActionTaken.retry,
    )

    payload = serialize_error_record(error)

    assert payload["timestamp"] == error.timestamp.isoformat()
    assert payload["error_type"] == "http_error"
    assert payload["action_taken"] == "retry"
    assert payload["selectors_matched"] == {"title": 0}
