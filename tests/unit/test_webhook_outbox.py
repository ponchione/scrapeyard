"""Unit tests for the durable webhook outbox store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scrapeyard.storage.database import close_db, init_db
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookDeliveryCreate,
    WebhookDeliveryStatus,
)


def _delivery(
    delivery_id: str = "delivery-1",
    *,
    job_id: str = "job-1",
    run_id: str | None = "run-1",
    event: str = "job.complete",
    next_attempt_at: datetime | None = None,
) -> WebhookDeliveryCreate:
    scheduled_at = next_attempt_at or datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    return WebhookDeliveryCreate(
        delivery_id=delivery_id,
        job_id=job_id,
        run_id=run_id,
        event=event,
        url="https://hooks.example.com/scrapeyard",
        headers={"X-Test": "yes"},
        timeout_seconds=7.0,
        payload={
            "delivery_id": delivery_id,
            "event": event,
            "job_id": job_id,
            "run_id": run_id,
        },
        next_attempt_at=scheduled_at,
    )


async def test_enqueue_delivery_and_list_pending(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)

    await store.enqueue_delivery(_delivery("due", next_attempt_at=now - timedelta(seconds=1)), now=now)
    await store.enqueue_delivery(_delivery("future", next_attempt_at=now + timedelta(minutes=5)), now=now)

    pending = await store.list_pending()

    assert [delivery.delivery_id for delivery in pending] == ["due", "future"]
    assert pending[0].status is WebhookDeliveryStatus.pending
    assert pending[0].job_id == "job-1"
    assert pending[0].run_id == "run-1"
    assert pending[0].event == "job.complete"
    assert pending[0].url == "https://hooks.example.com/scrapeyard"
    assert pending[0].headers == {"X-Test": "yes"}
    assert pending[0].timeout_seconds == 7.0
    assert pending[0].payload["delivery_id"] == "due"
    await close_db()


async def test_mark_delivered_records_success_and_attempt_count(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    delivered_at = now + timedelta(seconds=3)
    await store.enqueue_delivery(_delivery(), now=now)

    await store.mark_delivered("delivery-1", delivered_at=delivered_at, attempts=2)

    delivery = await store.get_delivery("delivery-1")
    assert delivery is not None
    assert delivery.status is WebhookDeliveryStatus.delivered
    assert delivery.attempts == 2
    assert delivery.delivered_at == delivered_at
    assert delivery.last_attempt_at == delivered_at
    assert delivery.last_error is None
    await close_db()


async def test_mark_retryable_failure_keeps_delivery_pending_with_backoff(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    attempted_at = now + timedelta(seconds=1)
    next_attempt_at = now + timedelta(minutes=1)
    await store.enqueue_delivery(_delivery(), now=now)

    await store.mark_retryable_failure(
        "delivery-1",
        attempted_at=attempted_at,
        next_attempt_at=next_attempt_at,
        last_error="timeout",
        attempts=1,
    )

    delivery = await store.get_delivery("delivery-1")
    assert delivery is not None
    assert delivery.status is WebhookDeliveryStatus.pending
    assert delivery.attempts == 1
    assert delivery.last_attempt_at == attempted_at
    assert delivery.next_attempt_at == next_attempt_at
    assert delivery.last_error == "timeout"
    assert delivery.delivered_at is None
    await close_db()


async def test_mark_permanent_failure_is_inspectable_and_not_due(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(_delivery(), now=now)

    await store.mark_permanent_failure(
        "delivery-1",
        attempted_at=now + timedelta(seconds=1),
        last_error="HTTP 404",
        attempts=1,
    )

    delivery = await store.get_delivery("delivery-1")
    assert delivery is not None
    assert delivery.status is WebhookDeliveryStatus.failed
    assert delivery.attempts == 1
    assert delivery.last_error == "HTTP 404"
    assert await store.list_pending() == []
    await close_db()


async def test_delivery_survives_close_and_reopen_cycle(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(_delivery(), now=now)

    await close_db()
    await init_db(str(db_dir))

    restored = await SQLiteWebhookOutboxStore().get_delivery("delivery-1")
    assert restored is not None
    assert restored.delivery_id == "delivery-1"
    assert restored.payload["job_id"] == "job-1"
    assert restored.status is WebhookDeliveryStatus.pending
    await close_db()
