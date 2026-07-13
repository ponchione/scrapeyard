from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.common.budgets import RunBudget
from scrapeyard.config.schema import FailStrategy, FetcherType, GroupBy, OnEmptyAction
from scrapeyard.engine.scraper import TargetResult, TargetStatus
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.target_execution import resolve_target_runtime_context
from scrapeyard.queue.worker import (
    JobExecutionContext,
    _collect_result_payload,
    _format_output,
    _persist_job_results,
    _process_all_targets,
)
from scrapeyard.storage.types import SaveResultMeta


def _concurrent_target(url: str, fetcher: FetcherType) -> MagicMock:
    return MagicMock(url=url, fetcher=fetcher, proxy=None)


def _job_execution_context(
    targets: list[MagicMock],
    *,
    concurrency: int | None = None,
) -> JobExecutionContext:
    config = MagicMock()
    config.project = "test"
    config.resolved_targets.return_value = targets
    config.execution.concurrency = concurrency or len(targets)
    config.execution.delay_between = 0
    config.execution.domain_rate_limit = 0
    config.adaptive = False
    config.schedule = None
    config.proxy = None
    config.retry = MagicMock()
    config.validation.required_fields = []
    config.validation.min_results = 0
    config.validation.on_empty = OnEmptyAction.warn
    return JobExecutionContext(
        config=config,
        job=MagicMock(),
        settings=MagicMock(proxy_url=""),
        started_at=datetime.now(timezone.utc),
        adaptive_dir="/tmp/adaptive",
        run_artifacts_dir=None,
        budget=RunBudget(
            max_duration_seconds=60,
            max_fetched_bytes=1_000_000,
            max_extracted_records=10_000,
            max_serialized_result_bytes=1_000_000,
            max_browser_debug_bytes=1_000_000,
        ),
        run_id="run-test",
        activity=MagicMock(checkpoint=AsyncMock()),
    )


