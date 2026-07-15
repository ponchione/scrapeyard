"""Bounded-cardinality Prometheus metrics for Scrapeyard runtime operations."""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
)

from scrapeyard.storage.types import CleanupBacklogSnapshot


REGISTRY = CollectorRegistry(auto_describe=True)
ProcessCollector(registry=REGISTRY)
_CLEANUP_BACKLOG_CATEGORIES = (
    "expired_results",
    "excess_results",
    "adhoc_jobs",
    "scheduled_runs",
    "expired_errors",
    "idempotency_records",
    "terminal_webhooks",
)

API_REQUESTS = Counter(
    "scrapeyard_api_requests_total",
    "HTTP requests completed by method, route template, and status class.",
    ("method", "route", "status_class"),
    registry=REGISTRY,
)
API_DURATION = Histogram(
    "scrapeyard_api_request_duration_seconds",
    "HTTP request duration by method and route template.",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15),
    registry=REGISTRY,
)
QUEUE_DEPTH = Gauge(
    "scrapeyard_queue_depth",
    "Waiting and deferred queue members by fixed priority.",
    ("priority",),
    registry=REGISTRY,
)
QUEUE_OLDEST_AGE = Gauge(
    "scrapeyard_queue_oldest_age_seconds",
    "Age of the oldest waiting queue member by fixed priority.",
    ("priority",),
    registry=REGISTRY,
)
ACTIVE_WORK = Gauge(
    "scrapeyard_active_work",
    "Current process work by bounded work type.",
    ("kind",),
    registry=REGISTRY,
)
WORK_CAPACITY = Gauge(
    "scrapeyard_work_capacity",
    "Configured process capacity by bounded work type.",
    ("kind",),
    registry=REGISTRY,
)
RUNS = Counter(
    "scrapeyard_runs_total",
    "Completed scrape runs by terminal outcome and trigger.",
    ("status", "trigger"),
    registry=REGISTRY,
)
RUN_DURATION = Histogram(
    "scrapeyard_run_duration_seconds",
    "Scrape run duration by terminal outcome.",
    ("status",),
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200),
    registry=REGISTRY,
)
TARGETS = Counter(
    "scrapeyard_targets_total",
    "Completed target executions by outcome and fetcher type.",
    ("status", "fetcher"),
    registry=REGISTRY,
)
TARGET_DURATION = Histogram(
    "scrapeyard_target_duration_seconds",
    "Target execution duration by outcome and fetcher type.",
    ("status", "fetcher"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)
OUTPUT_RECORDS = Counter(
    "scrapeyard_output_records_total",
    "Extracted records accepted for durable results.",
    registry=REGISTRY,
)
OUTPUT_BYTES = Counter(
    "scrapeyard_output_bytes_total",
    "Serialized durable result bytes written.",
    registry=REGISTRY,
)
RETRIES = Counter(
    "scrapeyard_retries_total",
    "Retry operations by bounded subsystem and outcome.",
    ("subsystem", "outcome"),
    registry=REGISTRY,
)
RATE_LIMIT_WAIT = Histogram(
    "scrapeyard_rate_limit_wait_seconds",
    "Time spent waiting for request throttles by bounded scope.",
    ("scope",),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 15, 60),
    registry=REGISTRY,
)
RATE_LIMIT_STATE_EVENTS = Counter(
    "scrapeyard_rate_limit_state_events_total",
    "Bounded rate-limit map saturation and eviction events.",
    ("scope", "event"),
    registry=REGISTRY,
)
WEBHOOK_DELIVERIES = Counter(
    "scrapeyard_webhook_attempts_total",
    "Webhook HTTP attempts by persisted outcome.",
    ("outcome",),
    registry=REGISTRY,
)
WEBHOOK_BACKLOG = Gauge(
    "scrapeyard_webhook_deliveries",
    "Durable webhook rows by status.",
    ("status",),
    registry=REGISTRY,
)
WEBHOOK_OLDEST_AGE = Gauge(
    "scrapeyard_webhook_oldest_pending_age_seconds",
    "Age of the oldest pending durable webhook.",
    registry=REGISTRY,
)
BACKGROUND_TASK = Gauge(
    "scrapeyard_background_task_up",
    "Whether a required process-local background subsystem is running.",
    ("task",),
    registry=REGISTRY,
)
LAST_SUCCESS = Gauge(
    "scrapeyard_background_last_success_timestamp_seconds",
    "Unix timestamp of the most recent successful subsystem cycle.",
    ("task",),
    registry=REGISTRY,
)
CLEANUP_RUNS = Counter(
    "scrapeyard_cleanup_runs_total",
    "Cleanup loop passes by outcome.",
    ("status",),
    registry=REGISTRY,
)
CLEANUP_ITEMS = Counter(
    "scrapeyard_cleanup_items_total",
    "Items processed by cleanup action.",
    ("action",),
    registry=REGISTRY,
)
CLEANUP_BYTES = Counter(
    "scrapeyard_cleanup_bytes_total",
    "Artifact bytes removed by cleanup reconciliation.",
    registry=REGISTRY,
)
CLEANUP_ARTIFACT_FINDINGS = Counter(
    "scrapeyard_cleanup_artifact_findings_total",
    "Metadata-backed result artifacts that failed validation by bounded kind.",
    ("kind",),
    registry=REGISTRY,
)
CLEANUP_HISTORY_FAILURES = Counter(
    "scrapeyard_cleanup_history_failures_total",
    "Durable history cleanup failures by bounded phase.",
    ("phase",),
    registry=REGISTRY,
)
CLEANUP_ELIGIBLE_ITEMS = Gauge(
    "scrapeyard_cleanup_eligible_items",
    "Rows currently eligible for retention cleanup by bounded category.",
    ("category",),
    registry=REGISTRY,
)
CLEANUP_OLDEST_ELIGIBLE_AGE = Gauge(
    "scrapeyard_cleanup_oldest_eligible_age_seconds",
    "Age of the oldest currently eligible retention row by bounded category.",
    ("category",),
    registry=REGISTRY,
)
DISK_FREE_BYTES = Gauge(
    "scrapeyard_result_storage_free_bytes",
    "Free bytes on the result artifact filesystem.",
    registry=REGISTRY,
)
METRICS_REFRESH_DURATION = Histogram(
    "scrapeyard_metrics_refresh_duration_seconds",
    "Time spent refreshing durable operational gauges.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2),
    registry=REGISTRY,
)
METRICS_REFRESH_FAILURES = Counter(
    "scrapeyard_metrics_refresh_failures_total",
    "Operational metric refresh failures by bounded subsystem.",
    ("subsystem",),
    registry=REGISTRY,
)
RECONCILIATION_PASSES = Counter(
    "scrapeyard_reconciliation_passes_total",
    "Periodic durability reconciliation passes by fixed service and outcome.",
    ("service", "status"),
    registry=REGISTRY,
)

