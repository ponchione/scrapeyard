"""Single-attempt HTTP execution and Retry-After handling for webhooks."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.engine.url_guard import (
    ResolvedPublicURL,
    UnsafeURLError,
    URLResolutionError,
)
from scrapeyard.webhook.types import (
    WebhookDispatchReason,
    WebhookDispatchResult,
    WebhookDispatchStatus,
    WebhookRequestConfig,
)

logger = logging.getLogger(__name__)

# Receivers' response bodies are read up to this size, undecoded, only so the
# connection can be reused; a longer body closes the connection instead.
_MAX_DRAINED_RESPONSE_BYTES = 64 * 1024


def is_retryable_status(status_code: int) -> bool:
    """5xx and 429 are retryable; other 4xx responses are permanent."""

    return status_code >= 500 or status_code == 429


def safe_payload_context(payload: dict[str, Any]) -> tuple[object, ...]:
    return (
        payload.get("delivery_id"),
        payload.get("job_id"),
        payload.get("run_id"),
        payload.get("event"),
    )


def parse_retry_after(
    response: httpx.Response,
    *,
    now: datetime,
) -> tuple[float | None, str]:
    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return None, "absent"
    if response.status_code not in {429, 503}:
        return None, "inapplicable_status"

    value = raw_value.strip()
    # str.isdigit also accepts digits such as "²" that float() rejects.
    if value.isascii() and value.isdigit():
        delay = float(value)
        if not math.isfinite(delay):
            return None, "out_of_range"
        return delay, "accepted_delta_seconds"

    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None, "malformed"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None, "timezone_missing"
    delay = (parsed.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    if delay <= 0:
        return None, "past_http_date"
    return delay, "accepted_http_date"


async def drain_response(response: httpx.Response) -> None:
    """Read a small body so the connection returns to the client's pool."""

    if response.is_stream_consumed:
        return
    received = 0
    async for chunk in response.aiter_raw():
        received += len(chunk)
        if received > _MAX_DRAINED_RESPONSE_BYTES:
            return


async def send_webhook_request(
    config: WebhookConfig | WebhookRequestConfig,
    payload: dict[str, Any],
    *,
    acquire_client: Callable[[str], Awaitable[httpx.AsyncClient]],
    release_client: Callable[[str, httpx.AsyncClient], Awaitable[None]],
    resolve_url: Callable[[str], ResolvedPublicURL],
    now_fn: Callable[[], datetime],
) -> WebhookDispatchResult:
    """Perform one safety-checked HTTP POST attempt without retrying."""

    url = str(config.url)
    start = time.monotonic()
    acquired_hostname: str | None = None
    client: httpx.AsyncClient | None = None
    try:
        resolved = await asyncio.to_thread(resolve_url, url)
        acquired_hostname = resolved.sni_hostname
        client = await acquire_client(acquired_hostname)
        headers = dict(config.headers)
        headers["Host"] = resolved.host_header
        request = httpx.Request(
            "POST",
            resolved.connect_url,
            json=payload,
            headers=headers,
            extensions={
                "sni_hostname": resolved.sni_hostname,
                "timeout": httpx.Timeout(config.timeout).as_dict(),
            },
        )
        sent_at = time.monotonic()
        response = await asyncio.wait_for(
            client.send(
                request,
                stream=True,
                follow_redirects=False,
            ),
            timeout=config.timeout,
        )
        try:
            remaining = config.timeout - (time.monotonic() - sent_at)
            if remaining > 0:
                with suppress(TimeoutError, httpx.HTTPError):
                    await asyncio.wait_for(drain_response(response), timeout=remaining)
            elapsed_ms = (time.monotonic() - start) * 1000
            return _classify_response(
                response,
                payload=payload,
                elapsed_ms=elapsed_ms,
                now=now_fn(),
            )
        finally:
            await response.aclose()

    except UnsafeURLError:
        logger.warning(
            "Webhook attempt blocked outcome=permanent_failed reason_code=%s",
            WebhookDispatchReason.unsafe_url.value,
        )
        return WebhookDispatchResult(
            WebhookDispatchStatus.permanent_failed,
            1,
            "Webhook URL is unsafe or non-public",
            WebhookDispatchReason.unsafe_url,
        )
    except (
        URLResolutionError,
        # The total deadline.
        TimeoutError,
        httpx.TimeoutException,
        httpx.ConnectError,
        httpx.HTTPError,
    ) as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        error_type = type(exc).__name__
        logger.warning(
            "Webhook HTTP attempt failed elapsed_ms=%.0f error_type=%s "
            "outcome=retryable_failed reason_code=%s",
            elapsed_ms,
            error_type,
            WebhookDispatchReason.transport_failure.value,
        )
        return WebhookDispatchResult(
            WebhookDispatchStatus.retryable_failed,
            1,
            f"Transport failure: {error_type}",
            WebhookDispatchReason.transport_failure,
        )
    finally:
        if acquired_hostname is not None and client is not None:
            await release_client(acquired_hostname, client)


def _classify_response(
    response: httpx.Response,
    *,
    payload: dict[str, Any],
    elapsed_ms: float,
    now: datetime,
) -> WebhookDispatchResult:
    if response.is_success:
        logger.info(
            "Webhook HTTP attempt completed status_code=%s "
            "elapsed_ms=%.0f outcome=delivered",
            response.status_code,
            elapsed_ms,
        )
        return WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)

    last_error = f"HTTP {response.status_code}"
    if not is_retryable_status(response.status_code):
        logger.warning(
            "Webhook HTTP attempt completed status_code=%s "
            "elapsed_ms=%.0f outcome=permanent_failed reason_code=%s",
            response.status_code,
            elapsed_ms,
            WebhookDispatchReason.permanent_http_response.value,
        )
        return WebhookDispatchResult(
            WebhookDispatchStatus.permanent_failed,
            1,
            last_error,
            WebhookDispatchReason.permanent_http_response,
        )

    retry_after, retry_after_action = parse_retry_after(response, now=now)
    if retry_after_action != "absent":
        delivery_id, job_id, run_id, event = safe_payload_context(payload)
        logger.info(
            "Webhook Retry-After evaluated delivery_id=%s job_id=%s "
            "run_id=%s event=%s status_code=%s accepted=%s "
            "reason_code=%s recovery_action=%s",
            delivery_id,
            job_id,
            run_id,
            event,
            response.status_code,
            retry_after is not None,
            WebhookDispatchReason.retryable_http_response.value,
            retry_after_action,
        )
    logger.warning(
        "Webhook HTTP attempt completed status_code=%s elapsed_ms=%.0f "
        "outcome=retryable_failed reason_code=%s",
        response.status_code,
        elapsed_ms,
        WebhookDispatchReason.retryable_http_response.value,
    )
    return WebhookDispatchResult(
        WebhookDispatchStatus.retryable_failed,
        1,
        last_error,
        WebhookDispatchReason.retryable_http_response,
        retry_after,
    )
