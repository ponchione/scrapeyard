from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.config.schema import BrowserActionConfig, RetryConfig
from scrapeyard.engine.browser_debug import run_browser_actions
from scrapeyard.engine.resilience import RetryHandler, RetryableError
from scrapeyard.queue.target_execution import guard_target_execution


def _budget(**overrides) -> RunBudget:
    values = {
        "max_duration_seconds": 60.0,
        "max_fetched_bytes": 100,
        "max_extracted_records": 10,
        "max_serialized_result_bytes": 4096,
        "max_browser_debug_bytes": 100,
    }
    values.update(overrides)
    return RunBudget(**values)


@pytest.mark.asyncio
async def test_record_budget_is_aggregate_and_concurrency_safe_at_exact_boundary():
    budget = _budget(max_extracted_records=6)

    await asyncio.gather(
        budget.consume_extracted_records(2),
        budget.consume_extracted_records(1),
        budget.consume_extracted_records(3),
    )

    assert budget.extracted_records == 6
    with pytest.raises(BudgetExceeded) as exc_info:
        await budget.consume_extracted_records(1)
    assert exc_info.value.limit_name is BudgetLimitName.extracted_records
    assert exc_info.value.configured_limit == 6
    assert exc_info.value.observed_amount == 7
    assert budget.extracted_records == 6


@pytest.mark.asyncio
async def test_fetched_byte_budget_allows_exact_boundary_and_rejects_one_over():
    budget = _budget(max_fetched_bytes=5)

    await budget.consume_fetched_bytes(2)
    await budget.consume_fetched_bytes(3)

    assert budget.fetched_bytes == 5
    with pytest.raises(BudgetExceeded) as exc_info:
        await budget.consume_fetched_bytes(1)
    assert exc_info.value.limit_name is BudgetLimitName.fetched_bytes
    assert exc_info.value.observed_amount == 6


@pytest.mark.asyncio
async def test_deadline_cancels_all_concurrent_children():
    budget = _budget(max_duration_seconds=0.01)
    cancelled: list[int] = []

    async def blocked(index: int) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(index)

    with pytest.raises(BudgetExceeded) as exc_info:
        await budget.wait_for(asyncio.gather(blocked(1), blocked(2)))

    await asyncio.sleep(0)
    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds
    assert sorted(cancelled) == [1, 2]


@pytest.mark.asyncio
async def test_deadline_does_not_wait_for_cancellation_resistant_child():
    budget = _budget(max_duration_seconds=0.01)
    release = asyncio.Event()
    finished = asyncio.Event()

    async def cancellation_resistant() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            finished.set()

    with pytest.raises(BudgetExceeded) as exc_info:
        await asyncio.wait_for(budget.wait_for(cancellation_resistant()), timeout=0.1)

    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds
    assert not finished.is_set()
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_already_expired_deadline_cancels_future_before_awaiting_it():
    now = [0.0]
    budget = RunBudget(
        max_duration_seconds=1,
        max_fetched_bytes=100,
        max_extracted_records=10,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=100,
        clock=lambda: now[0],
    )
    async def blocked() -> None:
        await asyncio.Event().wait()

    child = asyncio.create_task(blocked())
    future = asyncio.gather(child)
    now[0] = 2.0

    with pytest.raises(BudgetExceeded):
        await budget.wait_for(future)

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert future.done()
    assert child.cancelled()


@pytest.mark.asyncio
async def test_retry_backoff_uses_overall_deadline():
    budget = _budget(max_duration_seconds=0.01)
    retry = RetryHandler(
        RetryConfig(max_attempts=2, backoff="fixed", backoff_max=1),
        budget=budget,
    )

    async def retryable() -> None:
        raise RetryableError(503)

    with pytest.raises(BudgetExceeded) as exc_info:
        await retry.execute(retryable)
    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds


@pytest.mark.asyncio
async def test_rate_limit_wait_uses_overall_deadline():
    budget = _budget(max_duration_seconds=0.01)
    rate_limiter = AsyncMock()

    async def block_rate_limit(*_args) -> None:
        await asyncio.Event().wait()

    rate_limiter.acquire.side_effect = block_rate_limit
    runtime = MagicMock(domain="example.com")
    config = MagicMock()
    config.execution.domain_rate_limit = 5

    with pytest.raises(BudgetExceeded) as exc_info:
        await guard_target_execution(
            runtime=runtime,
            config=config,
            target_cfg=MagicMock(url="https://example.com"),
            circuit_breaker=MagicMock(),
            rate_limiter=rate_limiter,
            recorder=MagicMock(),
            budget=budget,
        )
    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds


@pytest.mark.asyncio
async def test_browser_action_wait_uses_overall_deadline():
    budget = _budget(max_duration_seconds=0.01)
    page = MagicMock()

    async def block_wait(*_args) -> None:
        await asyncio.Event().wait()

    page.wait_for_timeout = AsyncMock(side_effect=block_wait)
    action = BrowserActionConfig.model_validate({"type": "wait_ms", "wait_ms": 1000})

    with pytest.raises(BudgetExceeded) as exc_info:
        await run_browser_actions(page, [action], budget=budget)
    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds


@pytest.mark.asyncio
async def test_debug_budget_supports_partial_excerpt_but_not_partial_screenshot():
    budget = _budget(max_browser_debug_bytes=5)

    assert await budget.reserve_browser_debug_bytes(3, allow_partial=True) == 3
    assert await budget.reserve_browser_debug_bytes(4) == 0
    assert await budget.reserve_browser_debug_bytes(4, allow_partial=True) == 2
    assert budget.browser_debug_bytes == 5
