from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.common.settings import ServiceSettings
from scrapeyard.config.schema import FailStrategy
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ErrorType, JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.types import SaveResultMeta
from tests.unit.worker_helpers import make_job


def _settings(tmp_path, **overrides) -> ServiceSettings:
    values = {
        "db_dir": str(tmp_path / "db"),
        "storage_results_dir": str(tmp_path / "results"),
        "adaptive_dir": str(tmp_path / "adaptive"),
        "log_dir": str(tmp_path / "logs"),
        "run_max_duration_seconds": 60,
        "run_max_fetched_bytes": 1000,
        "run_max_extracted_records": 3,
        "run_max_serialized_result_bytes": 4096,
        "run_max_browser_debug_bytes": 1000,
    }
    values.update(overrides)
    return ServiceSettings(**values)


def _stores(run_id: str):
    job = make_job(
        job_id="job-budget",
        name="budget-job",
        status=JobStatus.queued,
        current_run_id=run_id,
    )
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.claim_run.return_value = True
    result_store = AsyncMock()
    result_store.save_result.return_value = SaveResultMeta(
        run_id=run_id,
        file_path="/tmp/result",
        record_count=0,
        serialized_bytes=256,
    )
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 1
    return job_store, result_store, error_store


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_strategy",
    [FailStrategy.partial, FailStrategy.continue_, FailStrategy.all_or_nothing],
)
async def test_concurrent_targets_share_one_record_ceiling_and_always_fail(
    tmp_path,
    fail_strategy,
):
    run_id = "run-record-budget"
    job_store, result_store, error_store = _stores(run_id)
    config = f"""
project: test
name: budget-job
targets:
  - url: https://one.example.com
    selectors:
      title: h1
  - url: https://two.example.com
    selectors:
      title: h1
execution:
  concurrency: 2
  fail_strategy: {fail_strategy.value}
webhook:
  url: https://hooks.example.com/budget
  on: [failed]
"""

    async def consume_two(target, *_args, budget, **_kwargs):
        await budget.consume_extracted_records(2)
        return TargetResult(
            url=target.url,
            status="success",
            data=[{"n": 1}, {"n": 2}],
            pages_scraped=1,
        )

    with patch("scrapeyard.queue.worker.scrape_target", consume_two), patch(
        "scrapeyard.queue.worker.get_settings",
        return_value=_settings(tmp_path),
    ):
        await scrape_task(
            "job-budget",
            config,
            run_id=run_id,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
        )

    budget_error = error_store.log_error.await_args.args[0]
    assert budget_error.error_type is ErrorType.budget_exceeded
    assert budget_error.budget.limit_name == "extracted_records"
    assert budget_error.budget.configured_limit == 3
    assert budget_error.budget.observed_amount == 4
    terminal_data = result_store.save_result.await_args.args[1]
    assert terminal_data["status"] == "failed"
    assert terminal_data["results"] == {}
    finalization = job_store.finalize_owned_run.await_args.args
    assert finalization[:5] == (
        "job-budget",
        run_id,
        JobStatus.failed.value,
        0,
        1,
    )
    intent = job_store.finalize_owned_run.await_args.kwargs["webhook_delivery"]
    assert intent is not None
    assert intent.event == "job.failed"
    assert intent.payload["delivery_id"] == intent.delivery_id


@pytest.mark.asyncio
async def test_deadline_cancels_browser_targets_and_releases_permit(tmp_path):
    run_id = "run-deadline"
    job_store, result_store, error_store = _stores(run_id)
    limiter = BrowserExecutionLimiter(1)
    started = asyncio.Event()
    config = """
project: test
name: budget-job
targets:
  - url: https://one.example.com
    fetcher: dynamic
    selectors:
      title: h1
  - url: https://two.example.com
    fetcher: dynamic
    selectors:
      title: h1
execution:
  concurrency: 2
"""

    async def block_target(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    with patch("scrapeyard.queue.worker.scrape_target", block_target), patch(
        "scrapeyard.queue.worker.get_settings",
        return_value=_settings(tmp_path, run_max_duration_seconds=0.02),
    ):
        await scrape_task(
            "job-budget",
            config,
            run_id=run_id,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
            browser_limiter=limiter,
        )

    assert started.is_set()
    assert limiter.active == 0
    budget_error = error_store.log_error.await_args.args[0]
    assert budget_error.budget.limit_name == "run_duration_seconds"
    finalization = job_store.finalize_owned_run.await_args.args
    assert finalization[:5] == (
        "job-budget",
        run_id,
        JobStatus.failed.value,
        0,
        1,
    )
