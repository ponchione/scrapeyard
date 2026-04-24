"""Webhook dispatcher: Protocol and httpx-based implementation."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

import httpx

from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import WebhookConfig
from scrapeyard.storage.protocols import WebhookOutboxStore
from scrapeyard.storage.webhook_outbox import WebhookDelivery, WebhookDeliveryCreate, WebhookDeliveryStatus

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0  # seconds
_DEFAULT_BACKOFF_MAX = 30.0  # seconds


@dataclass(frozen=True)
class WebhookRequestConfig:
    """Minimal webhook request settings used for persisted deliveries."""

    url: str
    headers: dict[str, str]
    timeout: float


class WebhookDispatchStatus(str, Enum):
    """Outcome of one dispatcher retry cycle."""

    delivered = "delivered"
    retryable_failed = "retryable_failed"
    permanent_failed = "permanent_failed"


@dataclass(frozen=True)
class WebhookDispatchResult:
    """Result from attempting to deliver a webhook."""

    status: WebhookDispatchStatus
    attempts: int
    last_error: str | None = None


class WebhookDispatcher(Protocol):
    """Async interface for dispatching webhook notifications."""

    async def dispatch(
        self,
        config: WebhookConfig | WebhookRequestConfig,
        payload: dict[str, Any],
    ) -> WebhookDispatchResult | None: ...
    async def submit(self, config: WebhookConfig, payload: dict[str, Any]) -> None: ...
    async def shutdown(self, timeout: float | None = None) -> None: ...


class HttpWebhookDispatcher:
    """Webhook dispatcher with retry, shared httpx client, tracked tasks, and optional outbox.

    On transient failures (connection errors, timeouts, 5xx responses) the
    dispatcher retries with exponential backoff up to *max_retries* times.
    4xx responses are considered permanent and are **not** retried. When an
    outbox store is provided, submit() durably persists the delivery before
    scheduling the background HTTP attempt and replays pending deliveries on
    startup.
    """

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        backoff_max: float = _DEFAULT_BACKOFF_MAX,
        outbox_store: WebhookOutboxStore | None = None,
    ) -> None:
        self._client_factory = client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._delivery_tasks: dict[str, asyncio.Task[None]] = {}
        self._accepting_tasks = True
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._outbox_store = outbox_store

    @property
    def pending_tasks(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    async def startup(self) -> None:
        """Re-enable task submission and replay pending outbox rows."""
        self._accepting_tasks = True
        if self._outbox_store is None:
            return
        for delivery in await self._outbox_store.list_pending():
            self._schedule_persisted_delivery(delivery)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff: base * 2^attempt, capped at backoff_max."""
        return float(min(self._backoff_base * (2 ** attempt), self._backoff_max))

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """5xx and 429 are retryable; 4xx (except 429) are permanent."""
        return status_code >= 500 or status_code == 429

    async def dispatch(
        self,
        config: WebhookConfig | WebhookRequestConfig,
        payload: dict[str, Any],
    ) -> WebhookDispatchResult:
        url = str(config.url)
        logger.debug("Webhook payload for %s: %s", url, payload)

        last_error: str | None = None
        for attempt in range(1 + self._max_retries):
            start = time.monotonic()
            attempts_so_far = attempt + 1
            try:
                client = await self._get_client()
                response = await client.post(
                    url,
                    json=payload,
                    headers=config.headers,
                    timeout=config.timeout,
                )
                elapsed_ms = (time.monotonic() - start) * 1000

                if response.is_success:
                    logger.info(
                        "Webhook dispatched to %s — %d in %.0fms (attempt %d)",
                        url, response.status_code, elapsed_ms, attempts_so_far,
                    )
                    return WebhookDispatchResult(WebhookDispatchStatus.delivered, attempts_so_far)

                last_error = f"HTTP {response.status_code}"
                if not self._is_retryable_status(response.status_code):
                    logger.warning(
                        "Webhook to %s returned %d in %.0fms — not retryable",
                        url, response.status_code, elapsed_ms,
                    )
                    return WebhookDispatchResult(
                        WebhookDispatchStatus.permanent_failed,
                        attempts_so_far,
                        last_error,
                    )

                logger.warning(
                    "Webhook to %s returned %d in %.0fms (attempt %d/%d)",
                    url, response.status_code, elapsed_ms,
                    attempts_so_far, 1 + self._max_retries,
                )

            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                last_error = str(exc) or exc.__class__.__name__
                logger.warning(
                    "Webhook to %s failed after %.0fms: %s (attempt %d/%d)",
                    url, elapsed_ms, exc, attempts_so_far, 1 + self._max_retries,
                )

            if attempt < self._max_retries:
                delay = self._backoff_delay(attempt)
                logger.debug(
                    "Webhook retry to %s in %.1fs", url, delay,
                )
                await asyncio.sleep(delay)

        logger.error(
            "Webhook to %s failed after %d attempts — leaving delivery retryable: %s",
            url, 1 + self._max_retries, last_error,
        )
        return WebhookDispatchResult(
            WebhookDispatchStatus.retryable_failed,
            1 + self._max_retries,
            last_error,
        )

    async def submit(self, config: WebhookConfig, payload: dict[str, Any]) -> None:
        """Submit a webhook delivery as a tracked background task.

        With an outbox store configured, the delivery is durably inserted before
        this method schedules the HTTP task or returns to its caller.
        """
        url = str(config.url)
        if not self._accepting_tasks:
            logger.warning("Skipping webhook to %s during shutdown", url)
            return

        if self._outbox_store is None:
            task = asyncio.create_task(
                self._run_dispatch(config, payload),
                name=f"scrapeyard-webhook:{url}",
            )
            self._track_task(task)
            return

        now = utc_now()
        delivery = self._build_delivery(config, payload, now)
        await self._outbox_store.enqueue_delivery(delivery, now=now)
        persisted = await self._outbox_store.get_delivery(delivery.delivery_id)
        if persisted is not None and persisted.status is WebhookDeliveryStatus.pending:
            self._schedule_persisted_delivery(persisted)

    async def shutdown(self, timeout: float | None = None) -> None:
        """Drain pending webhook tasks and close the shared HTTP client."""
        self._accepting_tasks = False
        pending = [task for task in self._tasks if not task.done()]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Cancelling %d pending webhook task(s) after %.1fs shutdown grace",
                    len([task for task in pending if not task.done()]),
                    timeout or 0.0,
                )
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        async with self._client_lock:
            client = self._client
            self._client = None

        if client is not None:
            await client.aclose()

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _schedule_persisted_delivery(self, delivery: WebhookDelivery) -> None:
        if not self._accepting_tasks:
            return
        existing = self._delivery_tasks.get(delivery.delivery_id)
        if existing is not None and not existing.done():
            return

        task = asyncio.create_task(
            self._run_persisted_delivery(delivery),
            name=f"scrapeyard-webhook:{delivery.url}",
        )
        self._tasks.add(task)
        self._delivery_tasks[delivery.delivery_id] = task

        def _discard(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            if self._delivery_tasks.get(delivery.delivery_id) is completed:
                self._delivery_tasks.pop(delivery.delivery_id, None)

        task.add_done_callback(_discard)

    def _build_delivery(
        self,
        config: WebhookConfig,
        payload: dict[str, Any],
        now: datetime,
    ) -> WebhookDeliveryCreate:
        persisted_payload = dict(payload)
        delivery_id = str(persisted_payload.get("delivery_id") or uuid.uuid4().hex)
        persisted_payload["delivery_id"] = delivery_id
        run_id = persisted_payload.get("run_id")
        return WebhookDeliveryCreate(
            delivery_id=delivery_id,
            job_id=str(persisted_payload.get("job_id") or ""),
            run_id=None if run_id is None else str(run_id),
            event=str(persisted_payload.get("event") or "webhook"),
            url=str(config.url),
            headers=dict(config.headers),
            timeout_seconds=float(config.timeout),
            payload=persisted_payload,
            next_attempt_at=now,
        )

    async def _run_dispatch(self, config: WebhookConfig, payload: dict[str, Any]) -> None:
        url = str(config.url)
        try:
            await self.dispatch(config, payload)
        except asyncio.CancelledError:
            logger.info("Webhook to %s cancelled during shutdown", url)
            raise
        except Exception:
            logger.exception("Unexpected webhook dispatch failure to %s", url)

    async def _run_persisted_delivery(self, delivery: WebhookDelivery) -> None:
        if self._outbox_store is None:
            return

        current = delivery
        while current.status is WebhookDeliveryStatus.pending:
            await self._sleep_until_due(current.next_attempt_at)
            request_config = WebhookRequestConfig(
                url=current.url,
                headers=current.headers,
                timeout=current.timeout_seconds,
            )
            try:
                result = await self.dispatch(request_config, current.payload)
            except asyncio.CancelledError:
                logger.info("Webhook to %s cancelled during shutdown", current.url)
                raise
            except Exception as exc:
                logger.exception("Unexpected webhook dispatch failure to %s", current.url)
                result = WebhookDispatchResult(
                    WebhookDispatchStatus.retryable_failed,
                    1,
                    str(exc) or exc.__class__.__name__,
                )
            if result is None:
                result = WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)

            attempted_at = utc_now()
            if result.status is WebhookDispatchStatus.delivered:
                await self._outbox_store.mark_delivered(
                    current.delivery_id,
                    delivered_at=attempted_at,
                    attempts=result.attempts,
                )
                return

            if result.status is WebhookDispatchStatus.permanent_failed:
                await self._outbox_store.mark_permanent_failure(
                    current.delivery_id,
                    attempted_at=attempted_at,
                    last_error=result.last_error or "permanent webhook failure",
                    attempts=result.attempts,
                )
                return

            retry_at = attempted_at + timedelta(
                seconds=self._persistent_retry_delay(current, result)
            )
            await self._outbox_store.mark_retryable_failure(
                current.delivery_id,
                attempted_at=attempted_at,
                next_attempt_at=retry_at,
                last_error=result.last_error or "retryable webhook failure",
                attempts=result.attempts,
            )
            if not self._accepting_tasks:
                return
            refreshed = await self._outbox_store.get_delivery(current.delivery_id)
            if refreshed is None:
                return
            current = refreshed

    async def _sleep_until_due(self, next_attempt_at: datetime) -> None:
        delay = (next_attempt_at - utc_now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

    def _persistent_retry_delay(
        self,
        delivery: WebhookDelivery,
        result: WebhookDispatchResult,
    ) -> float:
        attempt_index = max(delivery.attempts + result.attempts - 1, 0)
        return self._backoff_delay(attempt_index)
