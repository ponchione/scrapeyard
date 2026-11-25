"""Outbox attempt outcome persistence, exhaustion checks, and dead-letter logging."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from scrapeyard.runtime.metrics import RETRIES, WEBHOOK_DELIVERIES, mark_last_success
from scrapeyard.storage.protocols import WebhookOutboxStore
from scrapeyard.storage.webhook_outbox import WebhookFailureReason
from scrapeyard.webhook.types import (
    QueuedWebhookDelivery,
    WebhookDispatchReason,
    WebhookDispatchResult,
    WebhookDispatchStatus,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry, backoff, and lifetime limits for persisted webhook deliveries."""

    backoff_base: float
    backoff_max: float
    max_attempts: int
    max_age_seconds: int

    def backoff_delay(self, attempt_index: int) -> float:
        """Return capped exponential delay for a zero-based attempt index."""

        # Past 2**62 the delay is the cap anyway, and a larger power overflows a float.
        return float(min(self.backoff_base * (2 ** min(attempt_index, 62)), self.backoff_max))

    def delivery_deadline(self, delivery: QueuedWebhookDelivery) -> datetime:
        return as_utc(delivery.created_at) + timedelta(seconds=self.max_age_seconds)

    def delivery_age_seconds(self, delivery: QueuedWebhookDelivery, now: datetime) -> float:
        return max(0.0, (as_utc(now) - as_utc(delivery.created_at)).total_seconds())


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def terminal_reason(reason: WebhookDispatchReason | None) -> WebhookFailureReason:
    if reason is WebhookDispatchReason.permanent_http_response:
        return WebhookFailureReason.permanent_http_response
    if reason is WebhookDispatchReason.unsafe_url:
        return WebhookFailureReason.unsafe_url
    return WebhookFailureReason.non_retryable_failure


async def log_startup_backlog(
    outbox_store: WebhookOutboxStore,
    *,
    dispatch_concurrency: int,
    dispatch_batch_size: int,
) -> None:
    try:
        summary = await outbox_store.summarize()
    except Exception as exc:
        logger.error(
            "Webhook dispatcher startup summary unavailable "
            "error_type=%s recovery_action=coordinator_retry",
            type(exc).__name__,
        )
        return
    logger.info(
        "Webhook dispatcher startup backlog "
        "pending_count=%s delivered_count=%s failed_count=%s "
        "oldest_pending_age_seconds=%s pending_attempts_min=%s "
        "pending_attempts_max=%s pending_attempts_total=%s "
        "dispatch_concurrency=%s dispatch_batch_size=%s "
        "recovery_action=start_bounded_workers",
        summary.pending,
        summary.delivered,
        summary.failed,
        summary.oldest_pending_age_seconds,
        summary.pending_attempts_min,
        summary.pending_attempts_max,
        summary.pending_attempts_total,
        dispatch_concurrency,
        dispatch_batch_size,
    )


def log_dead_letter(
    delivery: QueuedWebhookDelivery,
    failed_at: datetime,
    reason: WebhookFailureReason,
    policy: RetryPolicy,
) -> None:
    level = logger.warning
    message = "Webhook permanent failure/dead-letter"
    if reason is WebhookFailureReason.attempt_exhausted:
        level = logger.error
        message = "Webhook attempt exhaustion"
    elif reason is WebhookFailureReason.age_exhausted:
        level = logger.error
        message = "Webhook age exhaustion"
    level(
        "%s delivery_id=%s job_id=%s run_id=%s event=%s "
        "attempt_count=%s max_attempts=%s delivery_age_seconds=%.3f "
        "max_age_seconds=%s reason_code=%s "
        "recovery_action=retain_dead_letter",
        message,
        delivery.delivery_id,
        delivery.job_id,
        delivery.run_id,
        delivery.event,
        delivery.attempts,
        policy.max_attempts,
        policy.delivery_age_seconds(delivery, failed_at),
        policy.max_age_seconds,
        reason.value,
    )


def log_delivery_event(
    level: Callable[..., None],
    message: str,
    delivery: QueuedWebhookDelivery,
    *,
    now: datetime,
    policy: RetryPolicy,
    reason_code: str,
    recovery_action: str,
    extra_fmt: str = "",
    extra_args: tuple[object, ...] = (),
) -> None:
    suffix = f" {extra_fmt}" if extra_fmt else ""
    level(
        "%s delivery_id=%s job_id=%s run_id=%s event=%s "
        "attempt_count=%s max_attempts=%s delivery_age_seconds=%.3f "
        "max_age_seconds=%s reason_code=%s recovery_action=%s" + suffix,
        message,
        delivery.delivery_id,
        delivery.job_id,
        delivery.run_id,
        delivery.event,
        delivery.attempts,
        policy.max_attempts,
        policy.delivery_age_seconds(delivery, now),
        policy.max_age_seconds,
        reason_code,
        recovery_action,
        *extra_args,
    )


