"""Unit tests for webhook/dispatcher.py — retry, backoff, and lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from urllib.parse import urlparse, urlunparse

import httpx
import pytest

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.engine.url_guard import ResolvedPublicURL, resolve_public_url
from scrapeyard.storage.webhook_outbox import (
    WebhookDelivery,
    WebhookDeliveryCreate,
    WebhookDeliveryStatus,
    WebhookFailureReason,
    WebhookOutboxSummary,
    WebhookRetentionSummary,
)
from scrapeyard.webhook.dispatcher import (
    HttpWebhookDispatcher,
    WebhookDispatchStatus,
    WebhookRequestConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_test_webhook_hosts(monkeypatch) -> None:
    """Keep fake .example hosts deterministic while retaining literal-IP guards."""

    def _resolve(url: str) -> ResolvedPublicURL:
        parsed = urlparse(url)
        if parsed.hostname and parsed.hostname.endswith(".example.com") or parsed.hostname == "example.com":
            port = f":{parsed.port}" if parsed.port is not None else ""
            return ResolvedPublicURL(
                urlunparse(parsed._replace(netloc=f"93.184.216.34{port}")),
                f"{parsed.hostname}{port}",
                parsed.hostname,
            )
        return resolve_public_url(url)

    monkeypatch.setattr("scrapeyard.webhook.dispatcher.resolve_public_url", _resolve)


def _webhook_config(url: str = "https://example.com/hook") -> WebhookConfig:
    return WebhookConfig(url=cast(Any, url), headers={}, timeout=5)


def _ok_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://x"))


def _err_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://x"))


def _payload(delivery_id: str = "delivery-1") -> dict[str, Any]:
    return {
        "delivery_id": delivery_id,
        "event": "job.complete",
        "job_id": "job-1",
        "run_id": "run-1",
    }


class MemoryWebhookOutboxStore:
    def __init__(self) -> None:
        self.deliveries: dict[str, WebhookDelivery] = {}

    async def enqueue_delivery(
        self,
        delivery: WebhookDeliveryCreate,
        *,
        now: datetime | None = None,
    ) -> None:
        created_at = now or datetime.now(timezone.utc)
        if delivery.delivery_id in self.deliveries:
            return
        self.deliveries[delivery.delivery_id] = WebhookDelivery(
            delivery_id=delivery.delivery_id,
            job_id=delivery.job_id,
            run_id=delivery.run_id,
            event=delivery.event,
            url=delivery.url,
            headers=delivery.headers,
            timeout_seconds=delivery.timeout_seconds,
            payload=delivery.payload,
            status=WebhookDeliveryStatus.pending,
            attempts=0,
            next_attempt_at=delivery.next_attempt_at,
            last_attempt_at=None,
            delivered_at=None,
            failed_at=None,
            failure_reason=None,
            last_error=None,
            scrubbed_at=None,
            created_at=created_at,
            updated_at=created_at,
        )

    async def list_pending(self, *, limit: int | None = None) -> list[WebhookDelivery]:
        rows = [
            delivery
            for delivery in self.deliveries.values()
            if delivery.status is WebhookDeliveryStatus.pending
        ]
        rows.sort(key=lambda delivery: delivery.next_attempt_at)
        return rows if limit is None else rows[:limit]

    async def list_due_pending(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[WebhookDelivery]:
        return [
            delivery
            for delivery in await self.list_pending()
            if delivery.next_attempt_at <= now
        ][:limit]

    async def list_exhausted_pending(
        self,
        *,
        attempts_gte: int,
        created_at_lte: datetime,
        limit: int,
    ) -> list[WebhookDelivery]:
        rows = [
            delivery
            for delivery in await self.list_pending()
            if delivery.attempts >= attempts_gte
            or delivery.created_at <= created_at_lte
        ]
        rows.sort(key=lambda delivery: (delivery.created_at, delivery.delivery_id))
        return rows[:limit]

    async def next_pending_due_at(self) -> datetime | None:
        pending = await self.list_pending()
        return None if not pending else pending[0].next_attempt_at

    async def oldest_pending_created_at(self) -> datetime | None:
        pending = await self.list_pending()
        return None if not pending else min(row.created_at for row in pending)

    async def summarize(
        self,
        *,
        now: datetime | None = None,
    ) -> WebhookOutboxSummary:
        observed_at = now or datetime.now(timezone.utc)
        pending = await self.list_pending()
        attempts = [row.attempts for row in pending]
        oldest = await self.oldest_pending_created_at()
        return WebhookOutboxSummary(
            pending=len(pending),
            delivered=sum(
                row.status is WebhookDeliveryStatus.delivered
                for row in self.deliveries.values()
            ),
            failed=sum(
                row.status is WebhookDeliveryStatus.failed
                for row in self.deliveries.values()
            ),
            oldest_pending_age_seconds=(
                None
                if oldest is None
                else max(0.0, (observed_at - oldest).total_seconds())
            ),
            pending_attempts_min=None if not attempts else min(attempts),
            pending_attempts_max=None if not attempts else max(attempts),
            pending_attempts_total=sum(attempts),
        )

    async def get_delivery(self, delivery_id: str) -> WebhookDelivery | None:
        return self.deliveries.get(delivery_id)

    async def begin_attempt(
        self,
        delivery_id: str,
        *,
        expected_attempts: int,
        attempted_at: datetime,
    ) -> WebhookDelivery | None:
        delivery = self.deliveries[delivery_id]
        if (
            delivery.status is not WebhookDeliveryStatus.pending
            or delivery.attempts != expected_attempts
        ):
            return None
        reserved = replace(
            delivery,
            attempts=delivery.attempts + 1,
            last_attempt_at=attempted_at,
            updated_at=attempted_at,
        )
        self.deliveries[delivery_id] = reserved
        return reserved

    async def mark_delivered(
        self,
        delivery_id: str,
        *,
        delivered_at: datetime,
        expected_attempts: int | None = None,
        attempts: int = 1,
    ) -> bool:
        delivery = self.deliveries[delivery_id]
        if delivery.status is not WebhookDeliveryStatus.pending:
            return False
        if expected_attempts is not None and delivery.attempts != expected_attempts:
            return False
        attempt_count = delivery.attempts + (attempts if expected_attempts is None else 0)
        self.deliveries[delivery_id] = replace(
            delivery,
            status=WebhookDeliveryStatus.delivered,
            attempts=attempt_count,
            last_attempt_at=(
                delivered_at if expected_attempts is None else delivery.last_attempt_at
            ),
            delivered_at=delivered_at,
            last_error=None,
            updated_at=delivered_at,
        )
        return True

    async def mark_retryable_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        next_attempt_at: datetime,
        last_error: str,
        expected_attempts: int | None = None,
        attempts: int = 1,
    ) -> bool:
        delivery = self.deliveries[delivery_id]
        if delivery.status is not WebhookDeliveryStatus.pending:
            return False
        if expected_attempts is not None and delivery.attempts != expected_attempts:
            return False
        attempt_count = delivery.attempts + (attempts if expected_attempts is None else 0)
        self.deliveries[delivery_id] = replace(
            delivery,
            status=WebhookDeliveryStatus.pending,
            attempts=attempt_count,
            last_attempt_at=(
                attempted_at if expected_attempts is None else delivery.last_attempt_at
            ),
            next_attempt_at=next_attempt_at,
            last_error=last_error,
            updated_at=attempted_at,
        )
        return True

    async def mark_failed(
        self,
        delivery_id: str,
        *,
        failed_at: datetime,
        reason: WebhookFailureReason,
        last_error: str | None,
        expected_attempts: int | None = None,
        attempts: int = 0,
    ) -> bool:
        delivery = self.deliveries[delivery_id]
        if delivery.status is not WebhookDeliveryStatus.pending:
            return False
        if expected_attempts is not None and delivery.attempts != expected_attempts:
            return False
        attempt_count = delivery.attempts + (attempts if expected_attempts is None else 0)
        self.deliveries[delivery_id] = replace(
            delivery,
            status=WebhookDeliveryStatus.failed,
            attempts=attempt_count,
            last_attempt_at=(
                failed_at
                if expected_attempts is None and attempts
                else delivery.last_attempt_at
            ),
            failed_at=failed_at,
            failure_reason=reason,
            last_error=last_error,
            updated_at=failed_at,
        )
        return True

    async def scrub_terminal_deliveries(
        self,
        *,
        delivered_before: datetime,
        failed_before: datetime,
        scrubbed_at: datetime,
        limit: int,
    ) -> WebhookRetentionSummary:
        del delivered_before, failed_before, scrubbed_at, limit
        return WebhookRetentionSummary()


async def _wait_until(condition) -> None:
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSendOnce:
    @pytest.mark.asyncio
    async def test_send_once_blocks_non_public_persisted_urls(self) -> None:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client)

        result = await dispatcher.send_once(
            WebhookRequestConfig(
                url="http://127.0.0.1/hook",
                headers={},
                timeout=5,
            ),
            {},
        )

        assert result.status is WebhookDispatchStatus.permanent_failed
        assert "unsafe" in (result.last_error or "")
        client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_once_disables_http_redirect_following(self) -> None:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client)

        result = await dispatcher.send_once(_webhook_config(), {})

        assert result.status is WebhookDispatchStatus.delivered
        assert client.post.await_args.kwargs["follow_redirects"] is False
        assert client.post.await_args.args[0] == "https://93.184.216.34/hook"
        assert client.post.await_args.kwargs["headers"]["Host"] == "example.com"
        assert client.post.await_args.kwargs["extensions"] == {
            "sni_hostname": "example.com"
        }


class TestBackoffDelay:
    def test_exponential_backoff(self) -> None:
        dispatcher = HttpWebhookDispatcher(backoff_base=1.0, backoff_max=30.0)
        assert dispatcher._backoff_delay(0) == 1.0
        assert dispatcher._backoff_delay(1) == 2.0
        assert dispatcher._backoff_delay(2) == 4.0
        assert dispatcher._backoff_delay(3) == 8.0

    def test_backoff_cap(self) -> None:
        dispatcher = HttpWebhookDispatcher(backoff_base=1.0, backoff_max=5.0)
        assert dispatcher._backoff_delay(0) == 1.0
        assert dispatcher._backoff_delay(10) == 5.0  # capped

    def test_is_retryable_status(self) -> None:
        assert HttpWebhookDispatcher._is_retryable_status(500) is True
        assert HttpWebhookDispatcher._is_retryable_status(502) is True
        assert HttpWebhookDispatcher._is_retryable_status(503) is True
        assert HttpWebhookDispatcher._is_retryable_status(429) is True
        assert HttpWebhookDispatcher._is_retryable_status(400) is False
        assert HttpWebhookDispatcher._is_retryable_status(404) is False
        assert HttpWebhookDispatcher._is_retryable_status(200) is False


class TestNotifyAndShutdown:
    @pytest.mark.asyncio
    async def test_notify_wakes_without_recreating_durable_intent(self) -> None:
        outbox = AsyncMock()
        dispatcher = HttpWebhookDispatcher(outbox_store=outbox)
        dispatcher._started = True

        await dispatcher.notify()

        assert dispatcher._wake_event.is_set()
        outbox.enqueue_delivery.assert_not_awaited()

class TestDurableOutboxDispatch:
    @pytest.mark.asyncio
    async def test_submit_persists_delivery_before_http_attempt_finishes(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        release_post = asyncio.Event()

        async def _post(*_args, **_kwargs):
            await release_post.wait()
            return _ok_response(200)

        client = AsyncMock()
        client.post = AsyncMock(side_effect=_post)
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
        )

        await _enqueue_memory_delivery(
            outbox,
            delivery_id="delivery-1",
            created_at=datetime.now(timezone.utc),
        )
        await dispatcher.startup()

        persisted = outbox.deliveries["delivery-1"]
        assert persisted.status is WebhookDeliveryStatus.pending
        assert persisted.payload["delivery_id"] == "delivery-1"
        release_post.set()
        await _wait_until(
            lambda: outbox.deliveries["delivery-1"].status
            is WebhookDeliveryStatus.delivered
        )
        await dispatcher.shutdown(timeout=1.0)
        assert outbox.deliveries["delivery-1"].status is WebhookDeliveryStatus.delivered

    @pytest.mark.asyncio
    async def test_startup_replays_pending_delivery_from_outbox(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        now = datetime.now(timezone.utc)
        await outbox.enqueue_delivery(
            WebhookDeliveryCreate(
                delivery_id="delivery-1",
                job_id="job-1",
                run_id="run-1",
                event="job.complete",
                url="https://hooks.example.com/scrapeyard",
                headers={},
                timeout_seconds=5.0,
                payload=_payload(),
                next_attempt_at=now - timedelta(seconds=1),
            ),
            now=now,
        )
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
        )

        await dispatcher.startup()
        await _wait_until(lambda: client.post.await_count == 1)
        await dispatcher.shutdown(timeout=1.0)

        client.post.assert_awaited_once()
        assert outbox.deliveries["delivery-1"].status is WebhookDeliveryStatus.delivered

    @pytest.mark.asyncio
    async def test_retryable_failure_leaves_delivery_pending_with_next_attempt(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        client = AsyncMock()
        client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            backoff_base=60.0,
            outbox_store=outbox,
        )

        await _enqueue_memory_delivery(
            outbox,
            delivery_id="delivery-1",
            created_at=datetime.now(timezone.utc),
        )
        await dispatcher.startup()
        await _wait_until(
            lambda: outbox.deliveries["delivery-1"].last_error is not None
        )

        delivery = outbox.deliveries["delivery-1"]
        assert delivery.status is WebhookDeliveryStatus.pending
        assert delivery.last_error is not None
        assert delivery.last_error == "Transport failure: TimeoutException"
        assert delivery.last_attempt_at is not None
        assert delivery.next_attempt_at > delivery.last_attempt_at
        await dispatcher.shutdown(timeout=0.01)

    @pytest.mark.asyncio
    async def test_nonretryable_4xx_marks_delivery_permanently_failed(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        client = AsyncMock()
        client.post = AsyncMock(return_value=_err_response(404))
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
        )

        await _enqueue_memory_delivery(
            outbox,
            delivery_id="delivery-1",
            created_at=datetime.now(timezone.utc),
        )
        await dispatcher.startup()
        await _wait_until(
            lambda: outbox.deliveries["delivery-1"].status
            is WebhookDeliveryStatus.failed
        )
        await dispatcher.shutdown(timeout=1.0)

        delivery = outbox.deliveries["delivery-1"]
        assert delivery.status is WebhookDeliveryStatus.failed
        assert delivery.attempts == 1
        assert delivery.last_error == "HTTP 404"
        assert delivery.failure_reason is WebhookFailureReason.permanent_http_response

    @pytest.mark.asyncio
    async def test_startup_marks_unsafe_persisted_delivery_failed_without_http(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        now = datetime.now(timezone.utc)
        await outbox.enqueue_delivery(
            WebhookDeliveryCreate(
                delivery_id="delivery-1",
                job_id="job-1",
                run_id="run-1",
                event="job.complete",
                url="http://127.0.0.1/hook",
                headers={},
                timeout_seconds=5.0,
                payload=_payload(),
                next_attempt_at=now - timedelta(seconds=1),
            ),
            now=now,
        )
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
        )

        await dispatcher.startup()
        await _wait_until(
            lambda: outbox.deliveries["delivery-1"].status
            is WebhookDeliveryStatus.failed
        )
        await dispatcher.shutdown(timeout=1.0)

        client.post.assert_not_awaited()
        delivery = outbox.deliveries["delivery-1"]
        assert delivery.status is WebhookDeliveryStatus.failed
        assert "non-public" in (delivery.last_error or "")
        assert delivery.failure_reason is WebhookFailureReason.unsafe_url

    @pytest.mark.asyncio
    async def test_as_utc_treats_naive_persisted_time_as_utc(self) -> None:
        dispatcher = HttpWebhookDispatcher(outbox_store=MemoryWebhookOutboxStore())
        assert dispatcher._as_utc(datetime(2026, 4, 24, 12, 1)) == datetime(
            2026, 4, 24, 12, 1, tzinfo=timezone.utc
        )


class TestDeliveryId:
    def test_payload_contains_delivery_id(self) -> None:
        from scrapeyard.webhook.payload import build_webhook_payload
        from scrapeyard.models.job import JobStatus

        payload = build_webhook_payload(
            job_id="j1",
            project="test",
            name="test-job",
            status=JobStatus.complete,
            run_id="run-1",
            result_path="/tmp/x",
            result_count=5,
            error_count=0,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:01:00Z",
        )
        assert "delivery_id" in payload
        assert isinstance(payload["delivery_id"], str)
        assert payload["delivery_id"].startswith("whv1_")
        assert len(payload["delivery_id"]) == 69

    def test_same_logical_event_reuses_delivery_id(self) -> None:
        from scrapeyard.webhook.payload import build_webhook_payload
        from scrapeyard.models.job import JobStatus

        p1 = build_webhook_payload(
            job_id="j1",
            project="test",
            name="test-job",
            status=JobStatus.complete,
            run_id="run-1",
            result_path="/tmp/x",
            result_count=5,
            error_count=0,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:01:00Z",
        )
        p2 = build_webhook_payload(
            job_id="j1",
            project="test",
            name="test-job",
            status=JobStatus.complete,
            run_id="run-1",
            result_path="/tmp/x",
            result_count=5,
            error_count=0,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:01:00Z",
        )
        assert p1["delivery_id"] == p2["delivery_id"]


async def _enqueue_memory_delivery(
    outbox: MemoryWebhookOutboxStore,
    *,
    delivery_id: str,
    created_at: datetime,
    next_attempt_at: datetime | None = None,
    url: str = "https://hooks.example.com/scrapeyard",
) -> WebhookDelivery:
    await outbox.enqueue_delivery(
        WebhookDeliveryCreate(
            delivery_id=delivery_id,
            job_id=f"job-{delivery_id}",
            run_id=f"run-{delivery_id}",
            event="job.complete",
            url=url,
            headers={"Authorization": "secret"},
            timeout_seconds=5.0,
            payload=_payload(delivery_id)
            | {
                "job_id": f"job-{delivery_id}",
                "run_id": f"run-{delivery_id}",
            },
            next_attempt_at=next_attempt_at or created_at,
        ),
        now=created_at,
    )
    return outbox.deliveries[delivery_id]


class TestPersistentRetryBounds:
    @pytest.mark.asyncio
    async def test_max_attempts_one_performs_one_total_http_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="one-attempt",
            created_at=now,
        )
        client = AsyncMock()
        client.post.return_value = _err_response(503)
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            max_delivery_attempts=1,
        )

        await dispatcher._process_delivery(delivery)

        client.post.assert_awaited_once()
        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.attempts == 1
        assert persisted.status is WebhookDeliveryStatus.failed
        assert persisted.failure_reason is WebhookFailureReason.attempt_exhausted

    @pytest.mark.asyncio
    async def test_row_already_at_attempt_limit_fails_without_http(self) -> None:
        now = datetime.now(timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="already-exhausted",
            created_at=now,
        )
        delivery = replace(delivery, attempts=3)
        outbox.deliveries[delivery.delivery_id] = delivery
        client = AsyncMock()
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            max_delivery_attempts=3,
        )

        await dispatcher._process_delivery(delivery)

        client.post.assert_not_awaited()
        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.status is WebhookDeliveryStatus.failed
        assert persisted.attempts == 3
        assert persisted.failure_reason is WebhookFailureReason.attempt_exhausted

    @pytest.mark.asyncio
    async def test_attempt_count_survives_restart_and_delivery_id_is_reused(self) -> None:
        now = datetime.now(timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="restart",
            created_at=now,
        )
        failing_client = AsyncMock()
        failing_client.post.side_effect = httpx.TimeoutException("secret endpoint detail")
        first = HttpWebhookDispatcher(
            client_factory=lambda: failing_client,
            outbox_store=outbox,
            max_delivery_attempts=3,
            backoff_base=1,
        )
        await first._process_delivery(delivery)
        after_failure = outbox.deliveries[delivery.delivery_id]
        assert after_failure.attempts == 1
        assert after_failure.status is WebhookDeliveryStatus.pending

        success_client = AsyncMock()
        success_client.post.return_value = _ok_response()
        second = HttpWebhookDispatcher(
            client_factory=lambda: success_client,
            outbox_store=outbox,
            max_delivery_attempts=3,
        )
        await second._process_delivery(after_failure)

        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.status is WebhookDeliveryStatus.delivered
        assert persisted.attempts == 2
        assert persisted.delivery_id == "restart"

    @pytest.mark.asyncio
    async def test_age_just_below_boundary_may_attempt_and_boundary_does_not(
        self,
        monkeypatch,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        below = await _enqueue_memory_delivery(
            outbox,
            delivery_id="below-age",
            created_at=created,
        )
        at_boundary = await _enqueue_memory_delivery(
            outbox,
            delivery_id="at-age",
            created_at=created,
        )
        client = AsyncMock()
        client.post.return_value = _ok_response()
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            max_delivery_age_seconds=10,
        )

        monkeypatch.setattr(
            "scrapeyard.webhook.dispatcher.utc_now",
            lambda: created + timedelta(seconds=9, microseconds=999999),
        )
        await dispatcher._process_delivery(below)
        monkeypatch.setattr(
            "scrapeyard.webhook.dispatcher.utc_now",
            lambda: created + timedelta(seconds=10),
        )
        await dispatcher._process_delivery(at_boundary)

        assert client.post.await_count == 1
        assert outbox.deliveries["below-age"].status is WebhookDeliveryStatus.delivered
        assert outbox.deliveries["at-age"].failure_reason is WebhookFailureReason.age_exhausted

    @pytest.mark.asyncio
    async def test_row_that_ages_out_before_future_due_time_does_not_send(
        self,
        monkeypatch,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="aged-while-waiting",
            created_at=created,
            next_attempt_at=created + timedelta(minutes=5),
        )
        client = AsyncMock()
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            max_delivery_age_seconds=10,
        )
        monkeypatch.setattr(
            "scrapeyard.webhook.dispatcher.utc_now",
            lambda: created + timedelta(seconds=10),
        )

        await dispatcher._process_delivery(delivery)

        client.post.assert_not_awaited()
        assert outbox.deliveries[delivery.delivery_id].failure_reason is (
            WebhookFailureReason.age_exhausted
        )

    @pytest.mark.asyncio
    async def test_retry_delay_at_delivery_deadline_fails_immediately(
        self,
        monkeypatch,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="retry-past-age",
            created_at=created,
        )
        client = AsyncMock()
        client.post.return_value = _err_response(503)
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            max_delivery_age_seconds=10,
            backoff_base=10,
        )
        monkeypatch.setattr("scrapeyard.webhook.dispatcher.utc_now", lambda: created)

        await dispatcher._process_delivery(delivery)

        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.status is WebhookDeliveryStatus.failed
        assert persisted.failure_reason is WebhookFailureReason.age_exhausted
        assert persisted.attempts == 1

    @pytest.mark.asyncio
    async def test_permanent_4xx_has_precise_reason_and_no_future_retry(self) -> None:
        now = datetime.now(timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="permanent-4xx",
            created_at=now,
        )
        client = AsyncMock()
        client.post.return_value = _err_response(400)
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
        )

        await dispatcher._process_delivery(delivery)

        client.post.assert_awaited_once()
        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.failure_reason is WebhookFailureReason.permanent_http_response
        assert persisted.last_error == "HTTP 400"

    @pytest.mark.asyncio
    async def test_unexpected_dispatcher_failure_is_non_retryable_and_sanitized(
        self,
        monkeypatch,
    ) -> None:
        now = datetime.now(timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="non-retryable",
            created_at=now,
        )
        dispatcher = HttpWebhookDispatcher(outbox_store=outbox)
        monkeypatch.setattr(
            dispatcher,
            "send_once",
            AsyncMock(side_effect=ValueError("secret failure detail")),
        )

        await dispatcher._process_delivery(delivery)

        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.failure_reason is WebhookFailureReason.non_retryable_failure
        assert persisted.last_error == "Non-retryable dispatcher failure: ValueError"
        assert "secret failure detail" not in persisted.last_error


class TestRetryAfter:
    def test_valid_delta_seconds(self) -> None:
        now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        response = httpx.Response(
            429,
            headers={"Retry-After": "17"},
            request=httpx.Request("POST", "https://example.com"),
        )

        delay, action = HttpWebhookDispatcher._parse_retry_after(response, now=now)

        assert delay == 17
        assert action == "accepted_delta_seconds"
        zero = httpx.Response(
            429,
            headers={"Retry-After": "0"},
            request=httpx.Request("POST", "https://example.com"),
        )
        assert HttpWebhookDispatcher._parse_retry_after(zero, now=now) == (
            0.0,
            "accepted_delta_seconds",
        )

    def test_valid_http_date(self) -> None:
        now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        response = httpx.Response(
            503,
            headers={"Retry-After": format_datetime(now + timedelta(seconds=30))},
            request=httpx.Request("POST", "https://example.com"),
        )

        delay, action = HttpWebhookDispatcher._parse_retry_after(response, now=now)

        assert delay == 30
        assert action == "accepted_http_date"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "header_value", "expected_delay"),
        [
            (429, "17", 17),
            (503, "Fri, 10 Jul 2026 12:00:30 GMT", 30),
        ],
    )
    async def test_valid_retry_after_controls_persisted_minimum_delay(
        self,
        monkeypatch,
        status,
        header_value,
        expected_delay,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id=f"retry-after-{status}",
            created_at=created,
        )
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            status,
            headers={"Retry-After": header_value},
            request=httpx.Request("POST", "https://example.com"),
        )
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            backoff_base=1,
        )
        monkeypatch.setattr("scrapeyard.webhook.dispatcher.utc_now", lambda: created)

        await dispatcher._process_delivery(delivery)

        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.status is WebhookDeliveryStatus.pending
        assert persisted.next_attempt_at == created + timedelta(seconds=expected_delay)

    @pytest.mark.parametrize(
        ("status", "value", "expected_action"),
        [
            (429, "bogus", "malformed"),
            (429, "-1", "malformed"),
            (429, "9" * 309, "out_of_range"),
            (
                503,
                "Fri, 10 Jul 2026 11:59:59 GMT",
                "past_http_date",
            ),
            (500, "12", "inapplicable_status"),
        ],
    )
    def test_invalid_past_and_inapplicable_values_are_ignored(
        self,
        status,
        value,
        expected_action,
    ) -> None:
        response = httpx.Response(
            status,
            headers={"Retry-After": value},
            request=httpx.Request("POST", "https://example.com"),
        )
        delay, action = HttpWebhookDispatcher._parse_retry_after(
            response,
            now=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc),
        )
        assert delay is None
        assert action == expected_action

    @pytest.mark.asyncio
    async def test_out_of_range_retry_after_uses_normal_backoff(
        self,
        monkeypatch,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="retry-after-out-of-range",
            created_at=created,
        )
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            429,
            headers={"Retry-After": "9" * 309},
            request=httpx.Request("POST", "https://example.com"),
        )
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            backoff_base=1,
        )
        monkeypatch.setattr("scrapeyard.webhook.dispatcher.utc_now", lambda: created)

        await dispatcher._process_delivery(delivery)

        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.status is WebhookDeliveryStatus.pending
        assert persisted.next_attempt_at == created + timedelta(seconds=1)

    @pytest.mark.asyncio
    async def test_large_finite_retry_after_exhausts_age_without_datetime_overflow(
        self,
        monkeypatch,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="retry-after-finite-overflow",
            created_at=created,
        )
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            503,
            headers={"Retry-After": "9" * 308},
            request=httpx.Request("POST", "https://example.com"),
        )
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
        )
        monkeypatch.setattr("scrapeyard.webhook.dispatcher.utc_now", lambda: created)

        await dispatcher._process_delivery(delivery)

        persisted = outbox.deliveries[delivery.delivery_id]
        assert persisted.status is WebhookDeliveryStatus.failed
        assert persisted.failure_reason is WebhookFailureReason.age_exhausted

    @pytest.mark.asyncio
    async def test_retry_after_cannot_extend_beyond_maximum_age(
        self,
        monkeypatch,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        delivery = await _enqueue_memory_delivery(
            outbox,
            delivery_id="retry-after-age",
            created_at=created,
        )
        client = AsyncMock()
        client.post.return_value = httpx.Response(
            503,
            headers={"Retry-After": "60"},
            request=httpx.Request("POST", "https://example.com"),
        )
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            max_delivery_age_seconds=30,
        )
        monkeypatch.setattr("scrapeyard.webhook.dispatcher.utc_now", lambda: created)

        await dispatcher._process_delivery(delivery)

        assert outbox.deliveries[delivery.delivery_id].failure_reason is (
            WebhookFailureReason.age_exhausted
        )


class TestBoundedCoordinator:
    @pytest.mark.asyncio
    async def test_large_due_backlog_never_exceeds_concurrency_or_task_bound(self) -> None:
        now = datetime.now(timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        for index in range(20):
            await _enqueue_memory_delivery(
                outbox,
                delivery_id=f"backlog-{index:02d}",
                created_at=now,
                next_attempt_at=now - timedelta(seconds=1),
            )
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def _post(*_args, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await release.wait()
                return _ok_response()
            finally:
                active -= 1

        client = AsyncMock()
        client.post.side_effect = _post
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            dispatch_concurrency=3,
            dispatch_batch_size=5,
        )

        await dispatcher.startup()
        await _wait_until(lambda: active == 3)

        assert max_active == 3
        assert dispatcher.background_task_count == 4
        assert dispatcher.pending_tasks <= 8
        release.set()
        await dispatcher.shutdown(timeout=2)
        assert max_active == 3

    @pytest.mark.asyncio
    async def test_future_backlog_creates_no_per_row_tasks(self) -> None:
        now = datetime.now(timezone.utc)
        outbox = MemoryWebhookOutboxStore()
        for index in range(100):
            await _enqueue_memory_delivery(
                outbox,
                delivery_id=f"future-{index:03d}",
                created_at=now,
                next_attempt_at=now + timedelta(hours=1),
            )
        dispatcher = HttpWebhookDispatcher(
            outbox_store=outbox,
            dispatch_concurrency=2,
            dispatch_batch_size=4,
        )

        await dispatcher.startup()
        await asyncio.sleep(0.02)

        assert dispatcher.background_task_count == 3
        assert dispatcher.pending_tasks == 0
        await dispatcher.shutdown(timeout=1)

    @pytest.mark.asyncio
    async def test_coordinator_wakes_at_age_deadline_before_future_due_time(
        self,
        monkeypatch,
    ) -> None:
        created = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        clock = [created]
        monkeypatch.setattr(
            "scrapeyard.webhook.dispatcher.utc_now",
            lambda: clock[0],
        )
        outbox = MemoryWebhookOutboxStore()
        await _enqueue_memory_delivery(
            outbox,
            delivery_id="age-wakeup",
            created_at=created,
            next_attempt_at=created + timedelta(hours=1),
        )
        client = AsyncMock()
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            max_delivery_age_seconds=10,
            dispatch_concurrency=1,
            dispatch_batch_size=1,
        )

        await dispatcher.startup()
        await asyncio.sleep(0.02)
        clock[0] = created + timedelta(seconds=10)
        dispatcher._wake_event.set()
        await _wait_until(
            lambda: outbox.deliveries["age-wakeup"].status
            is WebhookDeliveryStatus.failed
        )
        await dispatcher.shutdown(timeout=1)

        client.post.assert_not_awaited()
        assert outbox.deliveries["age-wakeup"].failure_reason is (
            WebhookFailureReason.age_exhausted
        )

    @pytest.mark.asyncio
    async def test_startup_due_queries_use_configured_batch_limit(self) -> None:
        class RecordingStore(MemoryWebhookOutboxStore):
            def __init__(self) -> None:
                super().__init__()
                self.due_limits: list[int] = []

            async def list_due_pending(self, *, now, limit):
                self.due_limits.append(limit)
                return await super().list_due_pending(now=now, limit=limit)

        outbox = RecordingStore()
        now = datetime.now(timezone.utc)
        await _enqueue_memory_delivery(
            outbox,
            delivery_id="batch",
            created_at=now,
        )
        client = AsyncMock()
        client.post.return_value = _ok_response()
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            dispatch_concurrency=2,
            dispatch_batch_size=7,
        )

        await dispatcher.startup()
        await _wait_until(lambda: client.post.await_count == 1)
        await dispatcher.shutdown(timeout=1)

        assert outbox.due_limits
        assert set(outbox.due_limits) == {7}

    @pytest.mark.asyncio
    async def test_duplicate_durable_intent_does_not_dispatch_concurrently(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        release = asyncio.Event()
        active = 0
        max_active = 0

        async def _post(*_args, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await release.wait()
            active -= 1
            return _ok_response()

        client = AsyncMock()
        client.post.side_effect = _post
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            dispatch_concurrency=2,
            dispatch_batch_size=2,
        )

        await asyncio.gather(
            _enqueue_memory_delivery(
                outbox,
                delivery_id="same",
                created_at=datetime.now(timezone.utc),
            ),
            _enqueue_memory_delivery(
                outbox,
                delivery_id="same",
                created_at=datetime.now(timezone.utc),
            ),
        )
        await dispatcher.startup()
        await _wait_until(lambda: active == 1)
        assert max_active == 1
        release.set()
        await dispatcher.shutdown(timeout=1)
        assert client.post.await_count == 1

    @pytest.mark.asyncio
    async def test_shutdown_cancellation_leaves_pending_and_restart_resumes_once(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        started = asyncio.Event()

        async def _blocked_post(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        blocked_client = AsyncMock()
        blocked_client.post.side_effect = _blocked_post
        first = HttpWebhookDispatcher(
            client_factory=lambda: blocked_client,
            outbox_store=outbox,
            dispatch_concurrency=1,
            dispatch_batch_size=1,
            max_delivery_attempts=3,
        )
        await _enqueue_memory_delivery(
            outbox,
            delivery_id="shutdown",
            created_at=datetime.now(timezone.utc),
        )
        await first.startup()
        await asyncio.wait_for(started.wait(), timeout=1)

        await first.shutdown(timeout=0.01)

        pending = outbox.deliveries["shutdown"]
        assert pending.status is WebhookDeliveryStatus.pending
        assert pending.attempts == 1

        success_client = AsyncMock()
        success_client.post.return_value = _ok_response()
        second = HttpWebhookDispatcher(
            client_factory=lambda: success_client,
            outbox_store=outbox,
            dispatch_concurrency=1,
            dispatch_batch_size=1,
            max_delivery_attempts=3,
        )
        await second.startup()
        await _wait_until(
            lambda: outbox.deliveries["shutdown"].status
            is WebhookDeliveryStatus.delivered
        )
        await second.shutdown(timeout=1)

        success_client.post.assert_awaited_once()
        assert outbox.deliveries["shutdown"].attempts == 2

    @pytest.mark.asyncio
    async def test_coordinator_store_failure_recovers(self, caplog) -> None:
        class FlakyStore(MemoryWebhookOutboxStore):
            def __init__(self) -> None:
                super().__init__()
                self.failed_once = False

            async def list_exhausted_pending(self, **kwargs):
                if not self.failed_once:
                    self.failed_once = True
                    raise RuntimeError("sensitive database detail")
                return await super().list_exhausted_pending(**kwargs)

        outbox = FlakyStore()
        now = datetime.now(timezone.utc)
        await _enqueue_memory_delivery(
            outbox,
            delivery_id="recovery",
            created_at=now,
        )
        client = AsyncMock()
        client.post.return_value = _ok_response()
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            outbox_store=outbox,
            dispatch_concurrency=1,
            dispatch_batch_size=1,
        )

        await dispatcher.startup()
        await asyncio.sleep(0.02)
        dispatcher._wake_event.set()
        await _wait_until(
            lambda: outbox.deliveries["recovery"].status
            is WebhookDeliveryStatus.delivered
        )
        await dispatcher.shutdown(timeout=1)

        assert "error_type=RuntimeError" in caplog.text
        assert "sensitive database detail" not in caplog.text
