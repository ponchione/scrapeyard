"""Tests that scrape_task uses the injected rate_limiter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.worker import scrape_task


def _make_job(**overrides):
    defaults = {
        "job_id": "j-rate",
        "project": "test",
        "name": "rate-test",
        "config_yaml": "",
        "status": JobStatus.queued,
    }
    defaults.update(overrides)
    return Job(**defaults)


@pytest.mark.asyncio
async def test_scrape_task_calls_rate_limiter_acquire():
    """The rate_limiter.acquire() should be called for each target's domain."""
    job = _make_job()
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.update_job.side_effect = lambda j: None

    rate_limiter = AsyncMock()
    rate_limiter.acquire = AsyncMock()

    success = TargetResult(
        url="https://example.com", status="success", data=[{"title": "T"}],
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=success)), \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.project = "test"
        cfg.name = "rate-test"
        cfg.resolved_targets.return_value = [
            MagicMock(
                url="https://example.com",
                fetcher=MagicMock(value="basic"),
                proxy=None,
            ),
        ]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 3
        cfg.execution.fail_strategy = MagicMock(value="partial")
        cfg.adaptive = None
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock()
        cfg.webhook = None
        cfg.output.group_by = MagicMock(value="target")
        cfg.proxy = None
        mock_load.return_value = cfg
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adapt",
            workers_running_lease_seconds=300,
            proxy_url="",
        )

        await scrape_task(
            job.job_id,
            "project: test\nname: rate-test\ntarget:\n  url: https://example.com\n  selectors:\n    t: h1",
            job_store=job_store,
            result_store=AsyncMock(),
            error_store=AsyncMock(),
            circuit_breaker=MagicMock(),
            rate_limiter=rate_limiter,
        )

    rate_limiter.acquire.assert_called_once_with("example.com", 3)
