"""Webhook dispatcher: Protocol and httpx-based implementation."""

from __future__ import annotations

import logging
import time
from typing import Protocol

import httpx

from scrapeyard.config.schema import WebhookConfig

logger = logging.getLogger(__name__)


class WebhookDispatcher(Protocol):
    """Async interface for dispatching webhook notifications."""

    async def dispatch(self, config: WebhookConfig, payload: dict) -> None: ...


class HttpWebhookDispatcher:
    """Fire-and-forget webhook dispatcher using httpx.

    Exceptions (timeouts, connection errors, non-2xx responses) are caught
    and logged at WARNING level. Successful dispatches are logged at INFO.
    The full payload is only logged at DEBUG.
    """

    async def dispatch(self, config: WebhookConfig, payload: dict) -> None:
        url = str(config.url)
        logger.debug("Webhook payload for %s: %s", url, payload)

        start = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
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