_active_targets = 0

for _priority in ("high", "normal", "low"):
    QUEUE_DEPTH.labels(_priority).set(0)
    QUEUE_OLDEST_AGE.labels(_priority).set(0)
for _kind in (
    "jobs",
    "targets",
    "browsers",
    "run_threads",
    "lingering_run_threads",
    "result_response_threads",
    "lingering_result_response_threads",
):
    ACTIVE_WORK.labels(_kind).set(0)
for _kind in ("jobs", "browsers", "run_threads", "result_response_threads"):
    WORK_CAPACITY.labels(_kind).set(0)
for _kind in ("missing", "corrupt", "unreadable", "unsafe"):
    CLEANUP_ARTIFACT_FINDINGS.labels(_kind)
for _phase in (
    "error_retention",
    "adhoc_selection",
    "adhoc_errors",
    "adhoc_finalization",
    "scheduled_selection",
    "scheduled_errors",
    "scheduled_pruning",
):
    CLEANUP_HISTORY_FAILURES.labels(_phase)
for _category in _CLEANUP_BACKLOG_CATEGORIES:
    CLEANUP_ELIGIBLE_ITEMS.labels(_category).set(0)
    CLEANUP_OLDEST_ELIGIBLE_AGE.labels(_category).set(0)
for _status in ("pending", "delivered", "failed"):
    WEBHOOK_BACKLOG.labels(_status).set(0)