async def _run_targets(
    context: JobExecutionContext,
    limiter: BrowserExecutionLimiter,
    *,
    job_id: str,
) -> list[TargetResult]:
    circuit_breaker = MagicMock()
    return await _process_all_targets(
        context=context,
        job_id=job_id,
        run_id=f"run-{job_id}",
        circuit_breaker=circuit_breaker,
        rate_limiter=AsyncMock(),
        browser_limiter=limiter,
        error_store=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_browser_limiter_caps_multiple_targets_in_one_job():
    limiter = BrowserExecutionLimiter(2)
    targets = [
        _concurrent_target(f"https://browser-{index}.example", FetcherType.dynamic)
        for index in range(4)
    ]
    context = _job_execution_context(targets)
    release = asyncio.Event()
    first_pair_started = asyncio.Event()
    active_fetches = 0
    max_active_fetches = 0
    total_started = 0

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        nonlocal active_fetches, max_active_fetches, total_started
        active_fetches += 1
        total_started += 1
        max_active_fetches = max(max_active_fetches, active_fetches)
        if total_started == 2:
            first_pair_started.set()
        try:
            await release.wait()
            return TargetResult(url=target.url, status=TargetStatus.success)
        finally:
            active_fetches -= 1

    with patch("scrapeyard.queue.worker.scrape_target", side_effect=_scrape) as scrape:
        task = asyncio.create_task(_run_targets(context, limiter, job_id="one-job"))
        await asyncio.wait_for(first_pair_started.wait(), timeout=1)

        assert limiter.active == 2
        assert scrape.call_count == 2

        release.set()
        results = await asyncio.wait_for(task, timeout=1)

    assert len(results) == 4
    assert max_active_fetches == 2
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_delay_between_paces_actual_target_starts_after_semaphore_waits():
    limiter = BrowserExecutionLimiter(2)
    targets = [
        _concurrent_target(f"https://target-{index}.example", FetcherType.basic)
        for index in range(4)
    ]
    context = _job_execution_context(targets, concurrency=2)
    context.config.execution.delay_between = 0.02
    started: list[float] = []

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        started.append(time.monotonic())
        index = int(target.url.split("-")[1].split(".")[0])
        await asyncio.sleep({0: 0.1, 1: 0.08}.get(index, 0))
        return TargetResult(url=target.url, status=TargetStatus.success)

    with patch("scrapeyard.queue.worker.scrape_target", side_effect=_scrape):
        results = await _run_targets(context, limiter, job_id="paced")

    assert len(results) == 4
    assert all(
        later - earlier >= 0.015
        for earlier, later in zip(started, started[1:], strict=False)
    )


@pytest.mark.asyncio
async def test_browser_limiter_is_shared_across_multiple_jobs():
    limiter = BrowserExecutionLimiter(2)
    contexts = [
        _job_execution_context(
            [
                _concurrent_target(
                    f"https://job-{job_index}-target-{target_index}.example",
                    FetcherType.stealthy,
                )
                for target_index in range(2)
            ]
        )
        for job_index in range(2)
    ]
    release = asyncio.Event()
    first_pair_started = asyncio.Event()
    active_fetches = 0
    max_active_fetches = 0
    total_started = 0

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        nonlocal active_fetches, max_active_fetches, total_started
        active_fetches += 1
        total_started += 1
        max_active_fetches = max(max_active_fetches, active_fetches)
        if total_started == 2:
            first_pair_started.set()
        try:
            await release.wait()
            return TargetResult(url=target.url, status=TargetStatus.success)
        finally:
            active_fetches -= 1

    with patch("scrapeyard.queue.worker.scrape_target", side_effect=_scrape):
        tasks = [
            asyncio.create_task(_run_targets(context, limiter, job_id=f"job-{index}"))
            for index, context in enumerate(contexts)
        ]
        await asyncio.wait_for(first_pair_started.wait(), timeout=1)
        assert limiter.active == 2

        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

    assert [len(job_results) for job_results in results] == [2, 2]
    assert max_active_fetches == 2
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_basic_target_runs_while_browser_permit_is_exhausted():
    limiter = BrowserExecutionLimiter(1)
    dynamic = _concurrent_target("https://browser.example", FetcherType.dynamic)
    basic = _concurrent_target("https://basic.example", FetcherType.basic)
    context = _job_execution_context([dynamic, basic])
    browser_started = asyncio.Event()
    release_browser = asyncio.Event()
    basic_completed = asyncio.Event()

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        if target.fetcher is FetcherType.dynamic:
            browser_started.set()
            await release_browser.wait()
        else:
            basic_completed.set()
        return TargetResult(url=target.url, status=TargetStatus.success)

    with patch("scrapeyard.queue.worker.scrape_target", side_effect=_scrape):
        task = asyncio.create_task(_run_targets(context, limiter, job_id="mixed"))
        await asyncio.wait_for(browser_started.wait(), timeout=1)
        await asyncio.wait_for(basic_completed.wait(), timeout=1)
        assert limiter.active == 1

        release_browser.set()
        await asyncio.wait_for(task, timeout=1)

    assert limiter.active == 0


@pytest.mark.asyncio
async def test_browser_permit_recovers_after_target_exception():
    limiter = BrowserExecutionLimiter(1)
    target = _concurrent_target("https://failing.example", FetcherType.dynamic)
    context = _job_execution_context([target])

    async def _raise(*_args, **_kwargs) -> TargetResult:
        raise RuntimeError("browser crashed")

    with patch("scrapeyard.queue.worker.scrape_target", side_effect=_raise):
        results = await _run_targets(context, limiter, job_id="failure")

    assert results[0].status is TargetStatus.failed
    assert results[0].error_detail == "RuntimeError: browser crashed"
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_target_metric_classifies_pre_result_exception_as_failed():
    limiter = BrowserExecutionLimiter(1)
    target = _concurrent_target("https://failing.example", FetcherType.basic)
    context = _job_execution_context([target])

    with (
        patch(
            "scrapeyard.queue.worker._fetch_and_validate_target",
            side_effect=RuntimeError("failed before result"),
        ),
        patch("scrapeyard.queue.worker.observe_target") as observe,
        pytest.raises(RuntimeError, match="failed before result"),
    ):
        await _run_targets(context, limiter, job_id="failure-metric")

    assert observe.call_args.kwargs["status"] == "failed"


@pytest.mark.asyncio
async def test_browser_permit_recovers_after_target_cancellation():
    limiter = BrowserExecutionLimiter(1)
    target = _concurrent_target("https://cancelled.example", FetcherType.stealthy)
    context = _job_execution_context([target])
    browser_started = asyncio.Event()

    async def _block(*_args, **_kwargs) -> TargetResult:
        browser_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with patch("scrapeyard.queue.worker.scrape_target", side_effect=_block):
        task = asyncio.create_task(_run_targets(context, limiter, job_id="cancelled"))
        await asyncio.wait_for(browser_started.wait(), timeout=1)
        assert limiter.active == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert limiter.active == 0


def test_collect_result_payload_flattens_data_and_errors_in_order():
    results = [
        TargetResult(
            url="https://a.example",
            status="success",
            data=[{"sku": "a1"}, {"sku": "a2"}],
            errors=["warn-a"],
        ),
        TargetResult(
            url="https://b.example",
            status="failed",
            data=[],
            errors=["err-b1", "err-b2"],
        ),
    ]

    flat_data, all_errors = _collect_result_payload(results)

    assert flat_data == [{"sku": "a1"}, {"sku": "a2"}]
    assert all_errors == ["warn-a", "err-b1", "err-b2"]


def test_resolve_target_runtime_context_uses_explicit_adaptive_override_and_proxy():
    target_cfg = MagicMock(url="https://shop.example/products", proxy=None)
    config = MagicMock()
    config.adaptive = True
    config.schedule = None
    config.proxy = None
    settings = MagicMock(proxy_url="http://service-proxy:8080")

    context = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir="/tmp/artifacts",
    )

    assert context.domain == "shop.example"
    assert context.adaptive is True
    assert context.proxy_url == "http://service-proxy:8080"
    assert context.artifacts_dir == "/tmp/artifacts/shop.example"


