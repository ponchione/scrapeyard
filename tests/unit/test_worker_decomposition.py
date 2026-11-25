from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.json_encoding import compact_json_size
from scrapeyard.config.schema import (
    ExecutionConfig,
    FailStrategy,
    FetcherType,
    GroupBy,
    OnEmptyAction,
    ScrapeConfig,
)
from scrapeyard.engine.browser_pool import BrowserPool, current_browser_pool
from scrapeyard.engine.scraper import TargetResult, TargetStatus
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.target_execution import resolve_target_runtime_context
from scrapeyard.queue.worker import scrape_task
from scrapeyard.runtime.metrics import TARGETS
from tests.factories import make_budget
from tests.unit.worker_helpers import (
    make_config_mock,
    make_job,
    make_settings_mock,
    run_scrape_task,
)


def _concurrent_target(url: str, fetcher: FetcherType) -> MagicMock:
    return MagicMock(url=url, fetcher=fetcher, proxy=None)


def _config_for_targets(
    targets: list[MagicMock],
    *,
    concurrency: int | None = None,
    group_by: GroupBy | str = GroupBy.target,
    fail_strategy: FailStrategy = FailStrategy.partial,
    reuse_browser: bool = False,
) -> MagicMock:
    cfg = make_config_mock(
        targets=targets,
        fail_strategy=fail_strategy,
        group_by=group_by,
        on_empty=OnEmptyAction.warn,
    )
    cfg.execution.concurrency = concurrency or len(targets)
    cfg.execution.reuse_browser = reuse_browser
    return cfg


@pytest.mark.asyncio
async def test_browser_limiter_caps_multiple_targets_in_one_job():
    limiter = BrowserExecutionLimiter(2)
    targets = [
        _concurrent_target(f"https://browser-{index}.example", FetcherType.dynamic)
        for index in range(4)
    ]
    cfg = _config_for_targets(targets)
    release = asyncio.Event()
    first_pair_started = asyncio.Event()
    active_fetches = 0
    max_active_fetches = 0
    total_started = 0
    called = 0

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        nonlocal active_fetches, max_active_fetches, total_started, called
        called += 1
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

    task = asyncio.create_task(
        run_scrape_task(cfg, _scrape, job_id="one-job", browser_limiter=limiter)
    )
    await asyncio.wait_for(first_pair_started.wait(), timeout=1)

    assert limiter.active == 2
    assert called == 2

    release.set()
    outcome = await asyncio.wait_for(task, timeout=1)

    assert len(outcome.saved_artifact["targets"]) == 4
    assert max_active_fetches == 2
    assert limiter.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
async def test_reuse_browser_lends_the_run_one_pool_and_closes_it_after_the_targets(reuse):
    targets = [
        _concurrent_target(f"https://www.example.test/c/{index}", FetcherType.dynamic)
        for index in range(3)
    ]
    cfg = _config_for_targets(targets, concurrency=1, reuse_browser=reuse)
    pools = []

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        pools.append(current_browser_pool())
        return TargetResult(url=target.url, status=TargetStatus.success)

    with patch.object(BrowserPool, "aclose", autospec=True) as aclose:
        await run_scrape_task(
            cfg, _scrape, job_id="reuse", browser_limiter=BrowserExecutionLimiter(1)
        )

    assert len(pools) == 3 and len({id(pool) for pool in pools}) == 1
    assert (pools[0] is not None) is reuse
    assert aclose.call_count == int(reuse)
    assert current_browser_pool() is None


