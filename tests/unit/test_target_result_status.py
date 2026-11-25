import pytest

from scrapeyard.engine.scrape_models import TargetResult, TargetStatus
from tests.unit.worker_helpers import make_config_mock, make_target, run_scrape_task


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


@pytest.mark.asyncio
async def test_format_output_serializes_target_status_enum_values():
    result = TargetResult(
        url="https://example.com",
        status=TargetStatus.success,
        data=[{"title": "Example"}],
    )
    cfg = make_config_mock(
        targets=[make_target("https://example.com")],
        group_by="target",
    )

    outcome = await run_scrape_task(cfg, [result], job_id="job-1")
    payload = outcome.saved_artifact

    assert payload["targets"][0]["status"] == "success"
    assert payload["results"]["example.com"]["status"] == "success"
