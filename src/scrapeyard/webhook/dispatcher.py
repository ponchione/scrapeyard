"""Webhook dispatcher: Protocol and httpx-based implementation."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from scrapeyard.config.schema import WebhookConfig

logger = logging.getLogger(__name__)

# Defaults for retry behaviour.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 1.0  # seconds
_DEFAULT_BACKOFF_MAX = 30.0  # seconds


class WebhookDispatcher(Protocol):
    """Async interface for dispatching webhook notifications."""

    async def dispatch(self, config: WebhookConfig, payload: dict) -> None: ...
    async def submit(self, config: WebhookConfig, payload: dict) -> None: ...
    async def shutdown(self, timeout: float | None = None) -> None: ...


class HttpWebhookDispatcher:
    """Webhook dispatcher with retry, shared httpx client, and tracked tasks.

    On transient failures (connection errors, timeouts, 5xx responses) the
    dispatcher retries with exponential backoff up to *max_retries* times.
    4xx responses are considered permanent and are **not** retried.
    """

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        *,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        backoff_max: float = _DEFAULT_BACKOFF_MAX,
    ) -> None:
        self._client_factory = client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._accepting_tasks = True
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max

    @property
    def pending_tasks(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    async def startup(self) -> None:
        """Re-enable task submission after a prior shutdown."""
        self._accepting_tasks = True

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff: base * 2^attempt, capped at backoff_max."""
        return float(min(self._backoff_base * (2 ** attempt), self._backoff_max))

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """5xx and 429 are retryable; 4xx (except 429) are permanent."""
        return status_code >= 500 or status_code == 429

    async def dispatch(self, config: WebhookConfig, payload: dict) -> None:
        url = str(config.url)
        logger.debug("Webhook payload for %s: %s", url, payload)

        last_exc: Exception | None = None
        for attempt in range(1 + self._max_retries):
            start = time.monotonic()
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
                        url, response.status_code, elapsed_ms, attempt + 1,
                    )
                    return

                # Permanent client error — don't retry.
                if not self._is_retryable_status(response.status_code):
                    logger.warning(
                        "Webhook to %s returned %d in %.0fms — not retryable",
                        url, response.status_code, elapsed_ms,
                    )
                    return

                logger.warning(
                    "Webhook to %s returned %d in %.0fms (attempt %d/%d)",
                    url, response.status_code, elapsed_ms,
                    attempt + 1, 1 + self._max_retries,
                )

            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                last_exc = exc
                logger.warning(
                    "Webhook to %s failed after %.0fms: %s (attempt %d/%d)",
                    url, elapsed_ms, exc, attempt + 1, 1 + self._max_retries,
                )

            # Sleep before next retry (unless this was the last attempt).
            if attempt < self._max_retries:
                delay = self._backoff_delay(attempt)
                logger.debug(
                    "Webhook retry to %s in %.1fs", url, delay,
                )
                await asyncio.sleep(delay)

        # All attempts exhausted.
        if last_exc:
            logger.error(
                "Webhook to %s failed after %d attempts — giving up: %s",
                url, 1 + self._max_retries, last_exc,
            )
        else:
            logger.error(
                "Webhook to %s returned retryable status after %d attempts — giving up",
                url, 1 + self._max_retries,
            )

    async def submit(self, config: WebhookConfig, payload: dict) -> None:
        """Submit a webhook delivery as a tracked background task."""
        url = str(config.url)
        if not self._accepting_tasks:
            logger.warning("Skipping webhook to %s during shutdown", url)
            return

        task = asyncio.create_task(
            self._run_dispatch(config, payload),
            name=f"scrapeyard-webhook:{url}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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

    async def _run_dispatch(self, config: WebhookConfig, payload: dict) -> None:
        url = str(config.url)
        try:
            await self.dispatch(config, payload)
        except asyncio.CancelledError:
            logger.info("Webhook to %s cancelled during shutdown", url)
            raise
        except Exception:
            logger.exception("Unexpected webhook dispatch failure to %s", url)
