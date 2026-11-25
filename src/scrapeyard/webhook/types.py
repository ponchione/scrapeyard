"""Shared types and protocols for webhook dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from scrapeyard.storage.webhook_outbox import (
    WebhookDelivery,
    WebhookDeliveryHeader,
    WebhookFailureReason,
)

QueuedWebhookDelivery = WebhookDelivery | WebhookDeliveryHeader


@dataclass(frozen=True, slots=True)
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


class WebhookDispatchReason(str, Enum):
    """Sanitized reason codes for one HTTP attempt."""

    retryable_http_response = "retryable_http_response"
    transport_failure = "transport_failure"
    permanent_http_response = WebhookFailureReason.permanent_http_response.value
    unsafe_url = WebhookFailureReason.unsafe_url.value
    non_retryable_failure = WebhookFailureReason.non_retryable_failure.value


@dataclass(frozen=True, slots=True)
class WebhookDispatchResult:
    """Result from attempting to deliver a webhook."""

    status: WebhookDispatchStatus
    attempts: int
    last_error: str | None = None
    reason_code: WebhookDispatchReason | None = None
    retry_after_seconds: float | None = None


class WebhookNotifier(Protocol):
    """Worker-facing interface for waking already-durable webhook intents."""

    async def notify(self) -> None:
        """Wake processing for an intent already committed in jobs.db."""
        ...
