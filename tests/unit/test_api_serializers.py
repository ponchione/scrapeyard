from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone

from scrapeyard.api import serializers
from scrapeyard.api.serializers import (
    redact_job_config,
    serialize_error_record,
    serialize_job_detail,
    serialize_job_run,
    serialize_job_summary,
)
from scrapeyard.engine.url_guard import redact_sensitive_config_text
from scrapeyard.models.job import (
    ActionTaken,
    BudgetErrorDetails,
    ErrorRecord,
    ErrorType,
    JobRun,
    JobStatus,
)
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
        "schedule_timezone": "UTC",
        "schedule_enabled": False,
        "schedule_failure_at": None,
        "schedule_failure_code": None,
        "schedule_consecutive_failures": 0,
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
        heartbeat_at=started_at,
        completed_at=completed_at,
        record_count=4,
        error_count=1,
    )

    payload = serialize_job_detail(
        job,
        redacted_config_yaml="project: integ\n",
        runs=[run],
        run_count=1,
        last_run_at=completed_at,
        next_run_at=next_run_at,
    )

    assert payload["job_id"] == "job-1"
    assert payload["config_yaml"] == "project: integ\n"
    assert payload["runs"] == [serialize_job_run(run)]
    assert payload["runs"][0]["heartbeat_at"] == started_at.isoformat()
    assert payload["next_run_at"] == next_run_at.isoformat()
    assert payload["last_run_at"] == completed_at.isoformat()


async def test_redact_job_config_redacts_config_secrets(monkeypatch):
    monkeypatch.setattr(serializers, "_redacted_configs", OrderedDict())
    config_yaml = """
project: integ
name: secret-job
proxy:
  url: http://user:pass@gate.example.com:7777
webhook:
  url: https://example.com/hook
  headers:
    Authorization: Bearer webhook-secret
target:
  url: https://example.com?api_key=target-secret&page=2
  browser:
    extra_headers:
      X-API-Key: browser-secret
  selectors:
    title: h1
"""

    redacted = await redact_job_config(config_yaml)

    assert "webhook-secret" not in redacted
    assert "browser-secret" not in redacted
    assert "target-secret" not in redacted
    assert "user:pass" not in redacted
    assert redacted.count("<redacted>") == 4
    assert redacted == redact_sensitive_config_text(config_yaml)


async def test_redact_job_config_caches_small_configs_by_content(monkeypatch):
    monkeypatch.setattr(serializers, "_redacted_configs", OrderedDict())
    calls: list[str] = []

    def _redact(text: str) -> str:
        calls.append(text)
        return redact_sensitive_config_text(text)

    monkeypatch.setattr(serializers, "redact_sensitive_config_text", _redact)
    small = "project: a\nwebhook:\n  headers:\n    Authorization: secret\n"
    large = small + "".join(f"key{index}: value\n" for index in range(6000))
    assert len(redact_sensitive_config_text(large)) > serializers._REDACTED_CONFIG_CACHE_MAX_CHARS

    assert await redact_job_config(small) == await redact_job_config(small)
    assert await redact_job_config(small + "name: b\n") != await redact_job_config(small)
    await redact_job_config(large)
    await redact_job_config(large)

    assert calls == [small, small + "name: b\n", large, large]


async def test_redact_job_config_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(serializers, "_redacted_configs", OrderedDict())
    for index in range(serializers._REDACTED_CONFIG_CACHE_ENTRIES + 5):
        await redact_job_config(f"project: p{index}\n")

    assert len(serializers._redacted_configs) == serializers._REDACTED_CONFIG_CACHE_ENTRIES


def test_serialize_error_record_formats_enum_and_datetime_fields():
    error = ErrorRecord(
        job_id="job-1",
        run_id="run-1",
        project="integ",
        target_url="https://user:pass@example.com",
        attempt=2,
        timestamp=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        error_type=ErrorType.http_error,
        http_status=500,
        fetcher_used="basic",
        error_message="boom at https://user:pass@example.com/private",
        selectors_matched={"title": 0},
        action_taken=ActionTaken.retry,
    )

    payload = serialize_error_record(error)

    assert payload["target_url"] == "https://example.com"
    assert payload["timestamp"] == error.timestamp.isoformat()
    assert payload["error_type"] == "http_error"
    assert payload["error_message"] == "boom at https://example.com/private"
    assert payload["action_taken"] == "retry"
    assert payload["selectors_matched"] == {"title": 0}


def test_serialize_error_record_redacts_sensitive_query_values():
    error = ErrorRecord(
        job_id="job-1",
        run_id="run-1",
        project="integ",
        target_url="https://example.com/products?api_key=secret&page=2",
        attempt=1,
        timestamp=datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc),
        error_type=ErrorType.network_error,
        fetcher_used="basic",
        error_message="failed at https://example.com/private?access_token=secret",
        action_taken=ActionTaken.fail,
    )

    payload = serialize_error_record(error)

    assert payload["target_url"] == (
        "https://example.com/products?api_key=<redacted>&page=<redacted>"
    )
    assert payload["error_message"] == "failed at https://example.com/private?access_token=<redacted>"


def test_serialize_error_record_includes_structured_budget_details():
    error = ErrorRecord(
        job_id="job-1",
        run_id="run-1",
        project="integ",
        target_url="",
        attempt=0,
        error_type=ErrorType.budget_exceeded,
        fetcher_used="service_budget",
        error_message="record budget exceeded",
        budget=BudgetErrorDetails(
            limit_name="extracted_records",
            configured_limit=10,
            observed_amount=11,
        ),
        action_taken=ActionTaken.fail,
    )

    payload = serialize_error_record(error)

    assert payload["error_type"] == "budget_exceeded"
    assert payload["budget"] == {
        "limit_name": "extracted_records",
        "configured_limit": 10,
        "observed_amount": 11,
    }