@pytest.mark.asyncio
async def test_same_domain_targets_keep_distinct_screenshot_artifacts(tmp_path):
    limiter = BrowserExecutionLimiter(2)
    targets = [
        _concurrent_target(f"https://shop.example/products/{index}", FetcherType.dynamic)
        for index in range(2)
    ]
    cfg = _config_for_targets(targets)
    captured_paths: list[Path] = []

    async def _scrape(target, *_args, **kwargs) -> TargetResult:
        artifacts_dir = Path(kwargs["artifacts_dir"])
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        screenshot = artifacts_dir / "dynamic-main.png"
        screenshot.write_bytes(target.url.encode())
        captured_paths.append(screenshot)
        return TargetResult(
            url=target.url,
            status=TargetStatus.success,
            data=[{"url": target.url}],
            debug={"screenshot_path": str(screenshot)},
        )

    settings = make_settings_mock(
        browser_debug_enabled=True,
        storage_results_dir=str(tmp_path / "results"),
    )
    await run_scrape_task(
        cfg,
        _scrape,
        job_id="same-domain",
        settings=settings,
        browser_limiter=limiter,
    )

    assert len(set(captured_paths)) == 2
    assert {path.parent.name for path in captured_paths} == {"attempt-1"}
    assert {path.parent.parent.name for path in captured_paths} == {
        "target-0001",
        "target-0002",
    }
    assert sorted(path.read_bytes().decode() for path in captured_paths) == sorted(
        target.url for target in targets
    )


@pytest.mark.asyncio
async def test_delay_between_paces_actual_target_starts_after_semaphore_waits():
    limiter = BrowserExecutionLimiter(2)
    targets = [
        _concurrent_target(f"https://target-{index}.example", FetcherType.basic)
        for index in range(4)
    ]
    cfg = _config_for_targets(targets, concurrency=2)
    cfg.execution.delay_between = 0.02
    clock = 100.0
    started: list[float] = []
    first_wave_started = 0
    release_first_wave = asyncio.Event()

    async def _paced_sleep(delay_seconds: float) -> None:
        nonlocal clock
        clock += delay_seconds
        await asyncio.sleep(0)

    budget = make_budget(max_duration_seconds=60)
    budget.clock = lambda: clock
    budget.sleep = AsyncMock(side_effect=_paced_sleep)

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        nonlocal first_wave_started
        started.append(clock)
        index = int(target.url.split("-")[1].split(".")[0])
        if index < 2:
            first_wave_started += 1
            if first_wave_started == 2:
                release_first_wave.set()
            await release_first_wave.wait()
        return TargetResult(url=target.url, status=TargetStatus.success)

    fake_time = SimpleNamespace(monotonic=lambda: clock)
    with (
        patch.object(RunBudget, "from_settings", return_value=budget),
        patch("scrapeyard.queue.worker.time", fake_time),
    ):
        outcome = await run_scrape_task(
            cfg, _scrape, job_id="paced", browser_limiter=limiter
        )

    assert len(outcome.saved_artifact["targets"]) == 4
    assert [
        later - earlier for earlier, later in zip(started, started[1:], strict=False)
    ] == pytest.approx([0.02, 0.02, 0.02])
    assert [call.args[0] for call in budget.sleep.await_args_list] == pytest.approx(
        [0.02, 0.02, 0.02]
    )


@pytest.mark.asyncio
async def test_post_target_delay_keeps_sequential_permit_until_completion_gap():
    targets = [_concurrent_target(f"https://shop.example/{i}", FetcherType.basic) for i in range(3)]
    cfg = _config_for_targets(targets, concurrency=1)
    cfg.execution.post_target_delay = 15
    events: list[object] = []

    async def scrape(target, *_args, **_kwargs):
        events.append(target.url)
        await asyncio.sleep(0)
        events.append("finished")
        return TargetResult(url=target.url, status=TargetStatus.success)

    async def sleep(seconds):
        events.append(seconds)
        await asyncio.sleep(0)

    budget = make_budget(max_duration_seconds=60)
    budget.sleep = AsyncMock(side_effect=sleep)
    with patch.object(RunBudget, "from_settings", return_value=budget):
        outcome = await run_scrape_task(
            cfg, scrape, job_id="post-delay", browser_limiter=BrowserExecutionLimiter(1)
        )
    assert len(outcome.saved_artifact["targets"]) == 3
    assert events == [
        targets[0].url,
        "finished",
        15,
        targets[1].url,
        "finished",
        15,
        targets[2].url,
        "finished",
    ]


