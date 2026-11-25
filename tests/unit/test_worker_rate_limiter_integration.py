"""Tests that scrape_task uses the injected rate_limiter with real stores and configs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from scrapeyard.engine.resilience import CircuitBreaker
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.database import init_db
from scrapeyard.storage.error_store import SQLiteErrorStore
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.result_store import LocalResultStore
from tests.factories import make_job_model, make_scrape_yaml


class RecordingRateLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    async def acquire(self, domain: str, min_interval: float) -> None:
        self.calls.append((domain, min_interval))


@pytest.mark.asyncio
async def test_scrape_task_passes_rate_limiter_to_request_boundary(tmp_path):
    await init_db(str(tmp_path / "db"))
    job_store = SQLiteJobStore()
    result_store = LocalResultStore(str(tmp_path / "results"), job_store)
    error_store = SQLiteErrorStore()
    config_yaml = make_scrape_yaml(
        project="test",
        name="rate-test",
        url="https://example.com",
        extra_top_level="execution:\n  domain_rate_limit: 3\n",
    )
    job = make_job_model(
        job_id="j-rate",
        project="test",
        name="rate-test",
        config_yaml=config_yaml,
        status=JobStatus.queued,
    )
    await job_store.save_job(job)
    rate_limiter = RecordingRateLimiter()
    observed_kwargs: dict[str, object] = {}

    async def fake_scrape(*_args, **kwargs):
        observed_kwargs.update(kwargs)
        return TargetResult(
            url="https://example.com", status="success", data=[{"title": "T"}],
        )

    with patch("scrapeyard.queue.worker.scrape_target", new=fake_scrape):
        await scrape_task(
            job.job_id,
            config_yaml,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=CircuitBreaker(5, 60),
            rate_limiter=rate_limiter,
        )

    assert rate_limiter.calls == []
    assert observed_kwargs["rate_limiter"] is rate_limiter
    assert observed_kwargs["domain_rate_limit"] == 3


@pytest.mark.asyncio
async def test_browser_permit_is_acquired_before_request_rate_limit(tmp_path):
    await init_db(str(tmp_path / "db"))
    job_store = SQLiteJobStore()
    result_store = LocalResultStore(str(tmp_path / "results"), job_store)
    error_store = SQLiteErrorStore()
    config_yaml = make_scrape_yaml(
        project="test",
        name="browser-rate-test",
        url="https://example.com",
        extra_top_level="execution:\n  domain_rate_limit: 3\n",
        extra_target="  fetcher: dynamic\n",
    )
    job = make_job_model(
        job_id="j-browser-rate",
        project="test",
        name="browser-rate-test",
        config_yaml=config_yaml,
        status=JobStatus.queued,
    )
    await job_store.save_job(job)
    rate_limiter = RecordingRateLimiter()
    permit_entered = asyncio.Event()
    release_permit = asyncio.Event()

    class BlockingBrowserLimiter:
        @asynccontextmanager
        async def slot(self):
            permit_entered.set()
            await release_permit.wait()
            yield

    async def fake_scrape(*_args, **kwargs):
        assert permit_entered.is_set()
        await kwargs["rate_limiter"].acquire(
            "example.com",
            kwargs["domain_rate_limit"],
        )
        return TargetResult(
            url="https://example.com", status="success", data=[{"title": "T"}],
        )

    with patch("scrapeyard.queue.worker.scrape_target", new=fake_scrape):
        task = asyncio.create_task(
            scrape_task(
                job.job_id,
                config_yaml,
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                circuit_breaker=CircuitBreaker(5, 60),
                rate_limiter=rate_limiter,
                browser_limiter=BlockingBrowserLimiter(),
            )
        )
        await asyncio.wait_for(permit_entered.wait(), timeout=1)
        assert rate_limiter.calls == []
        release_permit.set()
        await task

    assert rate_limiter.calls == [("example.com", 3)]
