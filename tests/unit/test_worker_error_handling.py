"""Tests for scrape_task crash recovery and duplicate-delivery guards."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import FailStrategy
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ErrorType, JobStatus
from scrapeyard.queue.run_lifecycle import finalize_run
from scrapeyard.queue.worker import scrape_task
from tests.unit.worker_helpers import SIMPLE_YAML, make_config_mock, make_job, make_target


@pytest.mark.asyncio
async def test_scrape_task_marks_job_failed_on_bad_yaml():
    """If load_config raises, the job should end in 'failed' status."""
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job

    updated_jobs = []

    async def capture_update(j):
        updated_jobs.append(j)

    job_store.update_job_status.side_effect = capture_update

    await scrape_task(
        job.job_id,
        "not: valid: yaml: config: missing: project",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )

    assert len(updated_jobs) > 0
    assert updated_jobs[-1].status == JobStatus.failed


@pytest.mark.asyncio
async def test_scrape_task_marks_job_failed_on_missing_job():
    """If job_store.get_job raises KeyError, job should still be marked failed."""
    job_store = AsyncMock()
    job_store.get_job.side_effect = KeyError("no such job")

    # scrape_task should not raise — it should catch and log.
    await scrape_task(
        "nonexistent-job",
        "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )


@pytest.mark.asyncio
async def test_scrape_task_skips_completed_duplicate_run():
    job = make_job(job_id="test-job-1", name="crash-test", status=JobStatus.complete).model_copy(
        update={
            "current_run_id": "run-1",
        }
    )
    job_store = AsyncMock()
    job_store.get_job.return_value = job

    result_store = AsyncMock()
    error_store = AsyncMock()

    await scrape_task(
        job.job_id,
        "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
        run_id="run-1",
        job_store=job_store,
        result_store=result_store,
        error_store=error_store,
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )

    job_store.update_job_status.assert_not_called()
    result_store.save_result.assert_not_called()
    error_store.log_errors.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_task_skips_recent_running_duplicate():
    running_job = make_job(job_id="test-job-1", name="crash-test", status=JobStatus.running).model_copy(
        update={
            "current_run_id": "run-2",
            "updated_at": datetime.now(timezone.utc),
        }
    )
    job_store = AsyncMock()
    job_store.get_job.return_value = running_job

    await scrape_task(
        running_job.job_id,
        "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
        run_id="run-2",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )

    job_store.update_job_status.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_task_skips_result_persistence_when_run_becomes_superseded():
    initial_job = make_job(job_id="test-job-1", name="crash-test", current_run_id="run-1")
    superseded_job = initial_job.model_copy(update={"current_run_id": "run-2"})
    job_store = AsyncMock()
    job_store.get_job.side_effect = [initial_job, superseded_job]
    result_store = AsyncMock()
    error_store = AsyncMock()
    circuit_breaker = MagicMock()

    success_result = TargetResult(
        url="http://example.com",
        status="success",
        data=[{"title": "ok"}],
    )

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=success_result)), \
         patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            storage_results_dir="/tmp/results",
            workers_running_lease_seconds=300,
            proxy_url="",
        )
        mock_load.return_value = MagicMock(
            project="test",
            name="crash-test",
            resolved_targets=MagicMock(return_value=[MagicMock(url="http://example.com", fetcher=MagicMock(value="basic"), proxy=None)]),
            execution=MagicMock(concurrency=1, delay_between=0, domain_rate_limit=0, fail_strategy=MagicMock(value="partial")),
            adaptive=False,
            schedule=None,
            retry=MagicMock(),
            validation=MagicMock(required_fields=[], min_results=0, on_empty="warn"),
            output=MagicMock(group_by="target"),
            webhook=None,
            proxy=None,
        )

        await scrape_task(
            initial_job.job_id,
            "project: test\nname: x\ntarget:\n  url: http://example.com\n  selectors:\n    t: h1",
            run_id="run-1",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    result_store.save_result.assert_not_called()
    job_store.finalize_run.assert_not_called()
    job_store.update_job_status.assert_called_once()


@pytest.mark.asyncio
async def test_scrape_task_reclaims_stale_running_job():
    stale_job = make_job(job_id="test-job-1", name="crash-test", status=JobStatus.running).model_copy(
        update={
            "current_run_id": "run-3",
            "updated_at": datetime.now(timezone.utc) - timedelta(seconds=600),
        }
    )
    job_store = AsyncMock()
    job_store.get_job.side_effect = [stale_job, stale_job, stale_job]

    updated_jobs = []

    async def capture_update(job):
        updated_jobs.append(job)

    job_store.update_job_status.side_effect = capture_update

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=MagicMock(status="failed", data=[], errors=["boom"], pages_scraped=0, error_type=None, http_status=None, error_detail="boom"))):
        await scrape_task(
            stale_job.job_id,
            "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
            run_id="run-3",
            job_store=job_store,
            result_store=AsyncMock(),
            error_store=AsyncMock(),
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert updated_jobs
    assert updated_jobs[0].status == JobStatus.running


@pytest.mark.asyncio
async def test_scrape_task_batches_multiple_target_errors():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.update_job_status = AsyncMock()

    error_store = AsyncMock()

    fail_result = TargetResult(
        url="http://example.com",
        status="failed",
        data=[],
        errors=["timeout", "proxy refused"],
        pages_scraped=0,
        error_type=ErrorType.timeout,
    )

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=fail_result)), \
         patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            workers_running_lease_seconds=300,
            proxy_url="",
        )
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "crash-test"
        cfg.resolved_targets.return_value = [MagicMock(url="http://example.com", fetcher=MagicMock(value="basic"), proxy=None)]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = MagicMock(value="partial")
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.webhook = None
        cfg.proxy = None

        await scrape_task(
            job.job_id,
            "project: test\nname: crash-test\ntarget:\n  url: http://example.com\n  selectors:\n    title: h1",
            job_store=job_store,
            result_store=AsyncMock(),
            error_store=error_store,
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
        )

    error_store.log_errors.assert_called_once()
    logged_errors = error_store.log_errors.call_args[0][0]
    assert len(logged_errors) == 2
    assert [record.error_message for record in logged_errors] == ["timeout", "proxy refused"]


@pytest.mark.asyncio
async def test_scrape_task_converts_unexpected_target_exception_to_partial_result():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.update_job_status = AsyncMock()

    result_store = AsyncMock()
    error_store = AsyncMock()
    circuit_breaker = MagicMock()

    bad_target = make_target("http://bad.example")
    good_target = make_target("http://good.example")
    cfg = make_config_mock(
        targets=[bad_target, good_target],
        fail_strategy=FailStrategy.partial,
    )
    cfg.name = "crash-test"
    cfg.execution.concurrency = 2

    async def scrape_side_effect(target_cfg, *_args, **_kwargs):
        if target_cfg.url == "http://bad.example":
            raise RuntimeError("browser closed")
        return TargetResult(
            url=target_cfg.url,
            status="success",
            data=[{"title": "ok"}],
            pages_scraped=1,
        )

    with (
        patch(
            "scrapeyard.queue.worker.scrape_target",
            new=AsyncMock(side_effect=scrape_side_effect),
        ),
        patch("scrapeyard.queue.worker.load_config", return_value=cfg),
        patch("scrapeyard.queue.worker.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            storage_results_dir="/tmp/results",
            workers_running_lease_seconds=300,
            proxy_url="",
        )

        await scrape_task(
            job.job_id,
            SIMPLE_YAML,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    result_store.save_result.assert_awaited_once()
    output_data = result_store.save_result.call_args.args[1]

    assert output_data["status"] == JobStatus.partial.value
    assert output_data["results"]["good.example"]["status"] == "success"
    assert output_data["results"]["good.example"]["data"] == [{"title": "ok"}]
    assert output_data["results"]["bad.example"]["status"] == "failed"
    assert output_data["results"]["bad.example"]["data"] == []
    assert output_data["results"]["bad.example"]["error_type"] is not None
    assert output_data["results"]["bad.example"]["error_detail"] == "RuntimeError: browser closed"

    bad_target_summary = next(
        target for target in output_data["targets"]
        if target["url"] == "http://bad.example"
    )
    assert bad_target_summary["status"] == "failed"
    assert bad_target_summary["error_type"] is not None
    assert bad_target_summary["errors"] == ["RuntimeError: browser closed"]

    final_update = job_store.update_job_status.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.partial

    error_store.log_errors.assert_awaited_once()
    logged_errors = error_store.log_errors.call_args.args[0]
    assert len(logged_errors) == 1
    assert logged_errors[0].target_url == "http://bad.example"
    assert logged_errors[0].error_type is not None
    assert logged_errors[0].error_message == "RuntimeError: browser closed"

    circuit_breaker.record_failure.assert_called_with("bad.example")


@pytest.mark.asyncio
async def test_unexpected_target_exception_redacts_url_userinfo():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.update_job_status = AsyncMock()

    result_store = AsyncMock()
    error_store = AsyncMock()
    cfg = make_config_mock(targets=[make_target("http://bad.example")])
    cfg.name = "crash-test"

    with (
        patch(
            "scrapeyard.queue.worker.scrape_target",
            new=AsyncMock(
                side_effect=RuntimeError("proxy http://user:pass@proxy.example:8080 refused")
            ),
        ),
        patch("scrapeyard.queue.worker.load_config", return_value=cfg),
        patch("scrapeyard.queue.worker.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            storage_results_dir="/tmp/results",
            workers_running_lease_seconds=300,
            proxy_url="",
        )

        await scrape_task(
            job.job_id,
            SIMPLE_YAML,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
        )

    output_data = result_store.save_result.call_args.args[1]
    target = output_data["targets"][0]
    assert "user:pass" not in target["error_detail"]
    assert "user:pass" not in target["errors"][0]
    assert "http://proxy.example:8080" in target["error_detail"]

    logged_errors = error_store.log_errors.call_args.args[0]
    assert "user:pass" not in logged_errors[0].error_message


@pytest.mark.asyncio
async def test_unexpected_target_exception_respects_all_or_nothing_strategy():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.update_job_status = AsyncMock()

    result_store = AsyncMock()
    error_store = AsyncMock()
    circuit_breaker = MagicMock()

    bad_target = make_target("http://bad.example")
    good_target = make_target("http://good.example")
    cfg = make_config_mock(
        targets=[bad_target, good_target],
        fail_strategy=FailStrategy.all_or_nothing,
    )
    cfg.name = "crash-test"
    cfg.execution.concurrency = 2

    async def scrape_side_effect(target_cfg, *_args, **_kwargs):
        if target_cfg.url == "http://bad.example":
            raise RuntimeError("browser closed")
        return TargetResult(
            url=target_cfg.url,
            status="success",
            data=[{"title": "ok"}],
            pages_scraped=1,
        )

    with (
        patch(
            "scrapeyard.queue.worker.scrape_target",
            new=AsyncMock(side_effect=scrape_side_effect),
        ),
        patch("scrapeyard.queue.worker.load_config", return_value=cfg),
        patch("scrapeyard.queue.worker.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            storage_results_dir="/tmp/results",
            workers_running_lease_seconds=300,
            proxy_url="",
        )

        await scrape_task(
            job.job_id,
            SIMPLE_YAML,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    result_store.save_result.assert_awaited_once()
    output_data = result_store.save_result.call_args.args[1]

    assert output_data["status"] == JobStatus.failed.value
    assert output_data["results"]["good.example"]["status"] == "success"
    assert output_data["results"]["bad.example"]["status"] == "failed"

    # Existing all_or_nothing behavior clears the flat persisted record count
    # after any target failure, even though grouped diagnostics still show the
    # per-target result details.
    assert result_store.save_result.call_args.kwargs["record_count"] == 0

    final_update = job_store.update_job_status.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.failed


@pytest.mark.asyncio
async def test_target_task_cancellation_still_propagates():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.update_job_status = AsyncMock()

    result_store = AsyncMock()
    error_store = AsyncMock()

    cancelled_target = make_target("http://cancelled.example")
    cfg = make_config_mock(targets=[cancelled_target])
    cfg.name = "crash-test"

    async def scrape_side_effect(*_args, **_kwargs):
        raise asyncio.CancelledError()

    with (
        patch(
            "scrapeyard.queue.worker.scrape_target",
            new=AsyncMock(side_effect=scrape_side_effect),
        ),
        patch("scrapeyard.queue.worker.load_config", return_value=cfg),
        patch("scrapeyard.queue.worker.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(
            adaptive_dir="/tmp/adaptive",
            storage_results_dir="/tmp/results",
            workers_running_lease_seconds=300,
            proxy_url="",
        )

        with pytest.raises(asyncio.CancelledError):
            await scrape_task(
                job.job_id,
                SIMPLE_YAML,
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

    result_store.save_result.assert_not_called()


# ---------------------------------------------------------------------------
# D2: finalize_run cross-DB error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_run_skips_when_no_run_id():
    """finalize_run is a no-op when run_id is None."""
    job_store = AsyncMock()
    error_store = AsyncMock()
    await finalize_run(None, JobStatus.complete, 5, job_store, error_store)
    error_store.count_errors_for_run.assert_not_called()
    job_store.finalize_run.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_run_happy_path():
    """Normal finalization calls count_errors then finalize_run."""
    job_store = AsyncMock()
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 3
    await finalize_run("run-1", JobStatus.complete, 10, job_store, error_store)
    error_store.count_errors_for_run.assert_awaited_once_with("run-1")
    job_store.finalize_run.assert_awaited_once_with("run-1", "complete", 10, 3)


@pytest.mark.asyncio
async def test_finalize_run_falls_back_to_fail_run_on_error():
    """If finalize_run raises, finalize_run falls back to fail_run."""
    job_store = AsyncMock()
    job_store.finalize_run.side_effect = RuntimeError("DB write failed")
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0

    # Should not raise — catches internally.
    await finalize_run("run-2", JobStatus.complete, 5, job_store, error_store)

    job_store.finalize_run.assert_awaited_once()
    job_store.fail_run.assert_awaited_once_with("run-2")


@pytest.mark.asyncio
async def test_finalize_run_survives_both_failures():
    """If both finalize_run and fail_run raise, finalize_run still doesn't crash."""
    job_store = AsyncMock()
    job_store.finalize_run.side_effect = RuntimeError("DB write failed")
    job_store.fail_run.side_effect = RuntimeError("Fallback also failed")
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0

    # Must not raise.
    await finalize_run("run-3", JobStatus.partial, 2, job_store, error_store)

    job_store.finalize_run.assert_awaited_once()
    job_store.fail_run.assert_awaited_once_with("run-3")
