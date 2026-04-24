"""Unit tests for webhook/dispatcher.py — retry, backoff, and lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.webhook.dispatcher import HttpWebhookDispatcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _webhook_config(url: str = "https://example.com/hook") -> WebhookConfig:
    return WebhookConfig(url=cast(Any, url), headers={}, timeout=5)


def _ok_response(status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://x"))


def _err_response(status_code: int) -> httpx.Response:
    return httpx.Response(status_code=status_code, request=httpx.Request("POST", "https://x"))


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
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=0)
        cfg = _webhook_config()
        await dispatcher.submit(cfg, {})
        # Let the task run.
        await asyncio.sleep(0.05)
        assert dispatcher.pending_tasks == 0

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
        client = AsyncMock()
        client.post = AsyncMock(return_value=_ok_response(200))
        dispatcher = HttpWebhookDispatcher(client_factory=lambda: client, max_retries=0)
        await dispatcher.shutdown(timeout=1.0)
        await dispatcher.startup()
        await dispatcher.submit(_webhook_config(), {})
        await asyncio.sleep(0.05)
        client.post.assert_awaited_once()


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