def test_resolve_target_runtime_context_strips_userinfo_from_domain():
    target_cfg = MagicMock(url="https://user:pass@shop.example:8443/products", proxy=None)
    config = MagicMock(adaptive=False, schedule=None, proxy=None)
    settings = MagicMock(proxy_url="")

    context = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir="/tmp/artifacts",
    )

    assert context.domain == "shop.example:8443"
    assert context.artifacts_dir == "/tmp/artifacts/shop.example:8443"


def test_resolve_target_runtime_context_enables_adaptive_for_scheduled_jobs_when_unspecified():
    target_cfg = MagicMock(url="https://shop.example/products", proxy=None)
    config = MagicMock()
    config.adaptive = None
    config.schedule = MagicMock()
    config.proxy = None
    settings = MagicMock(proxy_url="")

    context = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir=None,
    )

    assert context.domain == "shop.example"
    assert context.adaptive is True
    assert context.proxy_url is None
    assert context.artifacts_dir is None


def test_format_output_merges_results_with_source_domains_and_target_metadata():
    config = MagicMock(project="test", name="job")
    config.output.group_by = GroupBy.merge
    results = [
        TargetResult(url="https://a.example/products", status="success", data=[{"sku": "a1"}], errors=[], pages_scraped=1),
        TargetResult(url="https://b.example/products", status="failed", data=["raw-item"], errors=["boom"], pages_scraped=1),
    ]

    payload = _format_output(
        config,
        results,
        "job-1",
        JobStatus.partial,
        ["boom"],
    )

    assert payload["status"] == "partial"
    assert payload["targets"] == [
        {
            "url": "https://a.example/products",
            "status": "success",
            "count": 1,
            "observed_count": 1,
            "pages_scraped": 1,
            "error_type": None,
            "error_detail": None,
            "errors": [],
            "debug": None,
        },
        {
            "url": "https://b.example/products",
            "status": "failed",
            "count": 1,
            "observed_count": 1,
            "pages_scraped": 1,
            "error_type": None,
            "error_detail": None,
            "errors": ["boom"],
            "debug": None,
        },
    ]
    assert payload["results"] == [{"sku": "a1", "_source": "a.example"}, "raw-item"]
    assert results[0].data == [{"sku": "a1"}]


