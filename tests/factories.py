"""Shared factory helpers for Scrapeyard unit and integration tests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, cast

from scrapling import Fetcher
from scrapling.engines.toolbelt.custom import Response

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import db_transaction, get_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.webhook_outbox import (
    WEBHOOK_DELIVERY_COLUMNS,
    SQLiteWebhookOutboxStore,
    WebhookDelivery,
    WebhookDeliveryCreate,
    insert_webhook_delivery,
    row_to_webhook_delivery,
)

DEFAULT_TEST_NOW = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
_FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def make_budget(
    *,
    max_duration_seconds: float = 60,
    max_fetched_bytes: int = 1_000_000,
    max_extracted_records: int = 10_000,
    max_serialized_result_bytes: int = 1_000_000,
    max_browser_debug_bytes: int = 1_000_000,
    max_requests: int = 10_000,
) -> RunBudget:
    """Create a RunBudget with roomy test defaults unless overridden."""
    return RunBudget(
        max_duration_seconds=max_duration_seconds,
        max_fetched_bytes=max_fetched_bytes,
        max_extracted_records=max_extracted_records,
        max_serialized_result_bytes=max_serialized_result_bytes,
        max_browser_debug_bytes=max_browser_debug_bytes,
        max_requests=max_requests,
    )


def make_target_config(
    url: str = "https://example.com",
    *,
    fetcher: FetcherType = FetcherType.basic,
    selectors: dict[str, Any] | None = None,
    **overrides: Any,
) -> TargetConfig:
    """Create a validated TargetConfig with minimal boilerplate."""
    payload: dict[str, Any] = {
        "url": url,
        "fetcher": fetcher,
        "selectors": {"title": "h1"} if selectors is None else selectors,
        **overrides,
    }
    return TargetConfig.model_validate(payload)


def make_job_model(
    job_id: str = "job-1",
    *,
    project: str = "test",
    name: str = "test-job",
    config_yaml: str = "",
    status: JobStatus = JobStatus.queued,
    current_run_id: str | None = None,
    schedule_cron: str | None = None,
    schedule_enabled: bool = True,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    **overrides: Any,
) -> Job:
    """Create a Job model with sensible defaults."""
    payload: dict[str, Any] = {
        "job_id": job_id,
        "project": project,
        "name": name,
        "config_yaml": config_yaml,
        "status": status,
        "current_run_id": current_run_id,
        "schedule_cron": schedule_cron,
        "schedule_enabled": schedule_enabled,
        "updated_at": updated_at,
        **overrides,
    }
    if created_at is not None:
        payload["created_at"] = created_at
    return Job.model_validate(payload)


def make_scrape_yaml(
    *,
    project: str = "test",
    name: str = "test-job",
    url: str = "https://example.com",
    selector_name: str = "title",
    selector_query: str = "h1",
    extra_top_level: str = "",
    extra_target: str = "",
) -> str:
    """Render a minimal valid scrape YAML config."""
    lines = [
        f"project: {project}",
        f"name: {name}",
    ]
    if extra_top_level:
        lines.append(extra_top_level.rstrip("\n"))
    lines.extend([
        "target:",
        f"  url: {url}",
    ])
    if extra_target:
        lines.append(extra_target.rstrip("\n"))
    lines.extend([
        "  selectors:",
        f"    {selector_name}: {selector_query}",
    ])
    return "\n".join(lines) + "\n"


def make_delivery_create(
    delivery_id: str = "delivery-1",
    *,
    job_id: str = "job-1",
    run_id: str | None = "run-1",
    event: str = "job.complete",
    url: str = "https://hooks.example.com/scrapeyard",
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 7.0,
    payload: dict[str, Any] | None = None,
    next_attempt_at: datetime | None = None,
) -> WebhookDeliveryCreate:
    """Create a WebhookDeliveryCreate fixture with consistent defaults."""
    scheduled_at = next_attempt_at or DEFAULT_TEST_NOW
    return WebhookDeliveryCreate(
        delivery_id=delivery_id,
        job_id=job_id,
        run_id=run_id,
        event=event,
        url=url,
        headers={"X-Test": "yes"} if headers is None else headers,
        timeout_seconds=timeout_seconds,
        payload=(
            {
                "delivery_id": delivery_id,
                "event": event,
                "job_id": job_id,
                "run_id": run_id,
            }
            if payload is None
            else payload
        ),
        next_attempt_at=scheduled_at,
    )


def make_html_response(
    url: str,
    html: str,
    *,
    status: int = 200,
    reason: str = "OK",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
) -> Response:
    """Create a Scrapling Response from HTML text without network I/O."""
    return Response(
        url=url,
        text=html,
        body=html.encode("utf-8"),
        status=status,
        reason=reason,
        cookies={} if cookies is None else cookies,
        headers={"Content-Type": "text/html; charset=utf-8"} if headers is None else headers,
        request_headers={},
        **Fetcher._generate_parser_arguments(),
    )


async def seed_webhook_delivery(
    delivery: WebhookDeliveryCreate,
    *,
    now: datetime | None = None,
) -> None:
    """Insert a durable webhook delivery directly into jobs.db for test setup."""
    created_at = now or utc_now()
    async with get_db("jobs.db") as db, db_transaction(db):
        await insert_webhook_delivery(db, delivery, created_at=created_at)


async def fetch_webhook_delivery(delivery_id: str) -> WebhookDelivery | None:
    """Read a persisted webhook delivery row by ID for test assertions."""
    async with get_db("jobs.db", readonly=True) as db:
        cursor = await db.execute(
            f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} "
            "FROM webhook_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return row_to_webhook_delivery(cast(Mapping[str, Any], row))


async def list_pending_webhook_deliveries(*, limit: int = 10_000) -> list[WebhookDelivery]:
    """List all currently pending webhook deliveries via the public outbox store."""
    return await SQLiteWebhookOutboxStore().list_due_pending(now=_FAR_FUTURE, limit=limit)


async def claim_job_run(
    store: SQLiteJobStore,
    job_id: str,
    run_id: str,
    *,
    trigger: str = "adhoc",
    config_hash: str = "h" * 64,
    started_at: datetime | None = None,
) -> bool:
    """Queue and claim one run on SQLiteJobStore for test setup."""
    ts = started_at or DEFAULT_TEST_NOW
    await store.queue_run(
        job_id,
        expected_status=JobStatus.queued.value,
        expected_run_id=None,
        new_run_id=run_id,
        new_trigger=trigger,
        queued_at=ts,
    )
    return await store.claim_run(run_id, job_id, trigger, config_hash, ts)
