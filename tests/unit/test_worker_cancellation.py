"""Cooperative worker cancellation boundaries and side-effect suppression."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import (
    BackoffStrategy,
    FetcherType,
    OnEmptyAction,
    RetryConfig,
    TargetConfig,
)
from scrapeyard.engine.pagination import paginate_target
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.resilience import RetryableError, RetryHandler
from scrapeyard.engine.scraper import FetchOutcome, TargetResult
from scrapeyard.models.job import JobRun, JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.error_records import TargetErrorRecorder
from scrapeyard.queue.validation_policy import apply_validation
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.types import RunOwnershipError, SaveResultMeta
from tests.unit.worker_helpers import make_config_mock, make_job, make_settings_mock


async def _run_blocked_worker(
    mock_stores,
    *,
    browser: bool = False,
):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job.return_value = make_job(current_run_id="run-1")
    active = True
    job_store.run_is_active.side_effect = lambda *_args: active
    started = asyncio.Event()
    target = MagicMock()
    target.fetcher = FetcherType.dynamic if browser else FetcherType.basic
    target.url = "https://example.com"
    target.proxy = None
    config = make_config_mock(targets=[target])

    async def _blocked(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    limiter = BrowserExecutionLimiter(1)
    with patch("scrapeyard.queue.worker.load_config", return_value=config), patch(
        "scrapeyard.queue.worker.scrape_target",
        side_effect=_blocked,
    ), patch(
        "scrapeyard.queue.worker.get_settings",
        return_value=make_settings_mock(),
    ):
        task = asyncio.create_task(
            scrape_task(
                "job-1",
                "yaml",
                run_id="run-1",
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                circuit_breaker=circuit_breaker,
                rate_limiter=LocalDomainRateLimiter(),
                browser_limiter=limiter,
                webhook_dispatcher=AsyncMock(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        active = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    return job_store, result_store, error_store, limiter


async def test_cancellation_during_fetch_skips_failure_result_error_and_webhook(
    mock_stores,
):
    job_store, result_store, error_store, limiter = await _run_blocked_worker(
        mock_stores
    )

    job_store.fail_owned_run.assert_not_awaited()
    job_store.finalize_owned_run.assert_not_awaited()
    result_store.save_result.assert_not_awaited()
    error_store.log_errors.assert_not_awaited()
    assert limiter.active == 0
    assert not any(
        task.get_name().startswith("scrapeyard-heartbeat-job-1-run-1")
        for task in asyncio.all_tasks()
    )


async def test_browser_permit_released_after_explicit_run_cancellation(mock_stores):
    _, _, _, limiter = await _run_blocked_worker(mock_stores, browser=True)
    assert limiter.active == 0


async def test_cancellation_after_result_write_discards_artifact_before_finalization(
    mock_stores,
):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job.return_value = make_job(current_run_id="run-1")
    active = True
    job_store.run_is_active.side_effect = lambda *_args: active
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-1",
        file_path="/tmp/results/test/job/run-1",
        record_count=1,
        serialized_bytes=64,
    )

    async def _save(*_args, **_kwargs):
        nonlocal active
        active = False
        return result_store.save_result.return_value

    result_store.save_result.side_effect = _save
    success = TargetResult(
        url="https://example.com",
        status="success",
        data=[{"title": "written"}],
        pages_scraped=1,
    )
    with patch(
        "scrapeyard.queue.worker.load_config",
        return_value=make_config_mock(),
    ), patch(
        "scrapeyard.queue.worker.scrape_target",
        return_value=success,
    ), patch(
        "scrapeyard.queue.worker.get_settings",
        return_value=make_settings_mock(),
    ):
        await scrape_task(
            "job-1",
            "yaml",
            run_id="run-1",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
            webhook_dispatcher=AsyncMock(),
        )

    result_store.delete_result.assert_awaited_once_with("job-1", "run-1")
    job_store.finalize_owned_run.assert_not_awaited()
    job_store.fail_owned_run.assert_not_awaited()


@pytest.mark.parametrize(
    ("durable_status", "owner_stamped", "discarded"),
    [
        (JobStatus.complete, True, False),
        (JobStatus.partial, True, False),
        (JobStatus.cancelled, False, True),
        # A stale recovery's failure carries its own completed_at.
        (JobStatus.failed, False, True),
        # The worker records failed itself (every target failed, a run limit).
        (JobStatus.failed, True, False),
        (None, False, True),
    ],
)
async def test_cancellation_after_finalization_commit_keeps_the_finalized_artifact(
    mock_stores,
    durable_status,
    owner_stamped,
    discarded,
):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job.return_value = make_job(current_run_id="run-1")
    active = True
    job_store.run_is_active.side_effect = lambda *_args: active
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-1",
        file_path="/tmp/results/test/job/run-1",
        record_count=1,
        serialized_bytes=64,
    )

    committed = {}

    async def _commit_then_cancel(*args, **_kwargs):
        # The transaction commits on the database thread, then the awaiting
        # worker is cancelled before it sees the outcome.
        nonlocal active
        active = False
        committed["completed_at"] = args[5]
        raise asyncio.CancelledError

    async def _durable_run(*_args):
        if durable_status is None:
            raise OSError("disk I/O error")
        return JobRun(
            run_id="run-1",
            job_id="job-1",
            status=durable_status,
            trigger="adhoc",
            config_hash="0" * 64,
            completed_at=(
                committed["completed_at"]
                if owner_stamped
                else committed["completed_at"] + timedelta(seconds=1)
            ),
        )

    job_store.finalize_owned_run.side_effect = _commit_then_cancel
    job_store.get_job_run.side_effect = _durable_run
    success = TargetResult(
        url="https://example.com",
        status="success",
        data=[{"title": "written"}],
        pages_scraped=1,
    )
    with patch(
        "scrapeyard.queue.worker.load_config",
        return_value=make_config_mock(),
    ), patch(
        "scrapeyard.queue.worker.scrape_target",
        return_value=success,
    ), patch(
        "scrapeyard.queue.worker.get_settings",
        return_value=make_settings_mock(),
    ):
        with pytest.raises(asyncio.CancelledError):
            await scrape_task(
                "job-1",
                "yaml",
                run_id="run-1",
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                circuit_breaker=circuit_breaker,
                rate_limiter=LocalDomainRateLimiter(),
                webhook_dispatcher=AsyncMock(),
            )

    if discarded:
        result_store.delete_result.assert_awaited_once_with("job-1", "run-1")
    else:
        result_store.delete_result.assert_not_awaited()
    job_store.fail_owned_run.assert_not_awaited()


async def test_pagination_checkpoint_stops_before_next_page_fetch():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/one",
            "fetcher": "basic",
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )
    page = MagicMock()
    page.css.return_value = [MagicMock(attrib={"href": "/two"})]
    fetch = AsyncMock(
        return_value=FetchOutcome(
            page=MagicMock(),
            debug={"final_url": "https://example.com/two"},
        )
    )

    async def _cancel(name: str) -> None:
        if name == "before_pagination_page":
            raise RunOwnershipError(name, "job-1", "run-1")

    with pytest.raises(RunOwnershipError):
        await paginate_target(
            page=page,
            target=target,
            result=TargetResult(
                url=target.url,
                status="success",
                debug={"final_url": target.url},
            ),
            fetch_target_page=fetch,
            extract_page_data=MagicMock(return_value=[]),
            retry_handler=MagicMock(),
            fetcher_cls=object(),
            adaptive=False,
            retryable_status=set(),
            adaptive_dir="/tmp/adaptive",
            proxy_url=None,
            artifacts_dir=None,
            cancellation_guard=_cancel,
        )
    fetch.assert_not_awaited()


async def test_retry_backoff_checkpoint_stops_before_wait(monkeypatch):
    handler = RetryHandler(
        RetryConfig(
            max_attempts=2,
            backoff=BackoffStrategy.fixed,
            backoff_max=1,
        ),
        cancellation_guard=AsyncMock(
            side_effect=lambda name: (
                (_ for _ in ()).throw(
                    RunOwnershipError(name, "job-1", "run-1")
                )
                if name == "before_retry_backoff"
                else None
            )
        ),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("scrapeyard.engine.resilience.asyncio.sleep", sleep)

    with pytest.raises(RunOwnershipError):
        await handler.execute(AsyncMock(side_effect=RetryableError(503)))

    sleep.assert_not_awaited()


async def test_validation_retry_checkpoint_stops_before_second_scrape():
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com",
            "fetcher": "basic",
            "selectors": {"title": "h1"},
        }
    )
    validator = MagicMock()
    validator.validate.return_value = MagicMock(
        passed=False,
        action=OnEmptyAction.retry,
        message="empty",
    )
    scrape = AsyncMock()

    async def _cancel(name: str) -> None:
        if name == "before_validation_retry":
            raise RunOwnershipError(name, "job-1", "run-1")

    with pytest.raises(RunOwnershipError):
        await apply_validation(
            target_cfg=target,
            domain="example.com",
            adaptive=False,
            result=TargetResult(url=target.url, status="success", data=[]),
            config=make_config_mock(),
            adaptive_dir="/tmp/adaptive",
            run_artifacts_dir=None,
            recorder=TargetErrorRecorder(
                job_id="job-1",
                run_id="run-1",
                project="test",
                pending_errors=[],
                circuit_breaker=MagicMock(),
            ),
            rate_limiter=AsyncMock(),
            validator=validator,
            scrape=scrape,
            cancellation_guard=_cancel,
        )
    scrape.assert_not_awaited()