def test_format_output_rejects_reserved_source_field_in_merge_record():
    config = MagicMock(project="test", name="job")
    config.output.group_by = GroupBy.merge
    results = [
        TargetResult(
            url="https://shop.example/products",
            status="success",
            data=[{"_source": "catalog-feed", "sku": "a1"}],
        )
    ]

    with pytest.raises(
        ValueError,
        match="reserved merge field '_source'",
    ):
        _format_output(config, results, "job-1", JobStatus.complete, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", [GroupBy.merge, GroupBy.target])
async def test_persisted_results_preserve_public_data_and_redact_resolved_secrets(
    group_by: GroupBy,
):
    secret = "Vendor Value/91"
    target = MagicMock(url="https://shop.example/items", fetcher=FetcherType.basic)
    context = _job_execution_context([target])
    context.config.output.group_by = group_by
    context.config.execution.fail_strategy = FailStrategy.partial
    context.config.resolved_secret_values = (secret,)
    result_store = AsyncMock()
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-1",
        file_path="/tmp/results/job-1/run-1.json",
        record_count=1,
        serialized_bytes=512,
    )
    extracted = {
        "key": "product-key",
        "session_name": "morning",
        "api_token": "public-token",
        "product_url": (
            "https://shop.example/item?color=red&page=2#reviews"
        ),
        "deployment_secret": f"Bearer {secret}",
        "encoded_secret": "Vendor%20Value%2F91",
        "plus_encoded_secret": "Vendor+Value%2F91",
    }
    result = TargetResult(
        url="https://shop.example/items?cursor=public#inventory",
        status=TargetStatus.success,
        data=[extracted],
        debug={"session_name": "diagnostic-session"},
    )

    await _persist_job_results(
        context=context,
        job_id="job-1",
        run_id="run-1",
        all_results=[result],
        result_store=result_store,
    )

    artifact = result_store.save_result.await_args.args[1]
    if group_by == GroupBy.merge:
        persisted_record = artifact["results"][0]
        assert persisted_record["_source"] == "shop.example"
    else:
        persisted_record = artifact["results"]["shop.example"]["data"][0]
        assert artifact["results"]["shop.example"]["debug"]["session_name"] == (
            "<redacted>"
        )

    assert persisted_record == {
        "key": "product-key",
        "session_name": "morning",
        "api_token": "public-token",
        "product_url": "https://shop.example/item?color=red&page=2#reviews",
        "deployment_secret": "Bearer <redacted>",
        "encoded_secret": "<redacted>",
        "plus_encoded_secret": "<redacted>",
        **({"_source": "shop.example"} if group_by == GroupBy.merge else {}),
    }
    assert artifact["targets"][0]["url"] == (
        "https://shop.example/items?cursor=<redacted>#<redacted>"
    )
    assert artifact["targets"][0]["debug"]["session_name"] == "<redacted>"


def test_format_output_groups_results_by_domain_without_mutating_group_items():
    config = MagicMock(project="test", name="job")
    config.output.group_by = "target"
    first_item = {"sku": "a1"}
    second_item = {"sku": "b1"}
    results = [
        TargetResult(url="https://a.example/products", status="success", data=[first_item], errors=[]),
        TargetResult(url="https://b.example/products", status="failed", data=[second_item], errors=["boom"]),
    ]

    payload = _format_output(
        config,
        results,
        "job-1",
        JobStatus.partial,
        ["boom"],
    )

    assert payload["results"] == {
        "a.example": {
            "status": "success",
            "count": 1,
            "observed_count": 1,
            "data": [first_item],
            "debug": None,
            "error_type": None,
            "error_detail": None,
        },
        "b.example": {
            "status": "failed",
            "count": 1,
            "observed_count": 1,
            "data": [second_item],
            "debug": None,
            "error_type": None,
            "error_detail": None,
        },
    }
    assert first_item == {"sku": "a1"}
    assert second_item == {"sku": "b1"}


