"""Tests for final job status resolution via scrape_task."""

from __future__ import annotations

import pytest

from scrapeyard.config.schema import FailStrategy
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import JobStatus
from tests.unit.worker_helpers import make_config_mock, make_target, run_scrape_task


async def _final_status_for(
    strategy: FailStrategy,
    results: list[TargetResult],
) -> tuple[JobStatus, dict]:
    cfg = make_config_mock(
        targets=[make_target(r.url) for r in results],
        fail_strategy=strategy,
    )
    outcome = await run_scrape_task(cfg, results)
    return outcome.final_status, outcome.saved_artifact


@pytest.mark.asyncio
async def test_all_or_nothing_fails_and_withholds_partial_results_without_mutating_inputs():
    records = [{"x": 1}]
    results = [
        TargetResult(url="http://a.com", status="success", data=list(records)),
        TargetResult(url="http://b.com", status="failed", errors=["err"]),
    ]
    status, artifact = await _final_status_for(FailStrategy.all_or_nothing, results)
    assert status == JobStatus.failed
    assert artifact["results"] == {}
    assert results[0].data == [{"x": 1}]


@pytest.mark.asyncio
async def test_all_or_nothing_complete_on_all_success():
    results = [
        TargetResult(url="http://a.com", status="success", data=[{"x": 1}]),
    ]
    status, _artifact = await _final_status_for(FailStrategy.all_or_nothing, results)
    assert status == JobStatus.complete


@pytest.mark.asyncio
async def test_partial_returns_partial_on_mixed():
    results = [
        TargetResult(url="http://a.com", status="success", data=[{"x": 1}]),
        TargetResult(url="http://b.com", status="failed", errors=["err"]),
    ]
    status, _artifact = await _final_status_for(FailStrategy.partial, results)
    assert status == JobStatus.partial


@pytest.mark.asyncio
async def test_partial_returns_failed_when_all_fail():
    results = [
        TargetResult(url="http://a.com", status="failed", errors=["err"]),
    ]
    status, _artifact = await _final_status_for(FailStrategy.partial, results)
    assert status == JobStatus.failed


@pytest.mark.asyncio
async def test_continue_completes_if_data_exists():
    results = [
        TargetResult(url="http://a.com", status="success", data=[{"x": 1}]),
        TargetResult(url="http://b.com", status="failed", errors=["err"]),
    ]
    status, _artifact = await _final_status_for(FailStrategy.continue_, results)
    assert status == JobStatus.complete


@pytest.mark.asyncio
async def test_continue_fails_if_no_data():
    results = [
        TargetResult(url="http://a.com", status="failed", errors=["err"]),
    ]
    status, _artifact = await _final_status_for(FailStrategy.continue_, results)
    assert status == JobStatus.failed
