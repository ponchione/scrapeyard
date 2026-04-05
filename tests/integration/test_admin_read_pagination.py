"""Integration tests for paginated admin read endpoints."""

from __future__ import annotations

from datetime import datetime

import pytest

from scrapeyard.api.dependencies import get_error_store, get_job_store
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType, Job


def _make_job(**overrides) -> Job:
    defaults = {
        "job_id": "job-1",
        "project": "integ",
        "name": "job-name",
        "config_yaml": "target: https://example.com",
    }
    defaults.update(overrides)
    return Job(**defaults)


def _make_error(**overrides) -> ErrorRecord:
    defaults = {
        "job_id": "job-1",
        "run_id": "run-1",
        "project": "integ",
        "target_url": "https://example.com",
        "attempt": 1,
        "timestamp": datetime(2026, 3, 1, 12, 0, 0),
        "error_type": ErrorType.http_error,
        "http_status": 503,
        "fetcher_used": "httpx",
        "error_message": "HTTP 503",
        "selectors_matched": {"h1": 1},
        "action_taken": ActionTaken.retry,
    }
    defaults.update(overrides)
    return ErrorRecord(**defaults)


@pytest.mark.asyncio
async def test_jobs_list_returns_array_with_pagination_headers(client):
    store = get_job_store()
    await store.save_job(
        _make_job(
            job_id="job-1",
            name="oldest",
            created_at=datetime(2026, 3, 1, 8, 0, 0),
        )
    )
    await store.save_job(
        _make_job(
            job_id="job-2",
            name="middle",
            created_at=datetime(2026, 3, 1, 9, 0, 0),
        )
    )
    await store.save_job(
        _make_job(
            job_id="job-3",
            name="newest",
            created_at=datetime(2026, 3, 1, 10, 0, 0),
        )
    )

    response = await client.get("/jobs?project=integ&limit=2")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert [item["job_id"] for item in body] == ["job-3", "job-2"]
    assert response.headers["X-Scrapeyard-Limit"] == "2"
    assert response.headers["X-Scrapeyard-Offset"] == "0"
    assert response.headers["X-Scrapeyard-Item-Count"] == "2"
    assert response.headers["X-Scrapeyard-Has-More"] == "true"
    assert response.headers["X-Scrapeyard-Next-Offset"] == "2"


@pytest.mark.asyncio
async def test_errors_list_returns_newest_first_with_pagination_headers(client):
    store = get_error_store()
    await store.log_errors(
        [
            _make_error(job_id="job-1", run_id="run-1", timestamp=datetime(2026, 3, 1, 8, 0, 0)),
            _make_error(job_id="job-2", run_id="run-2", timestamp=datetime(2026, 3, 1, 9, 0, 0)),
            _make_error(job_id="job-3", run_id="run-3", timestamp=datetime(2026, 3, 1, 10, 0, 0)),
        ]
    )

    response = await client.get("/errors?project=integ&limit=2")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert [item["job_id"] for item in body] == ["job-3", "job-2"]
    assert response.headers["X-Scrapeyard-Limit"] == "2"
    assert response.headers["X-Scrapeyard-Offset"] == "0"
    assert response.headers["X-Scrapeyard-Item-Count"] == "2"
    assert response.headers["X-Scrapeyard-Has-More"] == "true"
    assert response.headers["X-Scrapeyard-Next-Offset"] == "2"

    page_two = await client.get("/errors?project=integ&limit=2&offset=2")

    assert page_two.status_code == 200
    assert [item["job_id"] for item in page_two.json()] == ["job-1"]
    assert page_two.headers["X-Scrapeyard-Has-More"] == "false"
