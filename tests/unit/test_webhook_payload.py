"""Tests for webhook payload construction and should_fire filtering."""

from __future__ import annotations

from scrapeyard.config.schema import WebhookConfig, WebhookStatus
from scrapeyard.models.job import JobStatus
from scrapeyard.webhook.payload import build_webhook_payload, should_fire


class TestShouldFire:
    def test_complete_in_default_on_list(self):
        config = WebhookConfig(url="https://example.com/hook")
        assert should_fire(config, JobStatus.complete) is True

    def test_partial_in_default_on_list(self):
        config = WebhookConfig(url="https://example.com/hook")
        assert should_fire(config, JobStatus.partial) is True

    def test_failed_not_in_default_on_list(self):
        config = WebhookConfig(url="https://example.com/hook")
        assert should_fire(config, JobStatus.failed) is False

    def test_custom_on_list(self):
        config = WebhookConfig(
            url="https://example.com/hook",
            on=[WebhookStatus.failed],
        )
        assert should_fire(config, JobStatus.failed) is True
        assert should_fire(config, JobStatus.complete) is False

    def test_non_terminal_status_never_fires(self):
        config = WebhookConfig(
            url="https://example.com/hook",
            on=[WebhookStatus.complete, WebhookStatus.partial, WebhookStatus.failed],
        )
        assert should_fire(config, JobStatus.queued) is False
        assert should_fire(config, JobStatus.running) is False


class TestBuildWebhookPayload:
    def test_all_fields_present(self):
        payload = build_webhook_payload(
            job_id="job-123",
            project="acme",
            name="scrape-prices",
            status=JobStatus.complete,
            run_id="20260316-120000-abcd1234",
            result_path="/results/acme/scrape-prices/20260316-120000-abcd1234",
            result_count=42,
            error_count=0,
            started_at="2026-03-16T12:00:00+00:00",
            completed_at="2026-03-16T12:01:00+00:00",
        )

        assert payload["event"] == "job.complete"
        assert payload["job_id"] == "job-123"
        assert payload["project"] == "acme"
        assert payload["name"] == "scrape-prices"
        assert payload["status"] == "complete"
        assert payload["run_id"] == "20260316-120000-abcd1234"
        assert payload["result_path"] == "/results/acme/scrape-prices/20260316-120000-abcd1234"
        assert payload["results_url"] == "/results/job-123?run_id=20260316-120000-abcd1234"
        assert payload["result_count"] == 42
        assert payload["error_count"] == 0
        assert payload["started_at"] == "2026-03-16T12:00:00+00:00"
        assert payload["completed_at"] == "2026-03-16T12:01:00+00:00"

    def test_event_format_for_each_status(self):
        for status in (JobStatus.complete, JobStatus.partial, JobStatus.failed):
            payload = build_webhook_payload(
                job_id="j", project="p", name="n", status=status,
                run_id="r", result_path="/r", result_count=0,
                error_count=0, started_at="t", completed_at="t",
            )
            assert payload["event"] == f"job.{status.value}"

    def test_none_run_id_gives_none_results_url(self):
        payload = build_webhook_payload(
            job_id="job-123",
            project="acme",
            name="scrape-prices",
            status=JobStatus.failed,
            run_id=None,
            result_path=None,
            result_count=0,
            error_count=3,
            started_at="2026-03-16T12:00:00+00:00",
            completed_at="2026-03-16T12:01:00+00:00",
        )

        assert payload["run_id"] is None
        assert payload["result_path"] is None
        assert payload["results_url"] is None
        assert payload["result_count"] == 0
        assert payload["event"] == "job.failed"
