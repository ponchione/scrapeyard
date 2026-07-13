"""Tests for worker-side error record construction."""

from scrapeyard.engine.scraper import TargetResult, TargetStatus
from scrapeyard.models.job import ActionTaken
from scrapeyard.queue.error_records import build_target_result_error_records


def _build_records(*, combine_errors: bool):
    return build_target_result_error_records(
        job_id="job-1",
        run_id="run-1",
        project="test",
        target_url="https://example.com",
        attempt=2,
        fetcher_used="basic",
        action=ActionTaken.fail,
        result=TargetResult(
            url="https://example.com",
            status=TargetStatus.failed,
            errors=["timeout", "proxy refused"],
        ),
        combine_errors=combine_errors,
    )


def test_combined_target_errors_produce_one_record() -> None:
    records = _build_records(combine_errors=True)

    assert [record.error_message for record in records] == ["timeout; proxy refused"]


def test_uncombined_target_errors_preserve_one_record_per_message() -> None:
    records = _build_records(combine_errors=False)

    assert [record.error_message for record in records] == ["timeout", "proxy refused"]
