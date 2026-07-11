"""Unit tests for the durable webhook outbox store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scrapeyard.storage.database import close_db, get_db, init_db
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookDeliveryCreate,
    WebhookDeliveryStatus,
    WebhookFailureReason,
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


async def test_mark_failed_is_inspectable_and_not_due(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(_delivery(), now=now)

    await store.mark_failed(
        "delivery-1",
        failed_at=now + timedelta(seconds=1),
        reason=WebhookFailureReason.non_retryable_failure,
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


async def test_duplicate_enqueue_does_not_reset_terminal_delivery(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    delivery = _delivery()
    await store.enqueue_delivery(delivery, now=now)
    await store.mark_delivered(
        delivery.delivery_id,
        delivered_at=now + timedelta(seconds=1),
        attempts=2,
    )

    await store.enqueue_delivery(delivery, now=now + timedelta(minutes=1))

    persisted = await store.get_delivery(delivery.delivery_id)
    assert persisted is not None
    assert persisted.status is WebhookDeliveryStatus.delivered
    assert persisted.attempts == 2
    assert persisted.delivered_at == now + timedelta(seconds=1)
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


async def test_list_pending_skips_malformed_delivery_rows(tmp_path, caplog):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(_delivery("good"), now=now)
    async with get_db("jobs.db") as db:
        await db.execute(
            """INSERT INTO webhook_deliveries
               (delivery_id, job_id, run_id, event, url, headers_json,
                timeout_seconds, payload_json, status, attempts, next_attempt_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (
                "bad-json",
                "job-1",
                "run-1",
                "job.complete",
                "https://hooks.example.com/scrapeyard",
                "{}",
                7.0,
                "{not-json",
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await db.commit()

    pending = await store.list_pending()

    assert [delivery.delivery_id for delivery in pending] == ["good"]
    assert "bad-json" in caplog.text
    await close_db()


async def test_get_delivery_returns_none_for_malformed_delivery_row(tmp_path, caplog):
    await init_db(str(tmp_path / "db"))
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    async with get_db("jobs.db") as db:
        await db.execute(
            """INSERT INTO webhook_deliveries
               (delivery_id, job_id, run_id, event, url, headers_json,
                timeout_seconds, payload_json, status, attempts, next_attempt_at,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (
                "bad-headers",
                "job-1",
                "run-1",
                "job.complete",
                "https://hooks.example.com/scrapeyard",
                "[]",
                7.0,
                "{}",
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await db.commit()

    assert await SQLiteWebhookOutboxStore().get_delivery("bad-headers") is None
    assert "bad-headers" in caplog.text
    await close_db()


async def test_due_query_is_bounded_and_excludes_future_rows(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(
        _delivery("due-2", next_attempt_at=now - timedelta(seconds=1)),
        now=now,
    )
    await store.enqueue_delivery(
        _delivery("due-1", next_attempt_at=now - timedelta(seconds=2)),
        now=now,
    )
    await store.enqueue_delivery(
        _delivery("future", next_attempt_at=now + timedelta(seconds=1)),
        now=now,
    )

    due = await store.list_due_pending(now=now, limit=1)

    assert [row.delivery_id for row in due] == ["due-1"]
    assert await store.next_pending_due_at() == now - timedelta(seconds=2)
    assert await store.oldest_pending_created_at() == now


async def test_begin_attempt_is_compare_and_set_and_survives_reopen(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(_delivery(), now=now)

    reserved = await store.begin_attempt(
        "delivery-1",
        expected_attempts=0,
        attempted_at=now + timedelta(seconds=1),
    )
    duplicate = await store.begin_attempt(
        "delivery-1",
        expected_attempts=0,
        attempted_at=now + timedelta(seconds=1),
    )
    await close_db()
    await init_db(str(db_dir))
    restored = await SQLiteWebhookOutboxStore().get_delivery("delivery-1")

    assert reserved is not None and reserved.attempts == 1
    assert duplicate is None
    assert restored is not None and restored.attempts == 1
    assert restored.status is WebhookDeliveryStatus.pending


async def test_terminal_transitions_cannot_return_to_pending(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(_delivery(), now=now)
    assert await store.mark_delivered(
        "delivery-1",
        delivered_at=now + timedelta(seconds=1),
    )

    assert not await store.mark_retryable_failure(
        "delivery-1",
        attempted_at=now + timedelta(seconds=2),
        next_attempt_at=now + timedelta(seconds=3),
        last_error="timeout",
    )
    assert not await store.mark_failed(
        "delivery-1",
        failed_at=now + timedelta(seconds=2),
        reason=WebhookFailureReason.non_retryable_failure,
        last_error="should not replace delivered state",
    )
    persisted = await store.get_delivery("delivery-1")
    assert persisted is not None
    assert persisted.status is WebhookDeliveryStatus.delivered


async def test_outbox_summary_counts_oldest_age_and_attempt_distribution(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    await store.enqueue_delivery(_delivery("pending-old"), now=now - timedelta(seconds=30))
    await store.enqueue_delivery(_delivery("pending-new"), now=now - timedelta(seconds=10))
    await store.begin_attempt(
        "pending-new",
        expected_attempts=0,
        attempted_at=now - timedelta(seconds=5),
    )
    await store.enqueue_delivery(_delivery("delivered"), now=now)
    await store.mark_delivered("delivered", delivered_at=now)
    await store.enqueue_delivery(_delivery("failed"), now=now)
    await store.mark_failed(
        "failed",
        failed_at=now,
        last_error="HTTP 404",
        reason=WebhookFailureReason.permanent_http_response,
    )

    summary = await store.summarize(now=now)

    assert summary.pending == 2
    assert summary.delivered == 1
    assert summary.failed == 1
    assert summary.oldest_pending_age_seconds == 30
    assert summary.pending_attempts_min == 0
    assert summary.pending_attempts_max == 1
    assert summary.pending_attempts_total == 1


async def test_terminal_retention_scrubs_secrets_but_keeps_tombstones(tmp_path):
    await init_db(str(tmp_path / "db"))
    store = SQLiteWebhookOutboxStore()
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    for delivery_id in ("delivered", "failed", "pending"):
        await store.enqueue_delivery(_delivery(delivery_id), now=now)
    await store.mark_delivered("delivered", delivered_at=now + timedelta(seconds=1))
    await store.mark_failed(
        "failed",
        failed_at=now + timedelta(seconds=1),
        last_error="HTTP 400 with sensitive context",
        reason=WebhookFailureReason.permanent_http_response,
    )

    before_retention = await store.scrub_terminal_deliveries(
        delivered_before=now,
        failed_before=now,
        scrubbed_at=now + timedelta(seconds=2),
        limit=10,
    )
    inspectable_failed = await store.get_delivery("failed")
    assert before_retention.delivered_scrubbed == 0
    assert before_retention.failed_scrubbed == 0
    assert inspectable_failed is not None
    assert inspectable_failed.last_error == "HTTP 400 with sensitive context"
    assert inspectable_failed.headers == {"X-Test": "yes"}

    summary = await store.scrub_terminal_deliveries(
        delivered_before=now + timedelta(seconds=2),
        failed_before=now + timedelta(seconds=2),
        scrubbed_at=now + timedelta(seconds=3),
        limit=10,
    )
    delivered = await store.get_delivery("delivered")
    failed = await store.get_delivery("failed")
    pending = await store.get_delivery("pending")

    assert summary.delivered_scrubbed == 1
    assert summary.failed_scrubbed == 1
    assert delivered is not None and delivered.is_scrubbed
    assert failed is not None and failed.is_scrubbed
    for terminal in (delivered, failed):
        assert terminal.url == ""
        assert terminal.headers == {}
        assert terminal.payload == {}
        assert terminal.timeout_seconds == 0
        assert terminal.last_error is None
        assert terminal.delivery_id in {"delivered", "failed"}
    assert failed.failure_reason is WebhookFailureReason.permanent_http_response
    assert pending is not None and not pending.is_scrubbed
    assert pending.url == "https://hooks.example.com/scrapeyard"
    assert (await store.list_pending())[0].delivery_id == "pending"

    repeated = await store.scrub_terminal_deliveries(
        delivered_before=now + timedelta(days=1),
        failed_before=now + timedelta(days=1),
        scrubbed_at=now + timedelta(days=1),
        limit=10,
    )
    assert repeated.delivered_scrubbed == 0
    assert repeated.failed_scrubbed == 0