def test_post_target_delay_schema_requires_sequential_execution():
    assert ExecutionConfig(concurrency=1, post_target_delay=15).post_target_delay == 15
    for invalid in (
        {"concurrency": 2, "post_target_delay": 15},
        {"concurrency": 1, "post_target_delay": -1},
    ):
        with pytest.raises(ValueError):
            ExecutionConfig(**invalid)


@pytest.mark.asyncio
async def test_post_target_delay_cannot_outlive_run_budget_or_start_next_target():
    targets = [_concurrent_target(f"https://shop.example/{i}", FetcherType.basic) for i in range(2)]
    # A frozen budget clock: the deadline cannot pass before the first target
    # on a slow runner, and the post-target sleep still times out in real time.
    budget = RunBudget.from_settings(
        SimpleNamespace(run_max_duration_seconds=0.03), clock=lambda: 0.0
    )
    cfg = _config_for_targets(targets, concurrency=1)
    cfg.execution.post_target_delay = 15
    limiter = BrowserExecutionLimiter(1)
    calls = 0

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        nonlocal calls
        calls += 1
        return TargetResult(url=target.url, status=TargetStatus.success)

    with patch.object(RunBudget, "from_settings", return_value=budget):
        outcome = await run_scrape_task(
            cfg, _scrape, job_id="post-delay-budget", browser_limiter=limiter
        )

    assert calls == 1
    assert limiter.active == 0
    assert outcome.final_status is JobStatus.failed
    assert outcome.saved_artifact["budget_error"]["limit_name"] == "run_duration_seconds"


@pytest.mark.asyncio
async def test_browser_limiter_is_shared_across_multiple_jobs():
    limiter = BrowserExecutionLimiter(2)
    configs = [
        _config_for_targets(
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

    async def _run_job(index: int):
        job = make_job(job_id=f"job-{index}", current_run_id=f"run-{index}")
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.claim_run.return_value = True
        job_store.run_is_active.return_value = True
        result_store = AsyncMock()
        result_store.save_result.return_value = SimpleNamespace(
            run_id=f"run-{index}", file_path="/tmp/r.json", record_count=0, serialized_bytes=64
        )
        error_store = AsyncMock()
        error_store.count_errors_for_run.return_value = 0
        await scrape_task(
            job.job_id,
            f"project: test\nname: job-{index}\n",
            run_id=f"run-{index}",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=MagicMock(check=MagicMock(return_value=None)),
            rate_limiter=AsyncMock(),
            browser_limiter=limiter,
        )
        return result_store.save_result.await_args.args[1]["targets"]

    with (
        patch("scrapeyard.queue.worker.load_config", side_effect=configs),
        patch("scrapeyard.queue.worker.get_settings", return_value=make_settings_mock()),
        patch("scrapeyard.queue.worker.scrape_target", side_effect=_scrape),
    ):
        tasks = [asyncio.create_task(_run_job(index)) for index in range(2)]
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
    cfg = _config_for_targets([dynamic, basic])
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

    task = asyncio.create_task(
        run_scrape_task(cfg, _scrape, job_id="mixed", browser_limiter=limiter)
    )
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
    cfg = _config_for_targets([target])

    async def _raise(*_args, **_kwargs) -> TargetResult:
        raise RuntimeError("browser crashed")

    outcome = await run_scrape_task(cfg, _raise, job_id="failure", browser_limiter=limiter)

    assert outcome.saved_artifact["targets"][0]["status"] == "failed"
    assert outcome.saved_artifact["targets"][0]["error_detail"] == "RuntimeError: browser crashed"
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_target_metric_classifies_pre_result_exception_as_failed():
    limiter = BrowserExecutionLimiter(1)
    target = _concurrent_target("https://failing.example", FetcherType.basic)
    cfg = _config_for_targets([target])
    circuit_breaker = MagicMock()
    circuit_breaker.check.side_effect = RuntimeError("failed before result")
    before = TARGETS.labels("failed", "basic")._value.get()

    outcome = await run_scrape_task(
        cfg,
        [],
        job_id="failure-metric",
        circuit_breaker=circuit_breaker,
        browser_limiter=limiter,
    )

    assert TARGETS.labels("failed", "basic")._value.get() == before + 1
    outcome.job_store.fail_owned_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_permit_recovers_after_target_cancellation():
    limiter = BrowserExecutionLimiter(1)
    target = _concurrent_target("https://cancelled.example", FetcherType.stealthy)
    cfg = _config_for_targets([target])
    browser_started = asyncio.Event()

    async def _block(*_args, **_kwargs) -> TargetResult:
        browser_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        run_scrape_task(cfg, _block, job_id="cancelled", browser_limiter=limiter)
    )
    await asyncio.wait_for(browser_started.wait(), timeout=1)
    assert limiter.active == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.active == 0


@pytest.mark.asyncio
async def test_collect_result_payload_flattens_data_and_errors_in_order():
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
    cfg = _config_for_targets(
        [_concurrent_target(r.url, FetcherType.basic) for r in results],
        concurrency=1,
    )

    outcome = await run_scrape_task(cfg, results)

    assert outcome.saved_artifact["errors"] == ["warn-a", "err-b1", "err-b2"]
    assert outcome.job_store.finalize_owned_run.await_args.args[3] == 2


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
    assert context.artifacts_dir == ("/tmp/artifacts/shop.example/target-0001/attempt-1")


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
    assert context.artifacts_dir == ("/tmp/artifacts/shop.example:8443/target-0001/attempt-1")


@pytest.mark.parametrize(
    "url",
    [
        "https://BÜCHER.example./products",
        "https://xn--bcher-kva.example:443/products",
    ],
)
def test_resolve_target_runtime_context_uses_canonical_protection_domain(url):
    target_cfg = MagicMock(url=url, proxy=None)
    config = MagicMock(adaptive=False, schedule=None, proxy=None)
    settings = MagicMock(proxy_url="")

    context = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir="/tmp/artifacts",
    )

    assert context.domain == "xn--bcher-kva.example"
    assert context.artifacts_dir == ("/tmp/artifacts/xn--bcher-kva.example/target-0001/attempt-1")


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


