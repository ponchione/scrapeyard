from scrapeyard.engine.scrape_models import TargetResult, TargetStatus
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.worker import _format_output


def test_target_result_coerces_string_status_to_enum():
    result = TargetResult(url="https://example.com", status="success")

    assert result.status is TargetStatus.success
    assert result.status == "success"


def test_target_result_rejects_unknown_status_string():
    try:
        TargetResult(url="https://example.com", status="done")
    except ValueError as exc:
        assert "done" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unknown target status")


def test_format_output_serializes_target_status_enum_values():
    result = TargetResult(
        url="https://example.com",
        status=TargetStatus.success,
        data=[{"title": "Example"}],
    )

    class _Cfg:
        project = "test"
        name = "job"

        class output:
            group_by = "target"

    payload = _format_output(
        _Cfg(),
        [result],
        [{"title": "Example"}],
        "job-1",
        final_status=JobStatus.complete,
        all_errors=[],
    )

    assert payload["targets"][0]["status"] == "success"
    assert payload["results"]["example.com"]["status"] == "success"