def test_format_output_keeps_same_domain_targets_separate():
    config = MagicMock(project="test", name="job")
    config.output.group_by = "target"
    results = [
        TargetResult(url="https://shop.example/products/a", status="success", data=[{"sku": "a"}]),
        TargetResult(url="https://shop.example/products/b", status="success", data=[{"sku": "b"}]),
    ]

    payload = _format_output(
        config,
        results,
        "job-1",
        JobStatus.complete,
        [],
    )

    assert payload["results"]["shop.example"]["data"] == [{"sku": "a"}]
    assert payload["results"]["shop.example#2"]["data"] == [{"sku": "b"}]


def test_format_output_redacts_url_userinfo_from_metadata_and_debug():
    config = MagicMock(project="test", name="job")
    config.output.group_by = GroupBy.merge
    result = TargetResult(
        url="https://user:pass@example.com:8443/products",
        status="failed",
        data=[{"sku": "a1"}],
        errors=["failed at https://user:pass@example.com/private"],
        error_detail="redirected to https://user:pass@example.com/private",
        debug={
            "final_url": "https://user:pass@example.com/private",
            "headers": {
                "Authorization": "Bearer secret",
                "X-Shop-Auth": "opaque-secret",
            },
            "screenshot_path": "/data/artifacts/job/run/dynamic-main.png",
        },
    )

    payload = _format_output(
        config,
        [result],
        "job-1",
        JobStatus.failed,
        result.errors,
    )

    assert payload["targets"][0]["url"] == "https://example.com:8443/products"
    assert payload["targets"][0]["error_detail"] == "redirected to https://example.com/private"
    assert payload["targets"][0]["errors"] == ["failed at https://example.com/private"]
    assert payload["targets"][0]["debug"]["final_url"] == "https://example.com/private"
    assert payload["targets"][0]["debug"]["headers"]["Authorization"] == "<redacted>"
    assert payload["targets"][0]["debug"]["headers"]["X-Shop-Auth"] == "<redacted>"
    assert payload["targets"][0]["debug"]["screenshot_path"] is None
    assert payload["results"] == [{"sku": "a1", "_source": "example.com:8443"}]


def test_format_output_redacts_sensitive_url_query_values_from_metadata_and_debug():
    config = MagicMock(project="test", name="job")
    config.output.group_by = GroupBy.merge
    result = TargetResult(
        url="https://example.com/products?api_key=secret&page=2",
        status="failed",
        data=[],
        errors=["failed at https://example.com/private?access_token=secret"],
        error_detail="redirected to https://example.com/private?session_id=secret",
        debug={
            "final_url": "https://example.com/private?token=secret&page=2",
            "request_failures": [
                {"url": "https://example.com/api?signature=secret", "error_text": "blocked"}
            ],
        },
    )

    payload = _format_output(
        config,
        [result],
        "job-1",
        JobStatus.failed,
        result.errors,
    )

    assert payload["targets"][0]["url"] == (
        "https://example.com/products?api_key=<redacted>&page=<redacted>"
    )
    assert payload["targets"][0]["error_detail"] == (
        "redirected to https://example.com/private?session_id=<redacted>"
    )
    assert payload["targets"][0]["errors"] == [
        "failed at https://example.com/private?access_token=<redacted>"
    ]
    assert payload["targets"][0]["debug"]["final_url"] == (
        "https://example.com/private?token=<redacted>&page=<redacted>"
    )
    assert payload["targets"][0]["debug"]["request_failures"][0]["url"] == (
        "https://example.com/api?signature=<redacted>"
    )
