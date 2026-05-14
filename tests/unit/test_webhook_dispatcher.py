"""Unit tests for webhook/dispatcher.py — retry, backoff, and lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.storage.webhook_outbox import (
    WebhookDelivery,
    WebhookDeliveryCreate,
    WebhookDeliveryStatus,
)
from scrapeyard.webhook.dispatcher import (
    HttpWebhookDispatcher,
    WebhookDispatchStatus,
    WebhookRequestConfig,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
            last_error=None,
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

    async def get_delivery(self, delivery_id: str) -> WebhookDelivery | None:
        return self.deliveries.get(delivery_id)

    async def mark_delivered(
        self,
        delivery_id: str,
        *,
        delivered_at: datetime,
        attempts: int = 1,
    ) -> None:
        delivery = self.deliveries[delivery_id]
        self.deliveries[delivery_id] = replace(
            delivery,
            status=WebhookDeliveryStatus.delivered,
            attempts=delivery.attempts + attempts,
            last_attempt_at=delivered_at,
            delivered_at=delivered_at,
            last_error=None,
            updated_at=delivered_at,
        )

    async def mark_retryable_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        next_attempt_at: datetime,
        last_error: str,
        attempts: int = 1,
    ) -> None:
        delivery = self.deliveries[delivery_id]
        self.deliveries[delivery_id] = replace(
            delivery,
            status=WebhookDeliveryStatus.pending,
            attempts=delivery.attempts + attempts,
            last_attempt_at=attempted_at,
            next_attempt_at=next_attempt_at,
            last_error=last_error,
            updated_at=attempted_at,
        )

    async def mark_permanent_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        last_error: str,
        attempts: int = 1,
    ) -> None:
        delivery = self.deliveries[delivery_id]
        self.deliveries[delivery_id] = replace(
            delivery,
            status=WebhookDeliveryStatus.failed,
            attempts=delivery.attempts + attempts,
            last_attempt_at=attempted_at,
            last_error=last_error,
            updated_at=attempted_at,
        )


async def _wait_until(condition) -> None:
    for _ in range(50):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not met")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDispatchSuccess:
    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self) -> None:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=3)
        cfg = _webhook_config()
        with patch("scrapeyard.webhook.dispatcher.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await dispatcher.dispatch(cfg, {"key": "val"})
        client.post.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_after_transient_5xx(self) -> None:
        """Retry on 503, succeed on second attempt."""
        client = AsyncMock()
        client.post = AsyncMock(
            side_effect=[_err_response(503), _ok_response(200)],
        )
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=3)
        cfg = _webhook_config()
        with patch("scrapeyard.webhook.dispatcher.asyncio.sleep", new_callable=AsyncMock):
            await dispatcher.dispatch(cfg, {})
        assert client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_success_after_timeout_exception(self) -> None:
        """Retry on httpx.TimeoutException, succeed on second attempt."""
        client = AsyncMock()
        client.post = AsyncMock(
            side_effect=[httpx.TimeoutException("timed out"), _ok_response(200)],
        )
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=2)
        cfg = _webhook_config()
        with patch("scrapeyard.webhook.dispatcher.asyncio.sleep", new_callable=AsyncMock):
            await dispatcher.dispatch(cfg, {})
        assert client.post.await_count == 2


class TestDispatchRetryExhaustion:
    @pytest.mark.asyncio
    async def test_all_attempts_fail(self) -> None:
        """After max_retries, dispatch gives up (no exception raised)."""
        client = AsyncMock()
        client.post = AsyncMock(
            side_effect=httpx.ConnectError("refused"),
        )
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=2)
        cfg = _webhook_config()
        with patch("scrapeyard.webhook.dispatcher.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await dispatcher.dispatch(cfg, {})
        # 1 initial + 2 retries = 3 attempts.
        assert client.post.await_count == 3
        # Sleep called between each retry (2 times).
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_all_5xx_exhausted(self) -> None:
        """Server errors exhaust all retries."""
        client = AsyncMock()
        client.post = AsyncMock(return_value=_err_response(500))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=1)
        cfg = _webhook_config()
        with patch("scrapeyard.webhook.dispatcher.asyncio.sleep", new_callable=AsyncMock):
            await dispatcher.dispatch(cfg, {})
        assert client.post.await_count == 2  # 1 + 1 retry

    @pytest.mark.asyncio
    async def test_http_error_redacts_url_userinfo_in_last_error(self) -> None:
        client = AsyncMock()
        client.post = AsyncMock(
            side_effect=httpx.ConnectError("failed https://user:pass@example.com/hook")
        )
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=0)

        result = await dispatcher.dispatch(
            _webhook_config("https://user:pass@example.com/hook"),
            {},
        )

        assert result.last_error is not None
        assert "user:pass" not in result.last_error
        assert "https://example.com/hook" in result.last_error


class TestNonRetryableStatus:
    @pytest.mark.asyncio
    async def test_4xx_not_retried(self) -> None:
        """Client errors (4xx except 429) are permanent — no retry."""
        client = AsyncMock()
        client.post = AsyncMock(return_value=_err_response(404))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=3)
        cfg = _webhook_config()
        with patch("scrapeyard.webhook.dispatcher.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await dispatcher.dispatch(cfg, {})
        client.post.assert_awaited_once()
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_429_is_retried(self) -> None:
        """429 Too Many Requests is retryable."""
        client = AsyncMock()
        client.post = AsyncMock(
            side_effect=[_err_response(429), _ok_response(200)],
        )
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=2)
        cfg = _webhook_config()
        with patch("scrapeyard.webhook.dispatcher.asyncio.sleep", new_callable=AsyncMock):
            await dispatcher.dispatch(cfg, {})
        assert client.post.await_count == 2

    @pytest.mark.asyncio
    async def test_send_once_blocks_non_public_persisted_urls(self) -> None:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=0)

        result = await dispatcher.send_once(
            WebhookRequestConfig(
                url="http://127.0.0.1/hook",
                headers={},
                timeout=5,
            ),
            {},
        )

        assert result.status is WebhookDispatchStatus.permanent_failed
        assert "non-public" in (result.last_error or "")
        client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_once_disables_http_redirect_following(self) -> None:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=0)

        result = await dispatcher.send_once(_webhook_config(), {})

        assert result.status is WebhookDispatchStatus.delivered
        assert client.post.await_args.kwargs["follow_redirects"] is False


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


class TestSubmitAndShutdown:
    @pytest.mark.asyncio
    async def test_submit_creates_tracked_task(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            max_retries=0,
            outbox_store=outbox,
        )
        cfg = _webhook_config()
        await dispatcher.submit(cfg, _payload())
        # Let the task run.
        await asyncio.sleep(0.05)
        assert dispatcher.pending_tasks == 0
        assert outbox.deliveries["delivery-1"].status is WebhookDeliveryStatus.delivered

    @pytest.mark.asyncio
    async def test_submit_rejected_during_shutdown(self) -> None:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=0)
        await dispatcher.shutdown(timeout=1.0)
        # After shutdown, submit should be silently skipped.
        await dispatcher.submit(_webhook_config(), {})
        client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_startup_re_enables_submit(self) -> None:
        outbox = MemoryWebhookOutboxStore()
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(
            client_factory=lambda: client,
            max_retries=0,
            outbox_store=outbox,
        )
        await dispatcher.shutdown(timeout=1.0)
        await dispatcher.startup()
        await dispatcher.submit(_webhook_config(), _payload())
        await asyncio.sleep(0.05)
        client.post.assert_awaited_once()


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
            max_retries=0,
            outbox_store=outbox,
        )

        await dispatcher.submit(_webhook_config(), _payload())

        persisted = outbox.deliveries["delivery-1"]
        assert persisted.status is WebhookDeliveryStatus.pending
        assert persisted.payload["delivery_id"] == "delivery-1"
        release_post.set()
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
            max_retries=0,
            outbox_store=outbox,
        )

        await dispatcher.startup()
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
            max_retries=0,
            backoff_base=60.0,
            outbox_store=outbox,
        )

        await dispatcher.submit(_webhook_config(), _payload())
        await _wait_until(lambda: outbox.deliveries["delivery-1"].attempts == 1)

        delivery = outbox.deliveries["delivery-1"]
        assert delivery.status is WebhookDeliveryStatus.pending
        assert delivery.last_error is not None
        assert "timed out" in delivery.last_error
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
            max_retries=0,
            outbox_store=outbox,
        )

        await dispatcher.submit(_webhook_config(), _payload())
        await dispatcher.shutdown(timeout=1.0)

        delivery = outbox.deliveries["delivery-1"]
        assert delivery.status is WebhookDeliveryStatus.failed
        assert delivery.attempts == 1
        assert delivery.last_error == "HTTP 404"

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
            max_retries=0,
            outbox_store=outbox,
        )

        await dispatcher.startup()
        await dispatcher.shutdown(timeout=1.0)

        client.post.assert_not_awaited()
        delivery = outbox.deliveries["delivery-1"]
        assert delivery.status is WebhookDeliveryStatus.failed
        assert "non-public" in (delivery.last_error or "")

    @pytest.mark.asyncio
    async def test_sleep_until_due_treats_naive_persisted_time_as_utc(self, monkeypatch) -> None:
        dispatcher = HttpWebhookDispatcher(outbox_store=MemoryWebhookOutboxStore())
        now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
        sleep = AsyncMock()
        monkeypatch.setattr("scrapeyard.webhook.dispatcher.utc_now", lambda: now)
        monkeypatch.setattr("scrapeyard.webhook.dispatcher.asyncio.sleep", sleep)

        await dispatcher._sleep_until_due(datetime(2026, 4, 24, 12, 1))

        sleep.assert_awaited_once_with(60.0)


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
        assert len(payload["delivery_id"]) == 32  # uuid4 hex

    def test_delivery_ids_are_unique(self) -> None:
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
        assert p1["delivery_id"] != p2["delivery_id"]
