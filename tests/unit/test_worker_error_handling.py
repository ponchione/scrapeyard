"""Tests for scrape_task crash recovery and duplicate-delivery guards."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName
from scrapeyard.config.schema import FailStrategy, FetcherType, WebhookConfig
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.resilience import CircuitBreaker, CircuitProbe, CircuitState
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ErrorType, JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.error_records import TargetErrorRecorder
from scrapeyard.queue.target_execution import TargetRuntimeContext
from scrapeyard.queue.worker import _fetch_and_validate_target, scrape_task
from scrapeyard.storage.types import RunOwnershipError
from tests.unit.worker_helpers import (
    SIMPLE_YAML,
    finalized_status,
    make_config_mock,
    make_job,
    make_settings_mock,
    make_target,
)


@pytest.mark.asyncio
async def test_scrape_task_marks_job_failed_on_bad_yaml():
    """If load_config raises, the job should end in 'failed' status."""
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job

    await scrape_task(
        job.job_id,
        "not: valid: yaml: config: missing: project",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=LocalDomainRateLimiter(),
    )

    job_store.fail_owned_run.assert_not_awaited()


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



@pytest.mark.asyncio
async def test_scrape_task_discards_result_when_finalization_loses_ownership():
    initial_job = make_job(job_id="test-job-1", name="crash-test", current_run_id="run-1")
    job_store = AsyncMock()
    job_store.get_job.return_value = initial_job
    job_store.claim_run.return_value = True
    job_store.finalize_owned_run.side_effect = RunOwnershipError(
        "finalize",
        initial_job.job_id,
        "run-1",
    )
    result_store = AsyncMock()
    error_store = AsyncMock()
    error_store.count_errors_for_run.return_value = 0
    circuit_breaker = MagicMock()
    webhook_dispatcher = AsyncMock()

    success_result = TargetResult(
        url="http://example.com",
        status="success",
        data=[{"title": "ok"}],
    )

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=success_result)), \
         patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.get_settings") as mock_settings, \
         patch("scrapeyard.queue.worker.observe_run") as observe_run:
        mock_settings.return_value = make_settings_mock()
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
            webhook=WebhookConfig(url="https://hooks.example.com/callback"),
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
            webhook_dispatcher=webhook_dispatcher,
        )

    result_store.save_result.assert_awaited_once()
    result_store.delete_result.assert_awaited_once_with(
        initial_job.job_id,
        "run-1",
    )
    job_store.finalize_owned_run.assert_awaited_once()
    job_store.fail_owned_run.assert_not_awaited()
    webhook_dispatcher.submit.assert_not_awaited()
    assert observe_run.call_args.kwargs["status"] == "ignored"


@pytest.mark.asyncio
async def test_scrape_task_never_reclaims_running_job_inside_worker():
    stale_job = make_job(job_id="test-job-1", name="crash-test", status=JobStatus.running).model_copy(
        update={
            "current_run_id": "run-3",
            "updated_at": datetime.now(timezone.utc) - timedelta(seconds=600),
        }
    )
    job_store = AsyncMock()
    job_store.get_job.return_value = stale_job
    result_store = AsyncMock()

    with patch("scrapeyard.queue.worker.scrape_target", new=AsyncMock(return_value=MagicMock(status="failed", data=[], errors=["boom"], pages_scraped=0, error_type=None, http_status=None, error_detail="boom"))):
        await scrape_task(
            stale_job.job_id,
            "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1",
            run_id="run-3",
            job_store=job_store,
            result_store=result_store,
            error_store=AsyncMock(),
            circuit_breaker=MagicMock(),
            rate_limiter=LocalDomainRateLimiter(),
        )

    job_store.claim_run.assert_not_awaited()
    job_store.recover_stale_run.assert_not_awaited()
    result_store.save_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_scrape_task_batches_multiple_target_errors():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.queue_run.return_value = True
    job_store.claim_run.return_value = True

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
        mock_settings.return_value = make_settings_mock()
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
    job_store.queue_run.return_value = True
    job_store.claim_run.return_value = True

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
        mock_settings.return_value = make_settings_mock()

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

    assert finalized_status(job_store) == JobStatus.partial

    error_store.log_errors.assert_awaited_once()
    logged_errors = error_store.log_errors.call_args.args[0]
    assert len(logged_errors) == 1
    assert logged_errors[0].target_url == "http://bad.example"
    assert logged_errors[0].error_type is not None
    assert logged_errors[0].error_message == "RuntimeError: browser closed"

    circuit_breaker.record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_unexpected_target_exception_redacts_url_userinfo():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.queue_run.return_value = True
    job_store.claim_run.return_value = True

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
        mock_settings.return_value = make_settings_mock()

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
    job_store.queue_run.return_value = True
    job_store.claim_run.return_value = True

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
        mock_settings.return_value = make_settings_mock()

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
    assert output_data["results"] == {}
    assert {target["status"] for target in output_data["targets"]} == {
        "success",
        "failed",
    }
    assert next(
        target
        for target in output_data["targets"]
        if target["url"] == "http://good.example"
    )["observed_count"] == 1
    assert result_store.save_result.call_args.kwargs["record_count"] == 0

    assert finalized_status(job_store) == JobStatus.failed


@pytest.mark.asyncio
async def test_target_task_cancellation_still_propagates():
    job = make_job(job_id="test-job-1", name="crash-test")
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.queue_run.return_value = True
    job_store.claim_run.return_value = True

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
        mock_settings.return_value = make_settings_mock()

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
    assert not any(
        task.get_name().startswith("scrapeyard-heartbeat-") and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_sustained_heartbeat_failure_cancels_targets_without_stale_mutation():
    job = make_job(
        job_id="job-lease-loss",
        name="lease-loss",
        current_run_id="run-lease-loss",
    )
    job_store = AsyncMock()
    job_store.get_job.return_value = job
    job_store.claim_run.return_value = True
    job_store.heartbeat_run.side_effect = OSError("database unavailable")
    result_store = AsyncMock()
    error_store = AsyncMock()
    limiter = BrowserExecutionLimiter(1)
    target_started = asyncio.Event()
    target = MagicMock(
        url="https://browser.example",
        fetcher=FetcherType.dynamic,
        proxy=None,
    )
    config = make_config_mock(
        targets=[target],
        webhook=WebhookConfig(url="https://hooks.example.com/callback"),
    )
    webhook_dispatcher = AsyncMock()

    async def _block_target(*_args, **_kwargs):
        target_started.set()
        await asyncio.Event().wait()

    with (
        patch("scrapeyard.queue.worker.load_config", return_value=config),
        patch("scrapeyard.queue.worker.scrape_target", side_effect=_block_target),
        patch(
            "scrapeyard.queue.worker.get_settings",
            return_value=make_settings_mock(
                workers_heartbeat_interval_seconds=0.01,
                workers_running_heartbeat_timeout_seconds=0.03,
            ),
        ),
    ):
        task = asyncio.create_task(
            scrape_task(
                job.job_id,
                SIMPLE_YAML,
                run_id="run-lease-loss",
                job_store=job_store,
                result_store=result_store,
                error_store=error_store,
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
                browser_limiter=limiter,
                webhook_dispatcher=webhook_dispatcher,
            )
        )
        await asyncio.wait_for(target_started.wait(), timeout=1)
        await asyncio.wait_for(task, timeout=1)

    assert job_store.heartbeat_run.await_count >= 3
    job_store.finalize_owned_run.assert_not_awaited()
    job_store.fail_owned_run.assert_not_awaited()
    webhook_dispatcher.submit.assert_not_awaited()
    result_store.delete_result.assert_awaited_once_with(
        job.job_id,
        "run-lease-loss",
    )
    assert limiter.active == 0
    assert not any(
        pending.get_name().startswith("scrapeyard-heartbeat-")
        and not pending.done()
        for pending in asyncio.all_tasks()
    )


def _browser_budget_context(circuit_breaker: CircuitBreaker) -> tuple[MagicMock, MagicMock]:
    target = make_target("https://browser.example")
    target.fetcher = FetcherType.dynamic
    recorder = TargetErrorRecorder(
        job_id="job-budget-circuit",
        run_id="run-budget-circuit",
        project="test",
        pending_errors=[],
        circuit_breaker=circuit_breaker,
    )
    context = MagicMock()
    context.recorder.return_value = recorder
    context.circuit_breaker = circuit_breaker
    context.config.retry = MagicMock()
    context.browser_limiter = None
    context.activity.checkpoint = AsyncMock()
    return target, context


@pytest.mark.asyncio
async def test_browser_fetch_deadline_preserves_closed_circuit_failures():
    circuit_breaker = CircuitBreaker(3, 60)
    circuit_breaker.record_failure("browser.example")
    circuit_breaker.record_failure("browser.example")
    target, context = _browser_budget_context(circuit_breaker)
    runtime = TargetRuntimeContext(
        domain="browser.example",
        adaptive=False,
        proxy_url=None,
        artifacts_dir=None,
    )

    with (
        patch(
            "scrapeyard.queue.worker.resolve_target_runtime_context",
            return_value=runtime,
        ),
        patch(
            "scrapeyard.queue.worker.guard_target_execution",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "scrapeyard.queue.worker.scrape_target",
            new=AsyncMock(
                side_effect=BudgetExceeded(
                    BudgetLimitName.run_duration_seconds,
                    1,
                    1,
                )
            ),
        ),
    ):
        with pytest.raises(BudgetExceeded):
            await _fetch_and_validate_target(
                target_cfg=target,
                context=context,
                pending_errors=[],
            )

    assert runtime.upstream_response_observed is False
    assert circuit_breaker.state("browser.example") is CircuitState.closed
    circuit_breaker.record_failure("browser.example")
    assert circuit_breaker.state("browser.example") is CircuitState.open


@pytest.mark.asyncio
async def test_half_open_browser_fetch_deadline_aborts_probe_without_closing():
    clock = [0.0]
    circuit_breaker = CircuitBreaker(1, 10, clock=lambda: clock[0])
    circuit_breaker.record_failure("browser.example")
    clock[0] = 10.0
    probe = circuit_breaker.check("browser.example")
    assert isinstance(probe, CircuitProbe)
    target, context = _browser_budget_context(circuit_breaker)
    runtime = TargetRuntimeContext(
        domain="browser.example",
        adaptive=False,
        proxy_url=None,
        artifacts_dir=None,
        circuit_probe=probe,
    )

    with (
        patch(
            "scrapeyard.queue.worker.resolve_target_runtime_context",
            return_value=runtime,
        ),
        patch(
            "scrapeyard.queue.worker.guard_target_execution",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "scrapeyard.queue.worker.scrape_target",
            new=AsyncMock(
                side_effect=BudgetExceeded(
                    BudgetLimitName.run_duration_seconds,
                    1,
                    1,
                )
            ),
        ),
    ):
        with pytest.raises(BudgetExceeded):
            await _fetch_and_validate_target(
                target_cfg=target,
                context=context,
                pending_errors=[],
            )

    assert circuit_breaker.state("browser.example") is CircuitState.open
    replacement = circuit_breaker.check("browser.example")
    assert isinstance(replacement, CircuitProbe)
    assert replacement.generation != probe.generation