for _task in (
    "worker",
    "scheduler",
    "cleanup",
    "webhook",
    "queued_reconciliation",
    "running_reconciliation",
):
    BACKGROUND_TASK.labels(_task).set(0)
    LAST_SUCCESS.labels(_task).set(0)
for _service in ("queued_reconciliation", "running_reconciliation"):
    for _outcome in ("success", "failure"):
        RECONCILIATION_PASSES.labels(_service, _outcome)


def render_metrics() -> bytes:
    """Render the process registry using Prometheus' text exposition format."""

    return generate_latest(REGISTRY)


def observe_api_request(method: str, route: str, status_code: int, duration: float) -> None:
    normalized_method = method if method in {"GET", "POST", "PUT", "DELETE", "PATCH"} else "OTHER"
    status_class = f"{status_code // 100}xx" if 100 <= status_code <= 599 else "unknown"
    API_REQUESTS.labels(normalized_method, route, status_class).inc()
    API_DURATION.labels(normalized_method, route).observe(max(0.0, duration))


def observe_run(*, status: str, trigger: str, duration_seconds: float) -> None:
    safe_status = status if status in {"complete", "partial", "failed", "cancelled", "ignored"} else "failed"
    safe_trigger = trigger if trigger in {"adhoc", "scheduled", "manual"} else "other"
    RUNS.labels(safe_status, safe_trigger).inc()
    RUN_DURATION.labels(safe_status).observe(max(0.0, duration_seconds))


def observe_target(
    *,
    status: str,
    fetcher: str,
    duration_seconds: float,
    records: int,
) -> None:
    safe_status = status if status in {"success", "failed", "cancelled"} else "failed"
    safe_fetcher = fetcher if fetcher in {"basic", "dynamic", "stealthy"} else "other"
    TARGETS.labels(safe_status, safe_fetcher).inc()
    TARGET_DURATION.labels(safe_status, safe_fetcher).observe(max(0.0, duration_seconds))
    if records > 0:
        OUTPUT_RECORDS.inc(records)


@contextmanager
def active_target() -> Iterator[None]:
    """Track one target currently executing, including non-browser targets."""

    global _active_targets
    _active_targets += 1
    ACTIVE_WORK.labels("targets").set(_active_targets)
    try:
        yield
    finally:
        _active_targets -= 1
        ACTIVE_WORK.labels("targets").set(_active_targets)


def observe_rate_limit_wait(scope: str, seconds: float) -> None:
    RATE_LIMIT_WAIT.labels(scope if scope in {"domain", "api"} else "other").observe(
        max(0.0, seconds)
    )


def observe_rate_limit_state(scope: str, event: str) -> None:
    """Record bounded-cardinality rate-limit map lifecycle events."""

    safe_scope = scope if scope in {"domain", "api"} else "other"
    safe_event = event if event in {"saturated", "evicted", "expired"} else "other"
    RATE_LIMIT_STATE_EVENTS.labels(safe_scope, safe_event).inc()


def set_cleanup_backlog(
    snapshots: dict[str, CleanupBacklogSnapshot],
    *,
    observed_at: datetime,
) -> None:
    """Publish exact fixed-category cleanup backlog snapshots."""

    for category, snapshot in snapshots.items():
        if category not in _CLEANUP_BACKLOG_CATEGORIES:
            continue
        CLEANUP_ELIGIBLE_ITEMS.labels(category).set(snapshot.eligible_count)
        oldest = snapshot.oldest_eligible_at
        age_seconds = 0.0 if oldest is None else (observed_at - oldest).total_seconds()
        CLEANUP_OLDEST_ELIGIBLE_AGE.labels(category).set(max(0.0, age_seconds))


def mark_last_success(task: str, *, observed_at: float | None = None) -> None:
    LAST_SUCCESS.labels(task).set(time.time() if observed_at is None else observed_at)
