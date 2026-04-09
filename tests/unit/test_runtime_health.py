from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.models.job import JobStatus
from scrapeyard.runtime.health import build_project_summary, load_project_summary


def test_build_project_summary_classifies_project_statuses():
    rows = [
        ("healthy-project", JobStatus.complete.value, 2),
        ("degraded-project", JobStatus.running.value, 1),
        ("degraded-project", JobStatus.partial.value, 1),
        ("failing-project", JobStatus.failed.value, 1),
    ]

    summary = build_project_summary(rows)

    assert summary["healthy-project"]["status"] == "healthy"
    assert summary["degraded-project"]["status"] == "degraded"
    assert summary["failing-project"]["status"] == "failing"
    assert summary["degraded-project"]["job_count"] == 2


@pytest.mark.asyncio
async def test_load_project_summary_returns_empty_when_store_unavailable():
    get_job_store = MagicMock(side_effect=RuntimeError("not ready"))

    summary = await load_project_summary(get_job_store)

    assert summary == {}


@pytest.mark.asyncio
async def test_load_project_summary_uses_store_summary_by_project():
    fake_store = MagicMock(summary_by_project=AsyncMock(return_value=[("proj", JobStatus.complete.value, 1)]))

    summary = await load_project_summary(lambda: fake_store)

    assert summary == {
        "proj": {
            "job_count": 1,
            "status": "healthy",
            "status_counts": {
                "queued": 0,
                "running": 0,
                "complete": 1,
                "partial": 0,
                "failed": 0,
            },
        }
    }
