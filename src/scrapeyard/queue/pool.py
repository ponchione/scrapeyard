"""Redis-backed queue service with embedded arq worker execution."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Protocol, cast

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.constants import in_progress_key_prefix, job_key_prefix, result_key_prefix
from arq.jobs import Job, ResultNotFound, serialize_job
from arq.utils import timestamp_ms
from arq.worker import Worker, func

from scrapeyard.common.settings import get_settings
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.cancellation import (
    QueueCancellationOutcome,
    QueueDeliveryState as QueueDeliveryState,
    RunCancellationResult,
)
from scrapeyard.queue.memory import get_process_rss_mb
from scrapeyard.queue.priority import (
    PRIORITIES,
    WeightedPriorityPolicy,
    priority_queue_names,
)

_ARQ_FUNCTION_NAME = "scrape_job"
_KEEP_RESULT_SECONDS = 3600
logger = logging.getLogger(__name__)

_ATOMIC_ENQUEUE_LUA = """
if redis.call('EXISTS', KEYS[2], KEYS[3]) > 0 then
    return 0
end
local payload_exists = redis.call('EXISTS', KEYS[1])
for index = 4, 7 do
    if payload_exists == 1 and redis.call('ZSCORE', KEYS[index], ARGV[4]) then
        return 0
    end
end
if payload_exists == 0 then
    for index = 4, 7 do
        redis.call('ZREM', KEYS[index], ARGV[4])
    end
end
local score = tonumber(ARGV[1])
local last_score = redis.call('GET', KEYS[9])
if last_score and tonumber(last_score) >= score then
    score = tonumber(last_score) + 0.001
end
local ttl = tonumber(ARGV[2]) + math.ceil(math.max(0, score - tonumber(ARGV[1])))
redis.call('PSETEX', KEYS[1], ttl, ARGV[3])
redis.call('ZADD', KEYS[8], score, ARGV[4])
redis.call('SET', KEYS[9], string.format('%.3f', score))
return 1
"""

_ADMIT_ONE_LUA = """
for index = 1, 3 do
    local candidate = redis.call(
        'ZRANGEBYSCORE', KEYS[index], '-inf', ARGV[1], 'WITHSCORES', 'LIMIT', 0, 1
    )
    if #candidate > 0 then
        redis.call('ZREM', KEYS[index], candidate[1])
        redis.call('ZADD', KEYS[4], candidate[2], candidate[1])
        return {candidate[1], KEYS[index]}
    end
end
return {}
"""

_MOVE_DELIVERY_LUA = """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score then
    return 0
