"""Shared factory helpers for worker test files.

Consolidates _make_job, _make_target, _make_config_mock, and the SIMPLE_YAML
constant that were previously copy-pasted across 6+ worker test modules.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from scrapeyard.config.schema import FailStrategy, PageCacheMode
from scrapeyard.engine.scrape_models import TargetResult
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.worker import scrape_task
from scrapeyard.storage.types import SaveResultMeta


def make_job(
    job_id: str = "job-1",
    project: str = "test",
    name: str = "test-job",
    config_yaml: str = "",
    status: JobStatus = JobStatus.queued,
    current_run_id: str | None = None,
    **overrides: Any,
) -> Job:
    """Create a Job model for testing with sensible defaults."""
    return Job(
        job_id=job_id,
        project=project,
        name=name,
        config_yaml=config_yaml,
        status=status,
        current_run_id=current_run_id,
        **overrides,
    )


def make_target(url: str = "http://example.com", *, proxy: object = None) -> MagicMock:
    """Create a mock TargetConfig with the url and basic fetcher."""
    target = MagicMock(url=url, proxy=proxy)
    target.fetcher.value = "basic"
    return target


def make_config_mock(
    *,
    targets: list[MagicMock] | None = None,
    fail_strategy: FailStrategy = FailStrategy.partial,
    webhook=None,
    validation_overrides: dict | None = None,
    on_empty: str = "warn",
    group_by: str = "target",
) -> MagicMock:
    """Create a fully-wired config mock with standard defaults.

    Parameters
    ----------
    targets:
        Target mocks to return from resolved_targets(). Defaults to a
        single target at http://example.com.
    fail_strategy:
        The fail strategy enum value.
    webhook:
        Optional WebhookConfig to attach.
    validation_overrides:
        Dict of fields to set on the validation mock (required_fields,
        min_results, on_empty).
    on_empty:
        Default on_empty value when validation_overrides doesn't specify it.
    group_by:
        Output group_by value.
    """
    if targets is None:
        targets = [make_target()]

    cfg = MagicMock()
    cfg.project = "test"
    cfg.name = "test-job"
    cfg.resolved_targets.return_value = targets
    cfg.execution.concurrency = 1
    cfg.execution.deadline_at = None
    cfg.execution.delay_between = 0
    cfg.execution.post_target_delay = 0
    cfg.execution.domain_rate_limit = 0
    cfg.execution.fail_strategy = fail_strategy
    cfg.execution.page_cache = PageCacheMode.off
    cfg.execution.reuse_browser = False
    cfg.adaptive = False
    cfg.schedule = None
    cfg.retry = MagicMock()
    cfg.validation = MagicMock(
        required_fields=[],
        min_results=0,
        on_empty=on_empty,
    )
    if validation_overrides:
        for k, v in validation_overrides.items():
            setattr(cfg.validation, k, v)
    cfg.output.group_by = group_by
    cfg.webhook = webhook
    cfg.proxy = None
    cfg.resolved_secret_values = ()
    return cfg


def make_settings_mock(**overrides: Any) -> MagicMock:
    """Create service settings with concrete worker lease and budget values."""
    values: dict[str, Any] = {
        "adaptive_dir": "/tmp/adaptive",
        "storage_results_dir": "/tmp/results",
        "proxy_url": "",
        "browser_debug_enabled": False,
        "page_cache_dir": "",
        "domain_daily_page_limit": 0,
        "domain_denial_cooldown_seconds": 0,
        "workers_heartbeat_interval_seconds": 30,
        "workers_running_heartbeat_timeout_seconds": 600,
        "run_max_duration_seconds": 60,
        "run_max_fetched_bytes": 1_000_000,
        "run_max_extracted_records": 10_000,
        "run_max_serialized_result_bytes": 1_000_000,
        "run_max_browser_debug_bytes": 1_000_000,
        "run_max_requests": 10_000,
    }
    values.update(overrides)
    return MagicMock(**values)


def finalized_status(job_store: AsyncMock) -> JobStatus:
    """Return the terminal status passed to the ownership-aware finalizer."""
    return JobStatus(job_store.finalize_owned_run.await_args.args[2])


@dataclass
class ScrapeTaskOutcome:
    """Captured outputs from executing the public scrape_task entrypoint."""

    job_store: Any
    result_store: Any
    error_store: Any
    circuit_breaker: Any

    @property
    def final_status(self) -> JobStatus:
        return finalized_status(self.job_store)

    @property
    def saved_artifact(self) -> dict[str, Any]:
        return self.result_store.save_result.await_args.args[1]


async def run_scrape_task(
    config: Any,
    target_results: Sequence[TargetResult] | Callable[..., Awaitable[TargetResult]],
    *,
    job_id: str = "job-1",
    run_id: str | None = "run-1",
    job: Job | None = None,
    settings: Any = None,
    circuit_breaker: Any = None,
    rate_limiter: Any = None,
    browser_limiter: Any = None,
    domain_guard: Any = None,
    webhook_dispatcher: Any = None,
    job_store: Any = None,
    result_store: Any = None,
    error_store: Any = None,
    config_yaml: str = "",
) -> ScrapeTaskOutcome:
    """Execute public scrape_task() with lightweight stores and scripted target results."""
    resolved_yaml = config_yaml or SIMPLE_YAML
    resolved_job = job or make_job(
        job_id=job_id,
        project=getattr(config, "project", "test"),
        name=getattr(config, "name", "test-job"),
        config_yaml=resolved_yaml,
        current_run_id=run_id,
    )
    if job_store is None:
        job_store = AsyncMock()
        job_store.get_job.return_value = resolved_job
        job_store.queue_run.return_value = True
        job_store.claim_run.return_value = True
        job_store.run_is_active.return_value = True
    if result_store is None:
        result_store = AsyncMock()
        result_store.save_result.return_value = SaveResultMeta(
            run_id=run_id or "run-1",
            file_path=f"/tmp/results/{job_id}/{run_id or 'run-1'}.json",
            record_count=0,
            serialized_bytes=128,
        )
    if error_store is None:
        error_store = AsyncMock()
        error_store.count_errors_for_run.return_value = 0
    if circuit_breaker is None:
        circuit_breaker = MagicMock()
        circuit_breaker.check.return_value = None
    if rate_limiter is None:
        rate_limiter = AsyncMock()
    resolved_settings = settings or make_settings_mock()

    if callable(target_results):
        scrape_side_effect: Any = target_results
    else:
        iterator = iter(target_results)

        async def _next_result(*_args: Any, **_kwargs: Any) -> TargetResult:
            return next(iterator)

        scrape_side_effect = _next_result

    with (
        patch("scrapeyard.queue.worker.load_config", return_value=config),
        patch("scrapeyard.queue.worker.get_settings", return_value=resolved_settings),
        patch("scrapeyard.queue.worker.scrape_target", side_effect=scrape_side_effect),
    ):
        await scrape_task(
            job_id,
            resolved_yaml,
            run_id=run_id,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=rate_limiter,
            browser_limiter=browser_limiter,
            webhook_dispatcher=webhook_dispatcher,
            domain_guard=domain_guard,
        )
    return ScrapeTaskOutcome(
        job_store=job_store,
        result_store=result_store,
        error_store=error_store,
        circuit_breaker=circuit_breaker,
    )


SIMPLE_YAML = (
    "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1"
)
