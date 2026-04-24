"""Shared factory helpers for worker test files.

Consolidates _make_job, _make_target, _make_config_mock, and the SIMPLE_YAML
constant that were previously copy-pasted across 6+ worker test modules.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from scrapeyard.config.schema import FailStrategy
from scrapeyard.models.job import Job, JobStatus


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
    cfg.execution.delay_between = 0
    cfg.execution.domain_rate_limit = 0
    cfg.execution.fail_strategy = fail_strategy
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
    return cfg


SIMPLE_YAML = (
    "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1"
)
