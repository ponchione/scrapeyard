"""Webhook payload construction and firing logic."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from scrapeyard.config.schema import ScrapeConfig, WebhookConfig
from scrapeyard.models.job import JobStatus
from scrapeyard.storage.webhook_outbox import WebhookDeliveryCreate


_DELIVERY_ID_PREFIX = "whv1_"
_DELIVERY_ID_DOMAIN = b"scrapeyard:webhook-delivery:v1"


def should_fire(config: WebhookConfig, status: JobStatus) -> bool:
    """Return True if the webhook should fire for the given job status."""
    return any(s.value == status.value for s in config.on)


def deterministic_delivery_id(
    *,
    job_id: str,
    run_id: str | None,
    event: str,
) -> str:
    """Return the stable logical delivery ID for one job/run/event tuple.

    The versioned, length-delimited encoding prevents ambiguous field
    boundaries without incorporating webhook configuration or other secrets.
    """

    digest = hashlib.sha256()
    digest.update(_DELIVERY_ID_DOMAIN)
    for value in (job_id, run_id or "", event):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return f"{_DELIVERY_ID_PREFIX}{digest.hexdigest()}"


def build_webhook_payload(
    *,
    job_id: str,
    project: str,
    name: str,
    status: JobStatus,
    run_id: str | None,
    result_path: str | None,
    result_count: int | None,
    error_count: int,
    started_at: str,
    completed_at: str,
) -> dict[str, Any]:
    """Construct the webhook POST body from job and run metadata.

    ``delivery_id`` is deterministic for the stable job/run/event tuple so
    receivers can deduplicate retries, restart replay, and repaired intent.
    """
    event = f"job.{status.value}"
    return {
        "delivery_id": deterministic_delivery_id(
            job_id=job_id,
            run_id=run_id,
            event=event,
        ),
        "event": event,
        "job_id": job_id,
        "project": project,
        "name": name,
        "status": status.value,
        "run_id": run_id,
        "result_path": result_path,
        "results_url": f"/results/{job_id}?run_id={run_id}" if run_id else None,
        "result_count": result_count,
        "error_count": error_count,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def webhook_delivery_from_payload(
    config: WebhookConfig,
    payload: dict[str, Any],
    *,
    next_attempt_at: datetime,
) -> WebhookDeliveryCreate:
    """Convert a terminal payload into the durable outbox representation."""

    persisted_payload = dict(payload)
    job_id = str(persisted_payload.get("job_id") or "")
    run_value = persisted_payload.get("run_id")
    run_id = None if run_value is None else str(run_value)
    event = str(persisted_payload.get("event") or "webhook")
    delivery_id = str(
        persisted_payload.get("delivery_id")
        or deterministic_delivery_id(job_id=job_id, run_id=run_id, event=event)
    )
    persisted_payload["delivery_id"] = delivery_id
    return WebhookDeliveryCreate(
        delivery_id=delivery_id,
        job_id=job_id,
        run_id=run_id,
        event=event,
        url=str(config.url),
        headers=dict(config.headers),
        timeout_seconds=float(config.timeout),
        payload=persisted_payload,
        next_attempt_at=next_attempt_at,
    )


def build_terminal_webhook_delivery(
    *,
    config: ScrapeConfig,
    job_id: str,
    status: JobStatus,
    run_id: str,
    result_path: str | None,
    result_count: int | None,
    error_count: int,
    started_at: datetime,
    completed_at: datetime,
) -> WebhookDeliveryCreate | None:
    """Build required terminal intent from a persisted scrape configuration."""

    webhook = config.webhook
    if webhook is None or not should_fire(webhook, status):
        return None
    payload = build_webhook_payload(
        job_id=job_id,
        project=config.project,
        name=config.name,
        status=status,
        run_id=run_id,
        result_path=result_path,
        result_count=result_count,
        error_count=error_count,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
    )
    return webhook_delivery_from_payload(
        webhook,
        payload,
        next_attempt_at=completed_at,
    )
