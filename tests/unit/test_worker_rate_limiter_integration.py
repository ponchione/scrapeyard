"""Tests that scrape_task uses the injected rate_limiter."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.engine.scraper import TargetResult
from scrapeyard.config.schema import FetcherType
from scrapeyard.queue.worker import scrape_task
from tests.unit.worker_helpers import make_job, make_settings_mock


@pytest.mark.asyncio
async def test_scrape_task_passes_rate_limiter_to_request_boundary():
    job = make_job(job_id="j-rate", name="rate-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.claim_run.return_value = True
    job_store.queue_run.return_value = True

    rate_limiter = AsyncMock()
    rate_limiter.acquire = AsyncMock()

    success = TargetResult(
        url="https://example.com", status="success", data=[{"title": "T"}],
    )

    scrape = AsyncMock(return_value=success)
    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target", new=scrape), \
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
        mock_settings.return_value = make_settings_mock(adaptive_dir="/tmp/adapt")

        await scrape_task(
            job.job_id,
            "project: test\nname: rate-test\ntarget:\n  url: https://example.com\n  selectors:\n    t: h1",
            job_store=job_store,
            result_store=AsyncMock(),
            error_store=AsyncMock(),
            circuit_breaker=MagicMock(),
            rate_limiter=rate_limiter,
        )

    rate_limiter.acquire.assert_not_awaited()
    assert scrape.await_args.kwargs["rate_limiter"] is rate_limiter
    assert scrape.await_args.kwargs["domain_rate_limit"] == 3


@pytest.mark.asyncio
async def test_browser_permit_is_acquired_before_request_rate_limit():
    job = make_job(job_id="j-browser-rate", name="browser-rate-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.claim_run.return_value = True
    rate_limiter = AsyncMock()
    permit_entered = asyncio.Event()
    release_permit = asyncio.Event()

    class BlockingBrowserLimiter:
        @asynccontextmanager
        async def slot(self):
            permit_entered.set()
            await release_permit.wait()
            yield

    success = TargetResult(
        url="https://example.com",
        status="success",
        data=[{"title": "T"}],
    )

    async def scrape(*_args, **kwargs):
        assert permit_entered.is_set()
        await kwargs["rate_limiter"].acquire(
            "example.com",
            kwargs["domain_rate_limit"],
        )
        return success

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target", new=scrape), \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        cfg = MagicMock()
        cfg.project = "test"
        cfg.name = "browser-rate-test"
        cfg.resolved_targets.return_value = [
            MagicMock(
                url="https://example.com",
                fetcher=FetcherType.dynamic,
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
        mock_settings.return_value = make_settings_mock(adaptive_dir="/tmp/adapt")

        task = asyncio.create_task(
            scrape_task(
                job.job_id,
                "project: test\nname: browser-rate-test",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=rate_limiter,
                browser_limiter=BlockingBrowserLimiter(),
            )
        )
        await asyncio.wait_for(permit_entered.wait(), timeout=1)
        rate_limiter.acquire.assert_not_awaited()
        release_permit.set()
        await task

    rate_limiter.acquire.assert_awaited_once_with("example.com", 3)