end
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], score, ARGV[1])
return 1
"""


@dataclass(frozen=True, slots=True)
class _DeliverySnapshot:
    state: QueueDeliveryState
    queue_name: str | None = None


class _PriorityJobHandle:
    """Result handle that follows one run as admission moves it between queues."""

    def __init__(self, pool: WorkerPool, run_id: str) -> None:
        self._pool = pool
        self._run_id = run_id

    async def result(
        self,
        timeout: float | None = None,
        *,
        poll_delay: float = 0.5,
    ) -> object:
        started = monotonic()
        while True:
            snapshot = await self._pool._delivery_snapshot(self._run_id)
            if snapshot.state is QueueDeliveryState.complete:
                if self._pool.redis is None:
                    raise RuntimeError("Redis disconnected while waiting for queue result")
                # Result keys are global to the arq job identity; queue name is
                # irrelevant once the result exists.
                return cast(
                    object,
                    await Job(
                        self._run_id,
                        redis=self._pool.redis,
                        _queue_name=self._pool._queue_name,
                    ).result(timeout=0, poll_delay=poll_delay),
                )
            if snapshot.state is QueueDeliveryState.missing:
                raise ResultNotFound(
                    "Not waiting for job result because the delivery is not in "
                    "any configured priority queue"
                )
            if timeout is not None and monotonic() - started > timeout:
                raise asyncio.TimeoutError
            await asyncio.sleep(poll_delay)


class _PriorityWorker(Worker):
    """Single arq Worker with weighted admission from three intake queues."""

    def __init__(self, *args: Any, priority_queues: dict[str, str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.priority_queues = priority_queues
        self.priority_policy = WeightedPriorityPolicy()

    async def _poll_iteration(self) -> None:
        if self.allow_pick_jobs and self.job_counter < self.max_jobs:
            ready_staged = await self.pool.zcount(
                self.queue_name,
                min=float("-inf"),
                max=timestamp_ms(),
            )
            available = max(0, self.max_jobs - self.job_counter - ready_staged)
            for _ in range(available):
                if not await self._admit_one():
                    break
        await super()._poll_iteration()

    async def _admit_one(self) -> bool:
        order = self.priority_policy.selection_order()
        queue_order = [self.priority_queues[priority] for priority in order]
        admitted = await cast(
            Awaitable[Any],
            self.pool.eval(
                _ADMIT_ONE_LUA,
                4,
                *queue_order,
                self.queue_name,
                str(timestamp_ms()),
            ),
        )
        if not admitted:
            return False
        self.priority_policy.admitted()
        return True


class QueueJobHandle(Protocol):
    """Minimal async handle for waiting on queued job completion."""

    async def result(
        self,
        timeout: float | None = None,
        *,
        poll_delay: float = 0.5,
    ) -> object: ...


class QueueTaskHandler(Protocol):
    """Typed first-party callback invoked by the arq adapter."""

    async def __call__(
        self,
        job_id: str,
        config_yaml: str,
        *,
        run_id: str | None = None,
        trigger: str = "adhoc",
        browser_limiter: BrowserExecutionLimiter,
    ) -> None: ...


class WorkerPool:
    """Queue service that enqueues jobs into Redis and executes them via arq."""

    def __init__(
        self,
        max_concurrent: int,
        max_browsers: int,
        memory_limit_mb: int,
        redis_settings: RedisSettings,
        queue_name: str,
        task_handler: QueueTaskHandler | None = None,
        cancellation_grace_seconds: float = 10.0,
        job_timeout_seconds: float = 300.0,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._max_browsers = max_browsers
        self._memory_limit_mb = memory_limit_mb
        self._redis_settings = redis_settings
        self._queue_name = queue_name
        self._priority_queues = priority_queue_names(queue_name)
        self._known_queues = (
            queue_name,
            *(self._priority_queues[priority] for priority in PRIORITIES),
        )
        self._task_handler = task_handler
        self._cancellation_grace_seconds = cancellation_grace_seconds
        self._job_timeout_seconds = job_timeout_seconds

        self._browser_limiter = BrowserExecutionLimiter(max_browsers)
        self._redis: ArqRedis | None = None
        self._worker: Worker | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._active_tasks = 0
        self._started = False

    def _check_memory(self) -> bool:
        """Return True if current RSS is within limits."""
        if self._memory_limit_mb <= 0:
            return True
        rss_mb = get_process_rss_mb()
        if rss_mb is None:
            return True
        return rss_mb < self._memory_limit_mb

    async def start(self) -> None:
        """Start the Redis connection and embedded arq worker."""
        if self._started:
            return

        settings = get_settings()
        timeout_seconds = settings.workers_redis_connect_timeout_seconds
        try:
            redis = await asyncio.wait_for(
                create_pool(
                    self._redis_settings,
                    default_queue_name=self._queue_name,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            logger.error(
                "Timed out connecting to Redis queue %s after %ss",
                self._queue_name,
                timeout_seconds,
            )
            raise RuntimeError(f"Timed out connecting to Redis after {timeout_seconds}s") from exc
        except Exception:
            logger.exception("Failed to connect to Redis queue %s", self._queue_name)
            raise

        self._redis = redis
        self._worker = _PriorityWorker(
            functions=[func(cast(Any, self._run_job), name=_ARQ_FUNCTION_NAME, keep_result=_KEEP_RESULT_SECONDS)],
            redis_pool=self._redis,
            queue_name=self._queue_name,
            priority_queues=self._priority_queues,
            handle_signals=False,
            max_jobs=self._max_concurrent,
            job_timeout=self._job_timeout_seconds,
            keep_result=_KEEP_RESULT_SECONDS,
            retry_jobs=False,
            allow_abort_jobs=True,
        )
        self._runner_task = asyncio.create_task(
            self._worker.async_run(),
            name="scrapeyard-arq-worker",
        )
        self._started = True

    async def stop(self, *, timeout: float | None = None) -> None:
        """Stop picking new jobs and close the embedded worker."""
        if not self._started:
            return

        if self._worker is None:
            raise RuntimeError("WorkerPool.stop() called but worker was never started")

        self._worker.allow_pick_jobs = False
        grace_seconds = (
            get_settings().workers_shutdown_grace_seconds
            if timeout is None
            else max(0.0, timeout)
        )
        pending = [
            task for task in self._worker.tasks.values()
            if not task.done()
        ]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True),
                    timeout=grace_seconds,
                )
            except asyncio.TimeoutError:
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        if self._worker.main_task is not None:
            self._worker.main_task.cancel()
        if self._runner_task is not None:
            with suppress(asyncio.CancelledError):
                await self._runner_task

        await self._worker.close()

        self._redis = None
        self._worker = None
        self._runner_task = None
        self._started = False

    async def enqueue(
        self,
        job_id: str,
        config_yaml: str,
        priority: str = "normal",
        needs_browser: bool = False,
        *,
        run_id: str | None = None,
        trigger: str = "adhoc",
    ) -> QueueJobHandle:
        """Enqueue a scrape job and return a handle for awaiting completion."""
        if not self._check_memory():
            raise MemoryError(
                f"Process memory exceeds {self._memory_limit_mb}MB limit — rejecting task"
            )
        if not self._started:
            await self.start()

        if self._redis is None:
            raise RuntimeError("WorkerPool.enqueue() called but Redis pool is not connected")
        if priority not in self._priority_queues:
            raise ValueError(f"Unknown queue priority: {priority!r}")
        if run_id is None:
            raise RuntimeError("WorkerPool.enqueue() requires a run_id")

        enqueue_time_ms = timestamp_ms()
        payload = serialize_job(
            _ARQ_FUNCTION_NAME,
            (job_id, config_yaml, run_id),
            {
                "needs_browser": needs_browser,
                "trigger": trigger,
            },
            None,
            enqueue_time_ms,
            serializer=self._redis.job_serializer,
        )
        queue_name = self._priority_queues[priority]
        await cast(
            Awaitable[Any],
            self._redis.eval(
                _ATOMIC_ENQUEUE_LUA,
                9,
                f"{job_key_prefix}{run_id}",
                f"{result_key_prefix}{run_id}",
                f"{in_progress_key_prefix}{run_id}",
                *self._known_queues,
                queue_name,
                f"{queue_name}:fifo-score",
                str(enqueue_time_ms),
                str(self._redis.expires_extra_ms),
                cast(str, payload),
                run_id,
            ),
        )
        return _PriorityJobHandle(self, run_id)

    async def inspect_delivery(self, run_id: str) -> QueueDeliveryState:
        """Inspect one arq delivery by its run-scoped Redis job identity.

        Global result, in-progress, and payload keys plus the base execution
        queue and three priority queues are read in one bounded transaction.
        No operation scans Redis or infers delivery state from SQLite.
        """
        if self._redis is None:
            raise RuntimeError(
                "WorkerPool.inspect_delivery() requires an active Redis connection"
            )
        return (await self._delivery_snapshot(run_id)).state

    async def _delivery_snapshot(self, run_id: str) -> _DeliverySnapshot:
        """Inspect global arq keys and the fixed bounded queue set atomically."""

        if self._redis is None:
            raise RuntimeError(
                "WorkerPool.inspect_delivery() requires an active Redis connection"
            )
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.exists(f"{result_key_prefix}{run_id}")
            pipe.exists(f"{in_progress_key_prefix}{run_id}")
            pipe.exists(f"{job_key_prefix}{run_id}")
            for queue_name in self._known_queues:
                pipe.zscore(queue_name, run_id)
            result_exists, in_progress, payload_exists, *scores = await pipe.execute()

        queue_members = [
            (queue_name, score)
            for queue_name, score in zip(self._known_queues, scores, strict=True)
            if score is not None
        ]
        if result_exists:
            return _DeliverySnapshot(QueueDeliveryState.complete)
        if len(queue_members) > 1:
            raise RuntimeError(
                f"Run {run_id!r} exists in multiple configured queues"
            )
        located_queue = queue_members[0][0] if queue_members else None
        if in_progress:
            return _DeliverySnapshot(QueueDeliveryState.in_progress, located_queue)
        if not payload_exists or located_queue is None:
            return _DeliverySnapshot(QueueDeliveryState.missing)
        score = float(queue_members[0][1])
        state = (
            QueueDeliveryState.deferred
            if score > timestamp_ms()
            else QueueDeliveryState.queued
        )
        return _DeliverySnapshot(state, located_queue)

    async def queue_depths(self) -> dict[str, int]:
        """Return waiting/deferred intake members, excluding admitted/active work."""

        if self._redis is None:
            raise RuntimeError("WorkerPool.queue_depths() requires an active Redis connection")
        async with self._redis.pipeline(transaction=True) as pipe:
            for priority in PRIORITIES:
                pipe.zcard(self._priority_queues[priority])
            depths = await pipe.execute()
        return dict(zip(PRIORITIES, (int(depth) for depth in depths), strict=True))

    async def queue_operational_snapshot(self) -> dict[str, tuple[int, float]]:
        """Return fixed-priority depth and oldest age without scanning Redis."""

        if self._redis is None:
            raise RuntimeError(
                "WorkerPool.queue_operational_snapshot() requires an active Redis connection"
            )
        now_ms = timestamp_ms()
        async with self._redis.pipeline(transaction=True) as pipe:
            for priority in PRIORITIES:
                queue_name = self._priority_queues[priority]
                pipe.zcard(queue_name)
                pipe.zrange(queue_name, 0, 0, withscores=True)
            values = await pipe.execute()

        snapshot: dict[str, tuple[int, float]] = {}
        for index, priority in enumerate(PRIORITIES):
            depth = int(values[index * 2])
            oldest = values[index * 2 + 1]
            age_seconds = (
                0.0
                if not oldest
                else max(0.0, (now_ms - float(oldest[0][1])) / 1000.0)
            )
            snapshot[priority] = (depth, age_seconds)
        return snapshot

    async def _move_to_execution_queue(self, run_id: str, source_queue: str) -> bool:
        if self._redis is None:
            raise RuntimeError("Redis disconnected while moving queued delivery")
        if source_queue == self._queue_name:
            return True
        moved = await cast(
            Awaitable[Any],
            self._redis.eval(
                _MOVE_DELIVERY_LUA,
                2,
                source_queue,
                self._queue_name,
                run_id,
            ),
        )
        return bool(moved)

    async def cancel_run(self, run_id: str) -> RunCancellationResult:
        """Abort and verify one arq delivery using only its run-scoped identity."""

        if self._redis is None:
            logger.error(
                "Run cancellation unavailable run_id=%s queue_outcome=%s "
                "cancellation_phase=redis_connection_check",
                run_id,
                QueueCancellationOutcome.unavailable.value,
            )
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.unavailable,
            )
        try:
            initial_snapshot = await self._delivery_snapshot(run_id)
            initial = initial_snapshot.state
        except Exception as exc:
            logger.error(
                "Run cancellation inspection unavailable run_id=%s queue_outcome=%s "
                "cancellation_phase=initial_inspection error_type=%s",
                run_id,
                QueueCancellationOutcome.unavailable.value,
                type(exc).__name__,
            )
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.unavailable,
            )

        if initial is QueueDeliveryState.complete:
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.complete,
                initial,
                initial,
            )
        if initial is QueueDeliveryState.missing:
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.missing,
                initial,
                initial,
            )

        delivery_queue = initial_snapshot.queue_name
        if delivery_queue is None:
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.unavailable,
                initial,
            )
        if initial in {QueueDeliveryState.queued, QueueDeliveryState.deferred}:
            try:
                moved = await self._move_to_execution_queue(run_id, delivery_queue)
                if not moved:
                    refreshed = await self._delivery_snapshot(run_id)
                    delivery_queue = refreshed.queue_name
                    if refreshed.state in {
                        QueueDeliveryState.complete,
                        QueueDeliveryState.missing,
                    }:
                        outcome = (
                            QueueCancellationOutcome.complete
                            if refreshed.state is QueueDeliveryState.complete
                            else QueueCancellationOutcome.missing
                        )
                        return RunCancellationResult(
                            run_id,
                            outcome,
                            initial,
                            refreshed.state,
                        )
                    if delivery_queue != self._queue_name:
                        raise RuntimeError("delivery moved to an unexpected queue")
            except Exception as exc:
                logger.error(
                    "Run cancellation queue admission unavailable run_id=%s "
                    "redis_state=%s queue_outcome=%s "
                    "cancellation_phase=priority_admission error_type=%s",
                    run_id,
                    initial.value,
                    QueueCancellationOutcome.unavailable.value,
                    type(exc).__name__,
                )
                return RunCancellationResult(
                    run_id,
                    QueueCancellationOutcome.unavailable,
                    initial,
                )
        job = Job(run_id, redis=self._redis, _queue_name=self._queue_name)
        logger.info(
            "Run abort requested run_id=%s redis_state=%s "
            "cancellation_phase=arq_abort grace_seconds=%s",
            run_id,
            initial.value,
            self._cancellation_grace_seconds,
        )
        try:
            aborted = await job.abort(
                timeout=self._cancellation_grace_seconds,
                poll_delay=min(0.1, self._cancellation_grace_seconds),
            )
        except asyncio.TimeoutError:
            logger.error(
                "Run abort timed out run_id=%s redis_state=%s queue_outcome=%s "
                "cancellation_phase=arq_abort grace_seconds=%s",
                run_id,
                initial.value,
                QueueCancellationOutcome.timeout.value,
                self._cancellation_grace_seconds,
            )
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.timeout,
                initial,
            )
        except Exception as exc:
            logger.error(
                "Run abort unavailable run_id=%s redis_state=%s queue_outcome=%s "
                "cancellation_phase=arq_abort error_type=%s",
                run_id,
                initial.value,
                QueueCancellationOutcome.unavailable.value,
                type(exc).__name__,
            )
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.unavailable,
                initial,
            )

        try:
            final = await self.inspect_delivery(run_id)
        except Exception as exc:
            logger.error(
                "Run cancellation verification unavailable run_id=%s "
                "redis_state=%s queue_outcome=%s "
                "cancellation_phase=final_inspection error_type=%s",
                run_id,
                initial.value,
                QueueCancellationOutcome.unavailable.value,
                type(exc).__name__,
            )
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.unavailable,
                initial,
            )

        if aborted:
            outcomes = {
                QueueDeliveryState.queued: QueueCancellationOutcome.queued_cancelled,
                QueueDeliveryState.deferred: QueueCancellationOutcome.deferred_cancelled,
                QueueDeliveryState.in_progress: (
                    QueueCancellationOutcome.in_progress_cancelled
                ),
            }
            outcome = outcomes[initial]
            logger.info(
                "Run abort completed run_id=%s prior_redis_state=%s "
                "redis_state=%s queue_outcome=%s cancellation_phase=quiescent",
                run_id,
                initial.value,
                final.value,
                outcome.value,
            )
            return RunCancellationResult(run_id, outcome, initial, final)

        if final is QueueDeliveryState.complete:
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.complete,
                initial,
                final,
            )
        if final is QueueDeliveryState.missing:
            return RunCancellationResult(
                run_id,
                QueueCancellationOutcome.missing,
                initial,
                final,
            )
        return RunCancellationResult(
            run_id,
            QueueCancellationOutcome.timeout,
            initial,
            final,
        )

    @property
    def redis(self) -> ArqRedis | None:
        """Return the active Redis pool, if the worker pool is started."""
        return self._redis

    async def ping(self) -> None:
        """Verify the queue's Redis adapter is connected and responsive."""
        if self._redis is None:
            raise RuntimeError("redis pool not connected")
        await self._redis.ping()

    @property
    def active_tasks(self) -> int:
        return self._active_tasks

    @property
    def active_browsers(self) -> int:
        return self._browser_limiter.active

    @property
    def browser_limiter(self) -> BrowserExecutionLimiter:
        """Return the process-wide limiter injected into worker target execution."""
        return self._browser_limiter

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def max_browsers(self) -> int:
        return self._max_browsers

    @property
    def background_ok(self) -> bool:
        """Whether the embedded arq runner is present and still executing."""

        return (
            self._started
            and self._runner_task is not None
            and not self._runner_task.done()
        )

    @property
    def background_detail(self) -> str | None:
        task = self._runner_task
        if not self._started:
            return "worker pool not started"
        if task is None:
            return "worker runner task missing"
        if task.cancelled():
            return "worker runner task stopped"
        if task.done():
            exception = task.exception()
            return (
                "worker runner task stopped"
                if exception is None
                else f"worker runner task failed: {type(exception).__name__}"
            )
        return None

    async def _run_job(
        self,
        _ctx: dict[str, Any],
        job_id: str,
        config_yaml: str,
        run_id: str | None = None,
        *,
        needs_browser: bool = False,
        trigger: str = "adhoc",
    ) -> dict[str, str]:
        """Execute one queued scrape job while tracking active job count."""
        # Retain this keyword for jobs already serialized in Redis. Browser
        # concurrency is enforced per target by the injected limiter.
        del needs_browser
        self._active_tasks += 1
        try:
            await self._execute(
                job_id, config_yaml, run_id=run_id, trigger=trigger,
            )
        finally:
            self._active_tasks -= 1
        return {"job_id": job_id}

    async def _execute(
        self,
        job_id: str,
        config_yaml: str,
        *,
        run_id: str | None = None,
        trigger: str = "adhoc",
    ) -> None:
        if self._task_handler is not None:
            await self._task_handler(
                job_id,
                config_yaml,
                run_id=run_id,
                trigger=trigger,
                browser_limiter=self._browser_limiter,
            )
            return
        raise RuntimeError("WorkerPool requires a task_handler to execute queued jobs")
