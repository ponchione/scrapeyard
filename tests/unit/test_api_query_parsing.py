import pytest
from fastapi import HTTPException

from scrapeyard.api.query_parsing import parse_error_filters
from scrapeyard.models.job import ErrorFilters, ErrorType


def test_parse_error_filters_builds_filters() -> None:
    parsed = parse_error_filters(
        project="acme",
        job_id="job-1",
        since="2026-03-01T12:00:00",
        error_type="timeout",
    )

    assert isinstance(parsed, ErrorFilters)
    assert parsed.project == "acme"
    assert parsed.job_id == "job-1"
    assert parsed.since is not None
    assert parsed.error_type == ErrorType.timeout


def test_parse_error_filters_normalizes_since_to_utc() -> None:
    parsed = parse_error_filters(
        project=None,
        job_id=None,
        since="2026-03-01T07:00:00-05:00",
        error_type=None,
    )

    assert parsed.since is not None
    assert parsed.since.isoformat() == "2026-03-01T12:00:00+00:00"


def test_parse_error_filters_accepts_api_rfc3339_utc_timestamp() -> None:
    parsed = parse_error_filters(
        project=None,
        job_id=None,
        since="2026-07-14T12:00:00Z",
        error_type=None,
    )

    assert parsed.since is not None
    assert parsed.since.isoformat() == "2026-07-14T12:00:00+00:00"


@pytest.mark.parametrize(
    "since",
    # The last two are dates whose offset puts the instant outside datetime's range.
    ["nope", "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"],
)
def test_parse_error_filters_returns_error_for_invalid_since(since: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_error_filters(project=None, job_id=None, since=since, error_type=None)

    assert exc_info.value.status_code == 400


def test_parse_error_filters_returns_error_for_invalid_error_type() -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_error_filters(project=None, job_id=None, since=None, error_type="bogus")

    assert exc_info.value.status_code == 400
