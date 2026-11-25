"""Webhook dispatcher: bounded durable workers and httpx delivery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, cast

import httpx

from scrapeyard.common.async_tools import MonotonicDeadline
from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import WebhookConfig
from scrapeyard.engine.url_guard import resolve_public_url
from scrapeyard.storage.protocols import WebhookOutboxStore
from scrapeyard.webhook.client_pool import WebhookClientPool, _ClientCacheEntry
from scrapeyard.webhook.http_delivery import (
    drain_response,
    is_retryable_status,
    parse_retry_after,
    send_webhook_request,
)
from scrapeyard.webhook.outcome import (
    RetryPolicy,
    as_utc,
    fail_if_exhausted,
    log_delivery_event,
    log_startup_backlog,
    persist_attempt_outcome,
)
from scrapeyard.webhook.types import (
    QueuedWebhookDelivery,
    WebhookDispatchReason,
    WebhookDispatchResult,
    WebhookDispatchStatus,
    WebhookNotifier,
    WebhookRequestConfig,
)

__all__ = [
    "HttpWebhookDispatcher",
    "QueuedWebhookDelivery",
    "WebhookClientPool",
    "WebhookDispatchReason",
    "WebhookDispatchResult",
    "WebhookDispatchStatus",
    "WebhookNotifier",
    "WebhookRequestConfig",
]

logger = logging.getLogger(__name__)

_DEFAULT_BACKOFF_BASE = 1.0
_DEFAULT_BACKOFF_MAX = 30.0
_DEFAULT_MAX_DELIVERY_ATTEMPTS = 5
_DEFAULT_MAX_DELIVERY_AGE_SECONDS = 86400
_DEFAULT_DISPATCH_CONCURRENCY = 4
_DEFAULT_DISPATCH_BATCH_SIZE = 100
_DEFAULT_CLIENT_CACHE_MAX_SIZE = 64
_DEFAULT_CLIENT_CACHE_IDLE_TTL_SECONDS = 300.0
_COORDINATOR_ERROR_RETRY_SECONDS = 1.0
_COORDINATOR_MIN_IDLE_DELAY_SECONDS = 0.05


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
        client_cache_max_size: int = _DEFAULT_CLIENT_CACHE_MAX_SIZE,
        client_cache_idle_ttl_seconds: float = _DEFAULT_CLIENT_CACHE_IDLE_TTL_SECONDS,
    ) -> None:
        if max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if max_delivery_age_seconds < 1:
            raise ValueError("max_delivery_age_seconds must be positive")
        if dispatch_concurrency < 1:
            raise ValueError("dispatch_concurrency must be positive")
        if dispatch_batch_size < dispatch_concurrency:
            raise ValueError("dispatch_batch_size must be >= dispatch_concurrency")

        self._client_pool = WebhookClientPool(
            client_factory=client_factory,
            max_size=client_cache_max_size,
            idle_ttl_seconds=client_cache_idle_ttl_seconds,
        )
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
        self._queue: asyncio.Queue[QueuedWebhookDelivery] | None = None
        self._wake_event = asyncio.Event()
        self._coordinator_task: asyncio.Task[None] | None = None
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._scheduled_ids: set[str] = set()
        self._active_ids: set[str] = set()

    @property
    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            backoff_base=self._backoff_base,
            backoff_max=self._backoff_max,
            max_attempts=self._max_delivery_attempts,
            max_age_seconds=self._max_delivery_age_seconds,
        )

    @property
    def _clients(self) -> dict[str, _ClientCacheEntry]:
        return self._client_pool.entries

    @property
    def _client_close_task(self) -> asyncio.Task[None] | None:
        return self._client_pool.close_task

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
    def client_cache_size(self) -> int:
        return self._client_pool.size

    @property
    def shutdown_pending(self) -> bool:
        """Whether shutdown still owns tasks that may use runtime resources."""

        if not self._stopping:
            return False
        return (
            any(not task.done() for task in self._tasks)
            or self._client_pool.close_pending
        )

    @property
    def background_ok(self) -> bool:
        """Whether the coordinator and fixed worker set are all running."""

        if self._outbox_store is None:
            return True
        expected = self._dispatch_concurrency + 1
        tasks = [*self._worker_tasks]
        if self._coordinator_task is not None:
            tasks.append(self._coordinator_task)
        return self._started and len(tasks) == expected and all(not task.done() for task in tasks)

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
            if self._started:
                if self.shutdown_pending:
                    raise RuntimeError(
                        "Webhook dispatcher cannot start while tasks from a previous "
                        "shutdown are still running"
                    )
                self._accepting_tasks = True
                return
            if self._outbox_store is None:
                self._accepting_tasks = True
                return
            if any(not task.done() for task in self._tasks):
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

            await log_startup_backlog(
                self._outbox_store,
                dispatch_concurrency=self._dispatch_concurrency,
                dispatch_batch_size=self._dispatch_batch_size,
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
        return self._retry_policy.backoff_delay(attempt_index)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return is_retryable_status(status_code)

    @staticmethod
    def _parse_retry_after(
        response: httpx.Response,
        *,
        now: datetime,
    ) -> tuple[float | None, str]:
        return parse_retry_after(response, now=now)

    @staticmethod
    async def _drain_response(response: httpx.Response) -> None:
        await drain_response(response)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return as_utc(value)

    async def send_once(
        self,
        config: WebhookConfig | WebhookRequestConfig,
        payload: dict[str, Any],
    ) -> WebhookDispatchResult:
        """Perform one safety-checked HTTP request without retrying."""

        return await send_webhook_request(
            config,
            payload,
            acquire_client=self._acquire_client,
            release_client=self._release_client,
            resolve_url=resolve_public_url,
            now_fn=utc_now,
        )

    async def notify(self) -> None:
        """Wake bounded workers without recreating an already-durable intent."""

        if not self._accepting_tasks:
            return
        if not self._started:
            await self.startup()
        self._wake_event.set()

    async def shutdown(self, timeout: float | None = None) -> None:
        """Stop scheduling, drain bounded work within grace, then cancel."""

        self._accepting_tasks = False
        self._stopping = True
        self._wake_event.set()
        deadline = MonotonicDeadline(timeout)
        unresolved_phases = await self._stop_background_tasks(deadline, timeout=timeout)
        if not await self._client_pool.close(deadline):
            unresolved_phases.append("http_client")
        if unresolved_phases:
            raise TimeoutError(
                "Webhook shutdown deadline exceeded in phase(s): " + ", ".join(unresolved_phases)
            )

    async def _stop_background_tasks(
        self,
        deadline: MonotonicDeadline,
        *,
        timeout: float | None,
    ) -> list[str]:
        unresolved_phases: list[str] = []
        coordinator = self._coordinator_task
        if coordinator is not None and not coordinator.done():
            coordinator.cancel()
            try:
                await deadline.run(asyncio.gather(coordinator, return_exceptions=True))
            except TimeoutError:
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
            except TimeoutError:
                timed_out = True

        cancelled = 0
        for task in self._worker_tasks:
            if not task.done():
                cancelled += 1
                task.cancel()
        if self._worker_tasks:
            try:
                await deadline.run(asyncio.gather(*self._worker_tasks, return_exceptions=True))
            except TimeoutError:
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

        live_tasks = {task for task in self._tasks if not task.done()}
        self._tasks.intersection_update(live_tasks)
        self._worker_tasks[:] = [task for task in self._worker_tasks if not task.done()]
        if self._coordinator_task is not None and self._coordinator_task.done():
            self._coordinator_task = None
        if live_tasks:
            if "coordinator" not in unresolved_phases and (
                self._coordinator_task is not None and not self._coordinator_task.done()
            ):
                unresolved_phases.append("coordinator")
            if "workers" not in unresolved_phases and self._worker_tasks:
                unresolved_phases.append("workers")
            raise TimeoutError(
                "Webhook shutdown deadline exceeded in phase(s): " + ", ".join(unresolved_phases)
            )

        self._started = False
        self._worker_tasks.clear()
        self._scheduled_ids.clear()
        self._active_ids.clear()
        self._queue = None
        return unresolved_phases

    async def _acquire_client(self, logical_hostname: str) -> httpx.AsyncClient:
        return await self._client_pool.acquire(logical_hostname)

    async def _release_client(
        self,
        logical_hostname: str,
        client: httpx.AsyncClient,
    ) -> None:
        await self._client_pool.release(logical_hostname, client)

    def _track_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch_exhausted_candidates(
        self,
        *,
        now: datetime,
    ) -> list[QueuedWebhookDelivery]:
        assert self._outbox_store is not None
        created_at_lte = now - timedelta(seconds=self._max_delivery_age_seconds)
        header_fetcher = getattr(self._outbox_store, "list_exhausted_pending_headers", None)
        if header_fetcher is not None:
            return cast(
                list[QueuedWebhookDelivery],
                await header_fetcher(
                    attempts_gte=self._max_delivery_attempts,
                    created_at_lte=created_at_lte,
                    limit=self._dispatch_batch_size,
                    exclude_ids=self._scheduled_ids,
                ),
            )
        return cast(
            list[QueuedWebhookDelivery],
            await self._outbox_store.list_exhausted_pending(
                attempts_gte=self._max_delivery_attempts,
                created_at_lte=created_at_lte,
                limit=self._dispatch_batch_size,
            ),
        )

    async def _fetch_due_candidates(
        self,
        *,
        now: datetime,
    ) -> list[QueuedWebhookDelivery]:
        assert self._outbox_store is not None
        header_fetcher = getattr(self._outbox_store, "list_due_pending_headers", None)
        if header_fetcher is not None:
            return cast(
                list[QueuedWebhookDelivery],
                await header_fetcher(
                    now=now,
                    limit=self._dispatch_batch_size,
                    exclude_ids=self._scheduled_ids,
                ),
            )
        return cast(
            list[QueuedWebhookDelivery],
            await self._outbox_store.list_due_pending(
                now=now,
                limit=self._dispatch_batch_size,
            ),
        )

    async def _coordinator_loop(self) -> None:
        assert self._outbox_store is not None
        while not self._stopping:
            self._wake_event.clear()
            try:
                now = utc_now()
                policy = self._retry_policy
                exhausted = await self._fetch_exhausted_candidates(now=now)
                exhausted_count = 0
                for delivery in exhausted:
                    if delivery.delivery_id in self._scheduled_ids:
                        continue
                    exhausted_count += int(
                        await fail_if_exhausted(
                            self._outbox_store, delivery, now=now, policy=policy
                        )
                    )

                due = await self._fetch_due_candidates(now=now)
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
                if (
                    queued_count
                    and len(due) >= self._dispatch_batch_size
                    and queue.qsize() <= self._dispatch_concurrency
                ) or exhausted_count:
                    await asyncio.sleep(0)
                    continue
                if self._scheduled_ids and queue.qsize() > self._dispatch_concurrency:
                    await self._wait_for_wake(_COORDINATOR_ERROR_RETRY_SECONDS)
                    continue

                await self._wait_until_next_outbox_event()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "Webhook coordinator/store failure error_type=%s "
                    "recovery_action=retry_coordinator",
                    type(exc).__name__,
                )
                await self._wait_for_wake(_COORDINATOR_ERROR_RETRY_SECONDS)

    async def _wait_until_next_outbox_event(self) -> None:
        assert self._outbox_store is not None
        next_due = await self._outbox_store.next_pending_due_at()
        oldest_pending = await self._outbox_store.oldest_pending_created_at()
        age_deadline = (
            None
            if oldest_pending is None
            else as_utc(oldest_pending) + timedelta(seconds=self._max_delivery_age_seconds)
        )
        wake_at = min(
            (value for value in (next_due, age_deadline) if value is not None),
            default=None,
        )
        timeout = (
            None
            if wake_at is None
            else max(0.0, (as_utc(wake_at) - utc_now()).total_seconds())
        )
        if timeout is not None:
            timeout = max(timeout, _COORDINATOR_MIN_IDLE_DELAY_SECONDS)
        await self._wait_for_wake(timeout)

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
                log_delivery_event(
                    logger.info,
                    "Webhook attempt cancelled",
                    delivery,
                    now=utc_now(),
                    policy=self._retry_policy,
                    reason_code="shutdown_cancelled",
                    recovery_action="leave_pending_for_restart",
                    extra_fmt="worker_index=%s",
                    extra_args=(worker_index,),
                )
                raise
            except Exception as exc:
                log_delivery_event(
                    logger.error,
                    "Webhook worker/store failure",
                    delivery,
                    now=utc_now(),
                    policy=self._retry_policy,
                    reason_code="worker_failure",
                    recovery_action="leave_pending_and_retry",
                    extra_fmt="error_type=%s",
                    extra_args=(type(exc).__name__,),
                )
            finally:
                self._active_ids.discard(delivery.delivery_id)
                self._scheduled_ids.discard(delivery.delivery_id)
                queue.task_done()
                if queue.qsize() <= self._dispatch_concurrency:
                    self._wake_event.set()
            if self._stopping and queue.empty():
                return

    async def _process_delivery(self, delivery: QueuedWebhookDelivery) -> None:
        assert self._outbox_store is not None
        now = utc_now()
        policy = self._retry_policy
        if await fail_if_exhausted(self._outbox_store, delivery, now=now, policy=policy):
            return

        reserved = await self._outbox_store.begin_attempt(
            delivery.delivery_id,
            expected_attempts=delivery.attempts,
            attempted_at=now,
        )
        if reserved is None:
            log_delivery_event(
                logger.info,
                "Webhook attempt reservation no-op",
                delivery,
                now=now,
                policy=policy,
                reason_code="reservation_race",
                recovery_action="skip_duplicate_local_attempt",
            )
            return

        log_delivery_event(
            logger.info,
            "Webhook attempt started",
            reserved,
            now=now,
            policy=policy,
            reason_code="attempt_started",
            recovery_action="http_post",
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

        await persist_attempt_outcome(
            self._outbox_store,
            reserved,
            result,
            policy=policy,
            now_fn=utc_now,
        )

    async def _wait_for_wake(self, timeout: float | None) -> None:
        try:
            if timeout is None:
                await self._wake_event.wait()
            else:
                await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except TimeoutError:
            return
