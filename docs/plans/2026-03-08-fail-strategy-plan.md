# Fail Strategy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a `fail_strategy` field to `ExecutionConfig` that controls how the worker handles target failures: `partial` (default, current behavior), `all_or_nothing` (fail entire job on any failure), `continue` (keep going, log all failures).

**Architecture:** Add `FailStrategy` enum to `schema.py`, add field to `ExecutionConfig`, then modify the status determination logic in `worker.py` to branch on `config.execution.fail_strategy`.

**Tech Stack:** Pydantic, pytest, asyncio

---

### Task 1: Add FailStrategy enum and field to schema

**Files:**
- Modify: `src/scrapeyard/config/schema.py`

**Step 1: Add the enum**

Add after the `GroupBy` class (around line 76):

```python
class FailStrategy(str, Enum):
    """How to handle target failures within a job."""

    partial = "partial"
    all_or_nothing = "all_or_nothing"
    continue_ = "continue"
```

Note: `continue` is a Python keyword, so use `continue_` as the attribute name with `"continue"` as the value.

**Step 2: Add field to ExecutionConfig**

Add to `ExecutionConfig` class after the `priority` field:

```python
    fail_strategy: FailStrategy = Field(
        default=FailStrategy.partial, description="How to handle target failures"
    )
```

**Step 3: Run ruff**

Run: `source .venv/bin/activate && ruff check src/scrapeyard/config/schema.py`
Expected: All checks passed

**Step 4: Commit**

```bash
git add src/scrapeyard/config/schema.py
git commit -m "feat: add FailStrategy enum and field to ExecutionConfig"
```

---

### Task 2: Write tests for fail_strategy behavior

**Files:**
- Create: `tests/unit/test_worker_fail_strategy.py`

**Step 1: Write the test file**

Create `tests/unit/test_worker_fail_strategy.py`:

```python
"""Test fail_strategy behavior in scrape_task."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import FailStrategy
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.worker import scrape_task


def _make_job(job_id: str = "job-1") -> Job:
    return Job(
        id=job_id,
        project="test",
        name="test-job",
        config_yaml="",
        status=JobStatus.queued,
    )


def _make_config_yaml(fail_strategy: str = "partial") -> str:
    return f"""
project: test
name: test-job
target: http://example.com
selectors:
  title: "h1"
execution:
  fail_strategy: {fail_strategy}
"""


@pytest.fixture
def mock_stores():
    job_store = AsyncMock()
    result_store = AsyncMock()
    error_store = AsyncMock()
    circuit_breaker = MagicMock()
    circuit_breaker.check = MagicMock()
    circuit_breaker.record_success = MagicMock()
    circuit_breaker.record_failure = MagicMock()
    return job_store, result_store, error_store, circuit_breaker


@pytest.mark.asyncio
async def test_partial_returns_partial_on_mixed(mock_stores):
    """partial: mixed success/failure yields JobStatus.partial."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    config_yaml = _make_config_yaml("partial")

    # Two targets: one succeeds, one fails.
    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape:
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [MagicMock(url="http://a.com"), MagicMock(url="http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.partial
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.format = "json"
        cfg.output.group_by = "target"

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", config_yaml,
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
        )

    # Check final status was partial.
    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.partial


@pytest.mark.asyncio
async def test_all_or_nothing_fails_on_any_failure(mock_stores):
    """all_or_nothing: any failure yields JobStatus.failed, no results saved."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    config_yaml = _make_config_yaml("all_or_nothing")

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape:
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [MagicMock(url="http://a.com"), MagicMock(url="http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.all_or_nothing
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.format = "json"
        cfg.output.group_by = "target"

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", config_yaml,
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
        )

    # Should be failed, not partial.
    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.failed
    # No results should be saved.
    result_store.save_result.assert_not_called()


@pytest.mark.asyncio
async def test_continue_completes_even_with_failures(mock_stores):
    """continue: all failures still yields JobStatus.complete if any data exists."""
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job = _make_job()
    job_store.get_job = AsyncMock(return_value=job)
    job_store.update_job = AsyncMock()

    config_yaml = _make_config_yaml("continue")

    success_result = TargetResult(url="http://a.com", status="success", data=[{"title": "A"}])
    fail_result = TargetResult(url="http://b.com", status="failed", errors=["timeout"])

    with patch("scrapeyard.queue.worker.load_config") as mock_load, \
         patch("scrapeyard.queue.worker.scrape_target") as mock_scrape:
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [MagicMock(url="http://a.com"), MagicMock(url="http://b.com")]
        cfg.execution.concurrency = 1
        cfg.execution.delay_between = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.continue_
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.format = "json"
        cfg.output.group_by = "target"

        mock_scrape.side_effect = [success_result, fail_result]

        await scrape_task(
            "job-1", config_yaml,
            job_store=job_store, result_store=result_store,
            error_store=error_store, circuit_breaker=circuit_breaker,
        )

    # Should be complete despite failures.
    final_update = job_store.update_job.call_args_list[-1][0][0]
    assert final_update.status == JobStatus.complete
    # Results should still be saved.
    result_store.save_result.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/unit/test_worker_fail_strategy.py -v`
Expected: FAIL — tests fail because worker doesn't respect fail_strategy yet

**Step 3: Commit**

```bash
git add tests/unit/test_worker_fail_strategy.py
git commit -m "test: add tests for fail_strategy behavior in worker"
```

---

### Task 3: Implement fail_strategy logic in worker.py

**Files:**
- Modify: `src/scrapeyard/queue/worker.py:125-139`

**Step 1: Import FailStrategy**

Add to imports in worker.py:

```python
from scrapeyard.config.schema import FailStrategy, OutputFormat
```

(Replace the existing `from scrapeyard.config.schema import OutputFormat` line.)

**Step 2: Replace the status determination block**

Replace lines 132-139 (the "Determine final status" block) with:

```python
    # Determine final status based on fail_strategy.
    failed_count = sum(1 for r in all_results if r.status == "failed")
    fail_strategy = config.execution.fail_strategy

    if fail_strategy == FailStrategy.all_or_nothing:
        if failed_count > 0:
            final_status = JobStatus.failed
            flat_data.clear()  # Discard all results.
        else:
            final_status = JobStatus.complete
    elif fail_strategy == FailStrategy.continue_:
        if flat_data:
            final_status = JobStatus.complete
        else:
            final_status = JobStatus.failed
    else:
        # FailStrategy.partial (default / current behavior).
        if failed_count == len(all_results):
            final_status = JobStatus.failed
        elif failed_count > 0 or not validation.passed:
            final_status = JobStatus.partial
        else:
            final_status = JobStatus.complete
```

**Step 3: Run tests**

Run: `source .venv/bin/activate && pytest tests/unit/test_worker_fail_strategy.py -v`
Expected: All 3 PASS

**Step 4: Run ruff**

Run: `source .venv/bin/activate && ruff check src/scrapeyard/config/ src/scrapeyard/queue/`
Expected: All checks passed

**Step 5: Commit**

```bash
git add src/scrapeyard/queue/worker.py
git commit -m "feat: implement fail_strategy logic in worker scrape_task"
```

---

### Task 4: Lint and final verification

**Files:** None (verification only)

**Step 1: Run ruff**

Run: `source .venv/bin/activate && ruff check src/scrapeyard/config/ src/scrapeyard/queue/`
Expected: All checks passed

**Step 2: Run full test suite**

Run: `source .venv/bin/activate && pytest tests/ -v`
Expected: All PASS

**Step 3: Commit if any fixes were needed**

```bash
git add -u
git commit -m "chore: lint fixes for fail_strategy"
```
