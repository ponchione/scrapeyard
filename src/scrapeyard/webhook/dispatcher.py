"""Webhook dispatcher: bounded durable workers and httpx delivery."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Any, Protocol

import httpx

from scrapeyard.common.time import utc_now
from scrapeyard.common.async_tools import MonotonicDeadline
from scrapeyard.config.schema import WebhookConfig
from scrapeyard.engine.url_guard import UnsafeURLError, resolve_public_url
from scrapeyard.runtime.metrics import RETRIES, WEBHOOK_DELIVERIES, mark_last_success
from scrapeyard.storage.protocols import WebhookOutboxStore
from scrapeyard.storage.webhook_outbox import WebhookDelivery, WebhookFailureReason

logger = logging.getLogger(__name__)

_DEFAULT_BACKOFF_BASE = 1.0
_DEFAULT_BACKOFF_MAX = 30.0
_DEFAULT_MAX_DELIVERY_ATTEMPTS = 5
_DEFAULT_MAX_DELIVERY_AGE_SECONDS = 86400
_DEFAULT_DISPATCH_CONCURRENCY = 4
_DEFAULT_DISPATCH_BATCH_SIZE = 100
_COORDINATOR_ERROR_RETRY_SECONDS = 1.0
_COORDINATOR_MIN_IDLE_DELAY_SECONDS = 0.05


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


class HttpWebhookDispatcher:
    """Durable dispatcher with one coordinator and a fixed worker pool.

    Persistent delivery limits apply to total attempts, including the first.
    HTTP attempts are made only for durable outbox rows.
    """

    def __init__(
        self,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        *,
        backoff_base: float = _DEFAULT_BACKOFF_BASE,
        backoff_max: float = _DEFAULT_BACKOFF_MAX,
        outbox_store: WebhookOutboxStore | None = None,
        max_delivery_attempts: int = _DEFAULT_MAX_DELIVERY_ATTEMPTS,
        max_delivery_age_seconds: int = _DEFAULT_MAX_DELIVERY_AGE_SECONDS,
        dispatch_concurrency: int = _DEFAULT_DISPATCH_CONCURRENCY,
        dispatch_batch_size: int = _DEFAULT_DISPATCH_BATCH_SIZE,
    ) -> None:
        if max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if max_delivery_age_seconds < 1:
            raise ValueError("max_delivery_age_seconds must be positive")
        if dispatch_concurrency < 1:
            raise ValueError("dispatch_concurrency must be positive")
        if dispatch_batch_size < dispatch_concurrency:
            raise ValueError("dispatch_batch_size must be >= dispatch_concurrency")

        self._client_factory = client_factory or httpx.AsyncClient
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._startup_lock = asyncio.Lock()
        self._accepting_tasks = True
        self._started = False
        self._stopping = False
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._outbox_store = outbox_store
        self._max_delivery_attempts = max_delivery_attempts
        self._max_delivery_age_seconds = max_delivery_age_seconds
        self._dispatch_concurrency = dispatch_concurrency
        self._dispatch_batch_size = dispatch_batch_size
        self._queue: asyncio.Queue[WebhookDelivery] | None = None
        self._wake_event = asyncio.Event()
        self._coordinator_task: asyncio.Task[None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._scheduled_ids: set[str] = set()
        self._active_ids: set[str] = set()

    @property
    def pending_tasks(self) -> int:
        """Return queued plus actively processed delivery count."""

        queue = self._queue
        return (0 if queue is None else queue.qsize()) + len(self._active_ids)

    @property
    def background_task_count(self) -> int:
        """Return live coordinator/worker task count for boundedness checks."""

        return sum(1 for task in self._tasks if not task.done())

    @property
    def dispatch_concurrency(self) -> int:
        return self._dispatch_concurrency

    @property
    def background_ok(self) -> bool:
        """Whether the coordinator and fixed worker set are all running."""

        if self._outbox_store is None:
            return True
        expected = self._dispatch_concurrency + 1
        tasks = [*self._worker_tasks]
        if self._coordinator_task is not None:
            tasks.append(self._coordinator_task)
        return self._started and len(tasks) == expected and all(
            not task.done() for task in tasks
        )

    @property
    def background_detail(self) -> str | None:
        if self.background_ok:
            return None
        if not self._started:
            return "webhook dispatcher not started"
        tasks = [*self._worker_tasks]
        if self._coordinator_task is not None:
            tasks.append(self._coordinator_task)
        for task in tasks:
            if task.cancelled():
                return "webhook background task stopped"
            if task.done():
                exception = task.exception()
                return (
                    "webhook background task stopped"
                    if exception is None
                    else f"webhook background task failed: {type(exception).__name__}"
                )
        return "webhook background task set incomplete"

    async def startup(self) -> None:
        """Start one coordinator and a fixed number of durable workers."""

        async with self._startup_lock:
            if self._started or self._outbox_store is None:
                self._accepting_tasks = True
                return
            lingering = [task for task in self._tasks if not task.done()]
            if lingering:
                raise RuntimeError(
                    "Webhook dispatcher cannot start while tasks from a previous "
                    "shutdown are still running"
                )

            self._accepting_tasks = True
            self._stopping = False
            self._tasks.clear()
            self._worker_tasks.clear()
            self._coordinator_task = None
            self._queue = asyncio.Queue(maxsize=self._dispatch_batch_size)
            self._scheduled_ids.clear()
            self._active_ids.clear()
            self._wake_event.set()
            self._started = True

            try:
                summary = await self._outbox_store.summarize()
            except Exception as exc:
                logger.error(
                    "Webhook dispatcher startup summary unavailable "
                    "error_type=%s recovery_action=coordinator_retry",
                    type(exc).__name__,
                )
            else:
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
                    self._dispatch_concurrency,
                    self._dispatch_batch_size,
                )

            for index in range(self._dispatch_concurrency):
                task = asyncio.create_task(
                    self._worker_loop(index),
                    name=f"scrapeyard-webhook-worker:{index}",
                )
                self._worker_tasks.append(task)
                self._track_task(task)
            self._coordinator_task = asyncio.create_task(
                self._coordinator_loop(),
                name="scrapeyard-webhook-coordinator",
            )
            self._track_task(self._coordinator_task)

    def _backoff_delay(self, attempt_index: int) -> float:
        """Return capped exponential delay for a zero-based attempt index."""

        return float(
            min(self._backoff_base * (2 ** attempt_index), self._backoff_max)
        )

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """5xx and 429 are retryable; other 4xx responses are permanent."""

        return status_code >= 500 or status_code == 429

    @staticmethod
    def _safe_payload_context(payload: dict[str, Any]) -> tuple[object, ...]:
        return (
            payload.get("delivery_id"),
            payload.get("job_id"),
            payload.get("run_id"),
            payload.get("event"),
        )

    @staticmethod
    def _parse_retry_after(
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
        if value.isdigit():
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

    async def send_once(
        self,
        config: WebhookConfig | WebhookRequestConfig,
        payload: dict[str, Any],
    ) -> WebhookDispatchResult:
        """Perform one safety-checked HTTP request without retrying."""

        url = str(config.url)
        start = time.monotonic()
        try:
            resolved = await asyncio.to_thread(resolve_public_url, url)
            client = await self._get_client()
            headers = dict(config.headers)
            headers["Host"] = resolved.host_header
            response = await client.post(
                resolved.connect_url,
                json=payload,
                headers=headers,
                timeout=config.timeout,
                follow_redirects=False,
                extensions={"sni_hostname": resolved.sni_hostname},
            )
            elapsed_ms = (time.monotonic() - start) * 1000

            if response.is_success:
                logger.info(
                    "Webhook HTTP attempt completed status_code=%s "
                    "elapsed_ms=%.0f outcome=delivered",
                    response.status_code,
                    elapsed_ms,
                )
                return WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)

            last_error = f"HTTP {response.status_code}"
            if not self._is_retryable_status(response.status_code):
                logger.warning(
                    "Webhook HTTP attempt completed status_code=%s "
                    "elapsed_ms=%.0f outcome=permanent_failed "
                    "reason_code=%s",
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

            retry_after, retry_after_action = self._parse_retry_after(
                response,
                now=utc_now(),
            )
            if retry_after_action != "absent":
                delivery_id, job_id, run_id, event = self._safe_payload_context(payload)
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
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
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

    async def notify(self) -> None:
        """Wake bounded workers without recreating an already-durable intent."""

        if not self._accepting_tasks:
            return
        if not self._started:
            await self.startup()
        self._wake_event.set()

    async def shutdown(self, timeout: float | None = None) -> None:
        """Stop scheduling, drain bounded work within grace, then cancel.

        Live tasks that miss the deadline remain tracked and cause
        :class:`asyncio.TimeoutError`; a later startup cannot overlap them.
        """

        self._accepting_tasks = False
        self._stopping = True
        self._wake_event.set()
        deadline = MonotonicDeadline(timeout)
        unresolved_phases: list[str] = []
        coordinator = self._coordinator_task
        if coordinator is not None and not coordinator.done():
            coordinator.cancel()
            try:
                await deadline.run(
                    asyncio.gather(coordinator, return_exceptions=True)
                )
            except asyncio.TimeoutError:
                unresolved_phases.append("coordinator")

        queue = self._queue
        queued_at_start = 0 if queue is None else queue.qsize()
        active_at_start = len(self._active_ids)
        timed_out = False
        if queue is not None and (queued_at_start or active_at_start):
            try:
                if timeout is None:
                    await queue.join()
                else:
                    await deadline.run(queue.join())
            except asyncio.TimeoutError:
                timed_out = True

        cancelled = 0
        for task in self._worker_tasks:
            if not task.done():
                cancelled += 1
                task.cancel()
        if self._worker_tasks:
            try:
                await deadline.run(
                    asyncio.gather(*self._worker_tasks, return_exceptions=True)
                )
            except asyncio.TimeoutError:
                # A cancelled gather and its child tasks can require one more
                # non-blocking loop turn to publish their terminal state.
                await asyncio.sleep(0)
                if any(not task.done() for task in self._worker_tasks):
                    unresolved_phases.append("workers")
                    logger.warning(
                        "Webhook worker cancellation did not acknowledge before "
                        "the shutdown deadline"
                    )

        remaining = (0 if queue is None else queue.qsize()) + len(self._active_ids)
        logger.info(
            "Webhook dispatcher shutdown complete queued_at_start=%s "
            "active_at_start=%s remaining_pending_work=%s "
            "worker_tasks_cancelled=%s grace_timed_out=%s "
            "unresolved_phases=%s recovery_action=%s",
            queued_at_start,
            active_at_start,
            remaining,
            cancelled,
            timed_out,
            ",".join(unresolved_phases) or "none",
            (
                "shutdown_failure"
                if unresolved_phases
                else "resume_pending_on_restart"
                if remaining or timed_out
                else "drained"
            ),
        )

        self._started = False

        async def _close_client() -> None:
            async with self._client_lock:
                client = self._client
                self._client = None
            if client is not None:
                await client.aclose()

        try:
            await deadline.run(_close_client())
        except asyncio.TimeoutError:
            unresolved_phases.append("http_client")

        live_tasks = {task for task in self._tasks if not task.done()}
        self._tasks.intersection_update(live_tasks)
        self._worker_tasks[:] = [
            task for task in self._worker_tasks if not task.done()
        ]
        if self._coordinator_task is not None and self._coordinator_task.done():
            self._coordinator_task = None
        if not live_tasks:
            self._worker_tasks.clear()
            self._scheduled_ids.clear()
            self._active_ids.clear()
            self._queue = None

        if unresolved_phases:
            raise asyncio.TimeoutError(
                "Webhook shutdown deadline exceeded in phase(s): "
                + ", ".join(unresolved_phases)
            )

    async def _get_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._client is None:
                self._client = self._client_factory()
            return self._client

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _coordinator_loop(self) -> None:
        assert self._outbox_store is not None
        while not self._stopping:
            self._wake_event.clear()
            try:
                now = utc_now()
                exhausted = await self._outbox_store.list_exhausted_pending(
                    attempts_gte=self._max_delivery_attempts,
                    created_at_lte=now
                    - timedelta(seconds=self._max_delivery_age_seconds),
                    limit=self._dispatch_batch_size,
                )
                exhausted_count = 0
                for delivery in exhausted:
                    if delivery.delivery_id in self._scheduled_ids:
                        continue
                    exhausted_count += int(
                        await self._fail_if_exhausted(delivery, now=now)
                    )

                due = await self._outbox_store.list_due_pending(
                    now=now,
                    limit=self._dispatch_batch_size,
                )
                queued_count = 0
                queue = self._queue
                if queue is None:
                    return
                for delivery in due:
                    if delivery.delivery_id in self._scheduled_ids:
                        continue
                    try:
                        queue.put_nowait(delivery)
                    except asyncio.QueueFull:
                        break
                    self._scheduled_ids.add(delivery.delivery_id)
                    queued_count += 1

                if due or exhausted:
                    logger.info(
                        "Webhook due batch fetched fetched_due_count=%s "
                        "queued_count=%s exhausted_fetched_count=%s "
                        "exhausted_transition_count=%s batch_limit=%s "
                        "recovery_action=bounded_batch_processed",
                        len(due),
                        queued_count,
                        len(exhausted),
                        exhausted_count,
                        self._dispatch_batch_size,
                    )

                if self._stopping:
                    return
                if queue.full() or (due and queued_count == 0):
                    await self._wait_for_wake(_COORDINATOR_ERROR_RETRY_SECONDS)
                    continue
                if queued_count or exhausted_count:
                    await asyncio.sleep(0)
                    continue

                next_due = await self._outbox_store.next_pending_due_at()
                oldest_pending = (
                    await self._outbox_store.oldest_pending_created_at()
                )
                age_deadline = (
                    None
                    if oldest_pending is None
                    else self._as_utc(oldest_pending)
                    + timedelta(seconds=self._max_delivery_age_seconds)
                )
                wake_at = min(
                    (value for value in (next_due, age_deadline) if value is not None),
                    default=None,
                )
                timeout = (
                    None
                    if wake_at is None
                    else max(0.0, (self._as_utc(wake_at) - utc_now()).total_seconds())
                )
                if timeout is not None:
                    timeout = max(timeout, _COORDINATOR_MIN_IDLE_DELAY_SECONDS)
                await self._wait_for_wake(timeout)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Webhook coordinator/store failure error_type=%s "
                    "recovery_action=retry_coordinator",
                    type(exc).__name__,
                )
                await self._wait_for_wake(_COORDINATOR_ERROR_RETRY_SECONDS)

    async def _worker_loop(self, worker_index: int) -> None:
        queue = self._queue
        if queue is None:
            return
        while True:
            delivery = await queue.get()
            self._active_ids.add(delivery.delivery_id)
            try:
                await self._process_delivery(delivery)
            except asyncio.CancelledError:
                logger.info(
                    "Webhook attempt cancelled delivery_id=%s job_id=%s "
                    "run_id=%s event=%s attempt_count=%s max_attempts=%s "
                    "delivery_age_seconds=%.3f max_age_seconds=%s "
                    "reason_code=shutdown_cancelled "
                    "recovery_action=leave_pending_for_restart worker_index=%s",
                    delivery.delivery_id,
                    delivery.job_id,
                    delivery.run_id,
                    delivery.event,
                    delivery.attempts,
                    self._max_delivery_attempts,
                    self._delivery_age_seconds(delivery, utc_now()),
                    self._max_delivery_age_seconds,
                    worker_index,
                )
                raise
            except Exception as exc:
                logger.error(
                    "Webhook worker/store failure delivery_id=%s job_id=%s "
                    "run_id=%s event=%s attempt_count=%s max_attempts=%s "
                    "delivery_age_seconds=%.3f max_age_seconds=%s "
                    "reason_code=worker_failure error_type=%s "
                    "recovery_action=leave_pending_and_retry",
                    delivery.delivery_id,
                    delivery.job_id,
                    delivery.run_id,
                    delivery.event,
                    delivery.attempts,
                    self._max_delivery_attempts,
                    self._delivery_age_seconds(delivery, utc_now()),
                    self._max_delivery_age_seconds,
                    type(exc).__name__,
                )
            finally:
                self._active_ids.discard(delivery.delivery_id)
                self._scheduled_ids.discard(delivery.delivery_id)
                queue.task_done()
                self._wake_event.set()
            if self._stopping:
                return

    async def _process_delivery(self, delivery: WebhookDelivery) -> None:
        assert self._outbox_store is not None
        now = utc_now()
        if await self._fail_if_exhausted(delivery, now=now):
            return

        reserved = await self._outbox_store.begin_attempt(
            delivery.delivery_id,
            expected_attempts=delivery.attempts,
            attempted_at=now,
        )
        if reserved is None:
            logger.info(
                "Webhook attempt reservation no-op delivery_id=%s job_id=%s "
                "run_id=%s event=%s attempt_count=%s max_attempts=%s "
                "delivery_age_seconds=%.3f max_age_seconds=%s "
                "reason_code=reservation_race "
                "recovery_action=skip_duplicate_local_attempt",
                delivery.delivery_id,
                delivery.job_id,
                delivery.run_id,
                delivery.event,
                delivery.attempts,
                self._max_delivery_attempts,
                self._delivery_age_seconds(delivery, now),
                self._max_delivery_age_seconds,
            )
            return

        logger.info(
            "Webhook attempt started delivery_id=%s job_id=%s run_id=%s "
            "event=%s attempt_count=%s max_attempts=%s "
            "delivery_age_seconds=%.3f max_age_seconds=%s "
            "reason_code=attempt_started recovery_action=http_post",
            reserved.delivery_id,
            reserved.job_id,
            reserved.run_id,
            reserved.event,
            reserved.attempts,
            self._max_delivery_attempts,
            self._delivery_age_seconds(reserved, now),
            self._max_delivery_age_seconds,
        )
        request_config = WebhookRequestConfig(
            url=reserved.url,
            headers=reserved.headers,
            timeout=reserved.timeout_seconds,
        )
        try:
            result = await self.send_once(request_config, reserved.payload)
            if not isinstance(result, WebhookDispatchResult):
                raise TypeError("send_once returned an invalid result")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = WebhookDispatchResult(
                WebhookDispatchStatus.permanent_failed,
                1,
                f"Non-retryable dispatcher failure: {type(exc).__name__}",
                WebhookDispatchReason.non_retryable_failure,
            )

        completed_at = utc_now()
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
            self._max_delivery_attempts,
            self._delivery_age_seconds(reserved, completed_at),
            self._max_delivery_age_seconds,
            result.status.value,
            reason_code,
        )
        WEBHOOK_DELIVERIES.labels(result.status.value).inc()
        mark_last_success("webhook")

        if result.status is WebhookDispatchStatus.delivered:
            await self._outbox_store.mark_delivered(
                reserved.delivery_id,
                delivered_at=completed_at,
                expected_attempts=reserved.attempts,
            )
            return

        if result.status is WebhookDispatchStatus.permanent_failed:
            reason = self._terminal_reason(result.reason_code)
            await self._outbox_store.mark_failed(
                reserved.delivery_id,
                failed_at=completed_at,
                reason=reason,
                last_error=result.last_error or "Non-retryable webhook failure",
                expected_attempts=reserved.attempts,
            )
            self._log_dead_letter(reserved, completed_at, reason)
            return

        if reserved.attempts >= self._max_delivery_attempts:
            await self._fail_delivery(
                reserved,
                failed_at=completed_at,
                reason=WebhookFailureReason.attempt_exhausted,
                last_error="Maximum total webhook delivery attempts reached",
            )
            return

        deadline = self._delivery_deadline(reserved)
        if self._as_utc(completed_at) >= deadline:
            await self._fail_delivery(
                reserved,
                failed_at=completed_at,
                reason=WebhookFailureReason.age_exhausted,
                last_error="Maximum webhook delivery age reached after retryable failure",
            )
            return

        delay = max(
            self._backoff_delay(max(reserved.attempts - 1, 0)),
            result.retry_after_seconds or 0.0,
        )
        remaining_seconds = (deadline - self._as_utc(completed_at)).total_seconds()
        if delay >= remaining_seconds:
            await self._fail_delivery(
                reserved,
                failed_at=completed_at,
                reason=WebhookFailureReason.age_exhausted,
                last_error="Next retry cannot occur before maximum delivery age",
            )
            return
        retry_at = completed_at + timedelta(seconds=delay)

        await self._outbox_store.mark_retryable_failure(
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
            self._max_delivery_attempts,
            self._delivery_age_seconds(reserved, completed_at),
            self._max_delivery_age_seconds,
            reason_code,
            delay,
        )

    async def _fail_if_exhausted(
        self,
        delivery: WebhookDelivery,
        *,
        now: datetime,
    ) -> bool:
        reason: WebhookFailureReason | None = None
        error = "Webhook delivery exhausted"
        if delivery.attempts >= self._max_delivery_attempts:
            reason = WebhookFailureReason.attempt_exhausted
            error = "Maximum total webhook delivery attempts reached"
        elif self._as_utc(now) >= self._delivery_deadline(delivery):
            reason = WebhookFailureReason.age_exhausted
            error = "Maximum webhook delivery age reached"
        if reason is None:
            return False
        await self._fail_delivery(
            delivery,
            failed_at=now,
            reason=reason,
            last_error=error,
        )
        return True

    async def _fail_delivery(
        self,
        delivery: WebhookDelivery,
        *,
        failed_at: datetime,
        reason: WebhookFailureReason,
        last_error: str,
    ) -> None:
        assert self._outbox_store is not None
        transitioned = await self._outbox_store.mark_failed(
            delivery.delivery_id,
            failed_at=failed_at,
            reason=reason,
            last_error=last_error,
            expected_attempts=delivery.attempts,
        )
        if transitioned:
            self._log_dead_letter(delivery, failed_at, reason)

    def _log_dead_letter(
        self,
        delivery: WebhookDelivery,
        failed_at: datetime,
        reason: WebhookFailureReason,
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
            self._max_delivery_attempts,
            self._delivery_age_seconds(delivery, failed_at),
            self._max_delivery_age_seconds,
            reason.value,
        )

    async def _wait_for_wake(self, timeout: float | None) -> None:
        try:
            if timeout is None:
                await self._wake_event.wait()
            else:
                await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return

    def _delivery_deadline(self, delivery: WebhookDelivery) -> datetime:
        return self._as_utc(delivery.created_at) + timedelta(
            seconds=self._max_delivery_age_seconds
        )

    def _delivery_age_seconds(
        self,
        delivery: WebhookDelivery,
        now: datetime,
    ) -> float:
        return max(0.0, (self._as_utc(now) - self._as_utc(delivery.created_at)).total_seconds())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _terminal_reason(
        reason: WebhookDispatchReason | None,
    ) -> WebhookFailureReason:
        if reason is WebhookDispatchReason.permanent_http_response:
            return WebhookFailureReason.permanent_http_response
        if reason is WebhookDispatchReason.unsafe_url:
            return WebhookFailureReason.unsafe_url
        return WebhookFailureReason.non_retryable_failure