async def fail_delivery(
    outbox_store: WebhookOutboxStore,
    delivery: QueuedWebhookDelivery,
    *,
    failed_at: datetime,
    reason: WebhookFailureReason,
    last_error: str,
    policy: RetryPolicy,
) -> None:
    transitioned = await outbox_store.mark_failed(
        delivery.delivery_id,
        failed_at=failed_at,
        reason=reason,
        last_error=last_error,
        expected_attempts=delivery.attempts,
    )
    if transitioned:
        log_dead_letter(delivery, failed_at, reason, policy)


async def fail_if_exhausted(
    outbox_store: WebhookOutboxStore,
    delivery: QueuedWebhookDelivery,
    *,
    now: datetime,
    policy: RetryPolicy,
) -> bool:
    reason: WebhookFailureReason | None = None
    error = "Webhook delivery exhausted"
    if delivery.attempts >= policy.max_attempts:
        reason = WebhookFailureReason.attempt_exhausted
        error = "Maximum total webhook delivery attempts reached"
    elif as_utc(now) >= policy.delivery_deadline(delivery):
        reason = WebhookFailureReason.age_exhausted
        error = "Maximum webhook delivery age reached"
    if reason is None:
        return False
    await fail_delivery(
        outbox_store,
        delivery,
        failed_at=now,
        reason=reason,
        last_error=error,
        policy=policy,
    )
    return True


async def persist_attempt_outcome(
    outbox_store: WebhookOutboxStore,
    reserved: QueuedWebhookDelivery,
    result: WebhookDispatchResult,
    *,
    policy: RetryPolicy,
    now_fn: Callable[[], datetime],
) -> None:
    completed_at = now_fn()
    reason_code = None if result.reason_code is None else result.reason_code.value
    logger.info(
        "Webhook attempt completed delivery_id=%s job_id=%s run_id=%s "
        "event=%s attempt_count=%s max_attempts=%s "
        "delivery_age_seconds=%.3f max_age_seconds=%s "
        "outcome=%s reason_code=%s recovery_action=persist_outcome",
        reserved.delivery_id,
        reserved.job_id,
        reserved.run_id,
        reserved.event,
        reserved.attempts,
        policy.max_attempts,
        policy.delivery_age_seconds(reserved, completed_at),
        policy.max_age_seconds,
        result.status.value,
        reason_code,
    )
    WEBHOOK_DELIVERIES.labels(result.status.value).inc()

    if result.status is WebhookDispatchStatus.delivered:
        await outbox_store.mark_delivered(
            reserved.delivery_id,
            delivered_at=completed_at,
            expected_attempts=reserved.attempts,
        )
        mark_last_success("webhook")
        return

    if result.status is WebhookDispatchStatus.permanent_failed:
        reason = terminal_reason(result.reason_code)
        await outbox_store.mark_failed(
            reserved.delivery_id,
            failed_at=completed_at,
            reason=reason,
            last_error=result.last_error or "Non-retryable webhook failure",
            expected_attempts=reserved.attempts,
        )
        log_dead_letter(reserved, completed_at, reason, policy)
        return

    if reserved.attempts >= policy.max_attempts:
        await fail_delivery(
            outbox_store,
            reserved,
            failed_at=completed_at,
            reason=WebhookFailureReason.attempt_exhausted,
            last_error="Maximum total webhook delivery attempts reached",
            policy=policy,
        )
        return

    deadline = policy.delivery_deadline(reserved)
    if as_utc(completed_at) >= deadline:
        await fail_delivery(
            outbox_store,
            reserved,
            failed_at=completed_at,
            reason=WebhookFailureReason.age_exhausted,
            last_error="Maximum webhook delivery age reached after retryable failure",
            policy=policy,
        )
        return

    delay = max(
        policy.backoff_delay(max(reserved.attempts - 1, 0)),
        result.retry_after_seconds or 0.0,
    )
    remaining_seconds = (deadline - as_utc(completed_at)).total_seconds()
    if delay >= remaining_seconds:
        await fail_delivery(
            outbox_store,
            reserved,
            failed_at=completed_at,
            reason=WebhookFailureReason.age_exhausted,
            last_error="Next retry cannot occur before maximum delivery age",
            policy=policy,
        )
        return
    retry_at = completed_at + timedelta(seconds=delay)

    await outbox_store.mark_retryable_failure(
        reserved.delivery_id,
        attempted_at=completed_at,
        next_attempt_at=retry_at,
        last_error=result.last_error or "Retryable webhook delivery failure",
        expected_attempts=reserved.attempts,
    )
    RETRIES.labels("webhook", "scheduled").inc()
    logger.info(
        "Webhook retry scheduled delivery_id=%s job_id=%s run_id=%s "
        "event=%s attempt_count=%s max_attempts=%s "
        "delivery_age_seconds=%.3f max_age_seconds=%s "
        "reason_code=%s retry_delay_seconds=%.3f "
        "recovery_action=wait_for_due_time",
        reserved.delivery_id,
        reserved.job_id,
        reserved.run_id,
        reserved.event,
        reserved.attempts,
        policy.max_attempts,
        policy.delivery_age_seconds(reserved, completed_at),
        policy.max_age_seconds,
        reason_code,
        delay,
    )