@pytest.mark.asyncio
async def test_format_output_merges_results_with_source_domains_and_target_metadata():
    results = [
        TargetResult(
            url="https://a.example/products",
            status="success",
            data=[{"sku": "a1"}],
            errors=[],
            pages_scraped=1,
        ),
        TargetResult(
            url="https://b.example/products",
            status="failed",
            data=["raw-item"],
            errors=["boom"],
            pages_scraped=1,
        ),
    ]
    config = _config_for_targets(
        [_concurrent_target(r.url, FetcherType.basic) for r in results],
        concurrency=1,
        group_by=GroupBy.merge,
    )

    outcome = await run_scrape_task(config, results, job_id="job-1")
    payload = outcome.saved_artifact

    assert payload["status"] == "partial"
    assert payload["targets"] == [
        {
            "url": "https://a.example/products",
            "status": "success",
            "count": 1,
            "observed_count": 1,
            "pagination": {"stop_reason": "unknown", "exhausted": False},
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
            "pagination": {"stop_reason": "unknown", "exhausted": False},
            "pages_scraped": 1,
            "error_type": None,
            "error_detail": None,
            "errors": ["boom"],
            "debug": None,
        },
    ]
    assert payload["results"] == [{"sku": "a1", "_source": "a.example"}, "raw-item"]
    assert results[0].data == [{"sku": "a1"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", [GroupBy.target, GroupBy.merge])
async def test_materialized_output_accounts_every_persisted_component(group_by):
    config = ScrapeConfig.model_validate(
        {
            "project": "test",
            "name": "job",
            "output": {"group_by": group_by.value},
            "target": {
                "url": "https://example.com/products",
                "selectors": {"sku": ".sku"},
            },
        }
    )
    record = {
        "sku": "a1",
        "pricing_visibility": "unknown",
        "display_price_text": None,
        "stock_status": "unknown",
    }
    result = TargetResult(
        url="https://example.com/products",
        status="success",
        data=[record],
        pages_scraped=1,
    )
    budget = make_budget(
        max_duration_seconds=60,
        max_fetched_bytes=1_000_000,
        max_extracted_records=100,
        max_serialized_result_bytes=1_000_000,
        max_browser_debug_bytes=1_000,
    )
    budget.reserve_estimated_result_bytes(compact_json_size(record) + 1)

    with patch.object(RunBudget, "from_settings", return_value=budget):
        outcome = await run_scrape_task(config, [result], job_id="job-1")
    payload = outcome.saved_artifact
    payload_materialized = {
        k: v for k, v in payload.items() if k not in {"run_budget", "run_id", "config_hash"}
    }

    assert budget.estimated_result_bytes >= compact_json_size(payload_materialized)
    if group_by is GroupBy.target:
        assert payload["results"]["example.com"]["data"] is result.data
    else:
        assert payload["results"][0]["_source"] == "example.com"


@pytest.mark.asyncio
async def test_format_output_rejects_reserved_source_field_in_merge_record():
    results = [
        TargetResult(
            url="https://shop.example/products",
            status="success",
            data=[{"_source": "catalog-feed", "sku": "a1"}],
        )
    ]
    config = _config_for_targets(
        [_concurrent_target("https://shop.example/products", FetcherType.basic)],
        group_by=GroupBy.merge,
    )

    outcome = await run_scrape_task(config, results, job_id="job-1")

    outcome.result_store.save_result.assert_not_awaited()
    outcome.job_store.fail_owned_run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", [GroupBy.merge, GroupBy.target])
async def test_persisted_results_preserve_public_data_and_redact_resolved_secrets(
    group_by: GroupBy,
):
    secret = "Vendor Value/91"
    target = _concurrent_target("https://shop.example/items", FetcherType.basic)
    config = _config_for_targets(
        [target],
        group_by=group_by,
        fail_strategy=FailStrategy.partial,
    )
    config.resolved_secret_values = (secret,)
    extracted = {
        "key": "product-key",
        "session_name": "morning",
        "api_token": "public-token",
        "product_url": ("https://shop.example/item?color=red&page=2#reviews"),
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

    outcome = await run_scrape_task(config, [result], job_id="job-1", run_id="run-1")
    artifact = outcome.saved_artifact

    if group_by == GroupBy.merge:
        persisted_record = artifact["results"][0]
        assert persisted_record["_source"] == "shop.example"
    else:
        persisted_record = artifact["results"]["shop.example"]["data"][0]
        assert artifact["results"]["shop.example"]["debug"]["session_name"] == "<redacted>"

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
    assert artifact["result_redaction"] == {
        "deployment_secret_matches": 3,
    }


@pytest.mark.asyncio
async def test_format_output_groups_results_by_domain_without_mutating_group_items():
    first_item = {"sku": "a1"}
    second_item = {"sku": "b1"}
    results = [
        TargetResult(
            url="https://a.example/products", status="success", data=[first_item], errors=[]
        ),
        TargetResult(
            url="https://b.example/products", status="failed", data=[second_item], errors=["boom"]
        ),
    ]
    config = _config_for_targets(
        [_concurrent_target(r.url, FetcherType.basic) for r in results],
        concurrency=1,
        group_by="target",
    )

    outcome = await run_scrape_task(config, results, job_id="job-1")
    payload = outcome.saved_artifact

    assert payload["results"] == {
        "a.example": {
            "status": "success",
            "count": 1,
            "observed_count": 1,
            "pagination": {"stop_reason": "unknown", "exhausted": False},
            "data": [first_item],
            "debug": None,
            "error_type": None,
            "error_detail": None,
        },
        "b.example": {
            "status": "failed",
            "count": 1,
            "observed_count": 1,
            "pagination": {"stop_reason": "unknown", "exhausted": False},
            "data": [second_item],
            "debug": None,
            "error_type": None,
            "error_detail": None,
        },
    }
    assert first_item == {"sku": "a1"}
    assert second_item == {"sku": "b1"}


@pytest.mark.asyncio
async def test_format_output_keeps_same_domain_targets_separate():
    results = [
        TargetResult(url="https://shop.example/products/a", status="success", data=[{"sku": "a"}]),
        TargetResult(url="https://shop.example/products/b", status="success", data=[{"sku": "b"}]),
    ]
    config = _config_for_targets(
        [_concurrent_target(r.url, FetcherType.basic) for r in results],
        concurrency=1,
        group_by="target",
    )

    outcome = await run_scrape_task(config, results, job_id="job-1")
    payload = outcome.saved_artifact

    assert payload["results"]["shop.example"]["data"] == [{"sku": "a"}]
    assert payload["results"]["shop.example#2"]["data"] == [{"sku": "b"}]


@pytest.mark.asyncio
async def test_format_output_uses_canonical_source_grouping():
    results = [
        TargetResult(
            url="https://BÜCHER.example.:443/products",
            status="success",
            data=[{"sku": "a"}],
        )
    ]
    config = _config_for_targets(
        [_concurrent_target("https://BÜCHER.example.:443/products", FetcherType.basic)],
        group_by=GroupBy.merge,
    )

    outcome = await run_scrape_task(config, results, job_id="job-1")
    payload = outcome.saved_artifact

    assert payload["results"] == [{"sku": "a", "_source": "xn--bcher-kva.example"}]


@pytest.mark.asyncio
async def test_format_output_redacts_url_userinfo_from_metadata_and_debug():
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
    config = _config_for_targets(
        [_concurrent_target(result.url, FetcherType.basic)],
        group_by=GroupBy.merge,
        fail_strategy=FailStrategy.continue_,
    )

    outcome = await run_scrape_task(config, [result], job_id="job-1")
    payload = outcome.saved_artifact

    assert payload["targets"][0]["url"] == "https://example.com:8443/products"
    assert payload["targets"][0]["error_detail"] == "redirected to https://example.com/private"
    assert payload["targets"][0]["errors"] == ["failed at https://example.com/private"]
    assert payload["targets"][0]["debug"]["final_url"] == "https://example.com/private"
    assert payload["targets"][0]["debug"]["headers"]["Authorization"] == "<redacted>"
    assert payload["targets"][0]["debug"]["headers"]["X-Shop-Auth"] == "<redacted>"
    assert payload["targets"][0]["debug"]["screenshot_path"] is None
    assert payload["results"] == [{"sku": "a1", "_source": "example.com:8443"}]


@pytest.mark.asyncio
async def test_format_output_redacts_sensitive_url_query_values_from_metadata_and_debug():
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
    config = _config_for_targets(
        [_concurrent_target(result.url, FetcherType.basic)],
        group_by=GroupBy.merge,
    )

    outcome = await run_scrape_task(config, [result], job_id="job-1")
    payload = outcome.saved_artifact

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


def _merge_config(urls: list[str], secrets: tuple[str, ...], group_by: GroupBy) -> ScrapeConfig:
    config = ScrapeConfig.model_validate(
        {
            "project": "test",
            "name": "job",
            "execution": {"concurrency": 1},
            "output": {"group_by": group_by.value},
            "targets": [{"url": url, "selectors": {"sku": ".sku"}} for url in urls],
        }
    )
    config._resolved_secret_values = secrets
    return config


@pytest.mark.asyncio
async def test_merged_record_sources_are_redacted_once_per_distinct_source():
    urls = ["https://shop.example/a", "https://plain.example/b", "https://shop.example/c"]

    async def _run_with_secret(secret: str) -> tuple[dict, int]:
        results = [
            TargetResult(
                url=url,
                status="success",
                data=[{"sku": f"{index}-{n}"} for n in range(20)],
            )
            for index, url in enumerate(urls)
        ]
        budget = make_budget(max_extracted_records=1_000, max_browser_debug_bytes=1_000)
        with patch.object(RunBudget, "from_settings", return_value=budget):
            outcome = await run_scrape_task(
                _merge_config(urls, (secret,), GroupBy.merge),
                results,
                job_id="job-1",
            )
        return outcome.saved_artifact, budget.estimated_result_bytes

    _unmatched, unmatched_estimate = await _run_with_secret("absent")
    payload, estimate = await _run_with_secret("shop")

    assert [item["_source"] for item in payload["results"]] == (
        ["<redacted>.example"] * 20 + ["plain.example"] * 20 + ["<redacted>.example"] * 20
    )
    assert payload["result_redaction"] == {"deployment_secret_matches": 40}
    growth = len("<redacted>.example") - len("shop.example")
    # 40 merged record _source fields plus the 2 target summary URLs grew by *growth*.
    assert estimate - unmatched_estimate == 42 * growth + compact_json_size(
        {"result_redaction": payload["result_redaction"]}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", [GroupBy.merge, GroupBy.target])
async def test_records_are_not_copied_for_redaction_without_secrets(group_by):
    config = _merge_config(["https://shop.example/a"], (), group_by)
    record = {"sku": "a1", "tags": ["x"]}
    result = TargetResult(url="https://shop.example/a", status="success", data=[record])
    budget = make_budget(max_extracted_records=1_000, max_browser_debug_bytes=1_000)
    budget.reserve_estimated_result_bytes(compact_json_size(record) + 1)

    with patch.object(RunBudget, "from_settings", return_value=budget):
        outcome = await run_scrape_task(config, [result], job_id="job-1")
    payload = outcome.saved_artifact

    assert result.data[0] is record
    if group_by is GroupBy.target:
        assert payload["results"]["shop.example"]["data"][0] is record
    else:
        assert payload["results"] == [{"sku": "a1", "tags": ["x"], "_source": "shop.example"}]
    assert "result_redaction" not in payload


@pytest.mark.asyncio
async def test_each_target_checks_run_ownership_once_before_its_fetch():
    targets = [
        _concurrent_target(f"https://t{index}.example", FetcherType.basic) for index in range(3)
    ]
    cfg = _config_for_targets(targets, concurrency=1)
    checkpoints: list[str] = []

    async def _scrape(target, *_args, **_kwargs) -> TargetResult:
        checkpoints.append(f"scrape:{target.url}")
        return TargetResult(url=target.url, status=TargetStatus.success)

    job_store = AsyncMock()
    job_store.get_job.return_value = make_job(job_id="checks", current_run_id="run-1")
    job_store.claim_run.return_value = True

    async def _check_active(_job_id: str, _run_id: str) -> bool:
        checkpoints.append("ownership_check")
        return True

    job_store.run_is_active.side_effect = _check_active
    await run_scrape_task(
        cfg,
        _scrape,
        job_id="checks",
        run_id="run-1",
        job_store=job_store,
        browser_limiter=BrowserExecutionLimiter(1),
    )

    assert checkpoints == [
        "ownership_check",
        "ownership_check",
        "scrape:https://t0.example",
        "ownership_check",
        "ownership_check",
        "scrape:https://t1.example",
        "ownership_check",
        "ownership_check",
        "scrape:https://t2.example",
        "ownership_check",
        "ownership_check",
        "ownership_check",
        "ownership_check",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "current_run_id"),
    [(JobStatus.complete, "run-1"), (JobStatus.running, "run-2")],
    ids=["terminal", "superseded"],
)
async def test_skipped_delivery_does_not_parse_its_config(monkeypatch, status, current_run_id):
    from scrapeyard.queue import worker

    job_store = AsyncMock()
    job_store.get_job.return_value = make_job(status=status, current_run_id=current_run_id)
    load_config = MagicMock(side_effect=AssertionError("config parsed"))
    monkeypatch.setattr(worker, "load_config", load_config)

    await scrape_task(
        "job-1",
        "unparsed: yaml",
        run_id="run-1",
        job_store=job_store,
        result_store=AsyncMock(),
        error_store=AsyncMock(),
        circuit_breaker=MagicMock(),
        rate_limiter=AsyncMock(),
    )

    load_config.assert_not_called()
    job_store.claim_run.assert_not_awaited()
