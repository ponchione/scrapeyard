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


class WebhookDispatcher(Protocol):
    """Async interface for dispatching webhook notifications."""

    async def dispatch(self, config: WebhookConfig, payload: dict) -> None: ...
    async def submit(self, config: WebhookConfig, payload: dict) -> None: ...
    async def shutdown(self, timeout: float | None = None) -> None: ...


class HttpWebhookDispatcher:
    """Webhook dispatcher with a shared httpx client and tracked tasks.

    Exceptions (timeouts, connection errors, non-2xx responses) are caught
    and logged at WARNING level. Successful dispatches are logged at INFO.
    The full payload is only logged at DEBUG.
    """

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._client_factory = client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._accepting_tasks = True

    @property
    def pending_tasks(self) -> int:
        return sum(1 for task in self._tasks if not task.done())

    async def startup(self) -> None:
        """Re-enable task submission after a prior shutdown."""
        self._accepting_tasks = True

    async def dispatch(self, config: WebhookConfig, payload: dict) -> None:
        url = str(config.url)
        logger.debug("Webhook payload for %s: %s", url, payload)

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
                    "Webhook dispatched to %s — %d in %.0fms",
                    url, response.status_code, elapsed_ms,
                )
            else:
                logger.warning(
                    "Webhook to %s returned %d in %.0fms",
                    url, response.status_code, elapsed_ms,
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.warning(
                "Webhook to %s failed after %.0fms: %s", url, elapsed_ms, exc,
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
