"""Test fail_strategy behavior in scrape_task."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import FailStrategy, WebhookConfig, WebhookStatus
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.types import SaveResultMeta
from tests.unit.worker_helpers import (
    finalized_status,
    make_job,
    make_settings_mock,
    make_target,
)


@pytest.mark.asyncio
async def test_partial_returns_partial_on_mixed(mock_stores):
    """partial: mixed success/failure yields JobStatus.partial."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [make_target("http://a.com"), make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.partial
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.proxy = None

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert finalized_status(job_store) == JobStatus.partial


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_by", "empty_results"),
    [("target", {}), ("merge", [])],
)
async def test_all_or_nothing_fails_atomically_in_both_grouping_modes(
    mock_stores,
    group_by,
    empty_results,
):
    """all_or_nothing: any failure yields JobStatus.failed, no results saved."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-result",
        file_path="/tmp/results/run-result",
        record_count=0,
        serialized_bytes=256,
    )

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [make_target("http://a.com"), make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.all_or_nothing
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = group_by
        cfg.proxy = None
        cfg.webhook = WebhookConfig(
            url="https://hooks.example.com/callback",
            on=[WebhookStatus.failed],
        )

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert finalized_status(job_store) == JobStatus.failed
    result_store.save_result.assert_called_once()
    call_kwargs = result_store.save_result.call_args
    assert call_kwargs.kwargs["record_count"] == 0
    assert call_kwargs.kwargs["status"] == "failed"
    artifact = call_kwargs.args[1]
    assert artifact["results"] == empty_results
    successful_target = next(
        target for target in artifact["targets"] if target["url"] == "http://a.com"
    )
    assert successful_target["count"] == 0
    assert successful_target["observed_count"] == 1
    finalization = job_store.finalize_owned_run.await_args
    assert finalization.args[3] == 0
    assert finalization.kwargs["webhook_delivery"].payload["result_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", ["target", "merge"])
async def test_fully_successful_all_or_nothing_publishes_all_records(
    mock_stores,
    group_by,
):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-result",
        file_path="/tmp/results/run-result",
        record_count=1,
        serialized_bytes=256,
    )
    success_result = TargetResult(
        url="http://a.com",
        status="success",
        data=[{"title": "A"}],
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, patch(
        "scrapeyard.queue.worker.scrape_target",
        return_value=success_result,
    ), patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [make_target("http://a.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.all_or_nothing
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = group_by
        cfg.proxy = None
        cfg.webhook = WebhookConfig(url="https://hooks.example.com/callback")

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    artifact = result_store.save_result.await_args.args[1]
    if group_by == "target":
        assert artifact["results"]["a.com"]["data"] == [{"title": "A"}]
    else:
        assert artifact["results"] == [{"title": "A", "_source": "a.com"}]
    assert result_store.save_result.await_args.kwargs["record_count"] == 1
    assert job_store.finalize_owned_run.await_args.args[3] == 1
    assert (
        job_store.finalize_owned_run.await_args.kwargs["webhook_delivery"].payload[
            "result_count"
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_continue_completes_even_with_failures(mock_stores):
    """continue: failures don't affect status if data exists."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [make_target("http://a.com"), make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.continue_
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.proxy = None

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert finalized_status(job_store) == JobStatus.complete
    result_store.save_result.assert_called_once()
    assert result_store.save_result.call_args.args[1]["results"]["a.com"]["data"] == [
        {"title": "A"}
    ]


@pytest.mark.asyncio
async def test_worker_passes_record_count_to_save_result(mock_stores):
    """Worker must pass len(flat_data) as record_count to save_result."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)

    success_result = TargetResult(
        url="http://a.com", status="success", data=[{"title": "A"}, {"title": "B"}]
    )

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [make_target("http://a.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.partial
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.proxy = None

        mock_scrape.return_value = success_result

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    result_store.save_result.assert_called_once()
    call_kwargs = result_store.save_result.call_args
    assert call_kwargs.kwargs.get("record_count") == 2


@pytest.mark.asyncio
async def test_worker_passes_final_status_to_save_result(mock_stores):
    """Worker must persist the computed final job status with the result metadata."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = make_job()
    job_store.get_job = AsyncMock(return_value=job)

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = make_settings_mock()
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [make_target("http://a.com"), make_target("http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.partial
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.proxy = None

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", "yaml",
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    result_store.save_result.assert_called_once()
    call_kwargs = result_store.save_result.call_args
    assert call_kwargs.kwargs.get("status") == "partial"
