"""Redis-backed queue service with embedded arq worker execution."""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from time import monotonic
from typing import Any, Awaitable, Callable, Protocol, cast

from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.constants import abort_jobs_ss, in_progress_key_prefix, job_key_prefix, result_key_prefix
from arq.jobs import Job, ResultNotFound, serialize_job
from arq.utils import timestamp_ms
from arq.worker import Worker, func
from redis.exceptions import RedisError

from scrapeyard.common.async_tools import AwaitableCancelled, MonotonicDeadline
from scrapeyard.common.settings import get_settings
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter
from scrapeyard.queue.cancellation import (
    QueueCancellationOutcome,
    QueueDeliveryState as QueueDeliveryState,
    RunCancellationResult,
)
from scrapeyard.queue.memory import memory_headroom_mb, release_memory
from scrapeyard.queue.priority import (
    PRIORITIES,
    WeightedPriorityPolicy,
    priority_queue_names,
)
from scrapeyard.runtime.metrics import MEMORY_DEFERRALS, mark_last_success

_ARQ_FUNCTION_NAME = "scrape_job"
_KEEP_RESULT_SECONDS = 3600
# Shutdown stops draining active jobs this far before its grace expires, so
# cancelled runs can record their terminal state instead of staying running
# until the heartbeat timeout.
_CANCELLATION_RESERVE_MAX_SECONDS = 5.0
_CANCELLATION_RESERVE_FRACTION = 0.25
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
local orphans_removed = 0
local orphan_limit = tonumber(ARGV[3])
for index = 1, 3 do
    while true do
        local candidate = redis.call(
            'ZRANGEBYSCORE', KEYS[index], '-inf', ARGV[1], 'WITHSCORES', 'LIMIT', 0, 1
        )
        if #candidate == 0 then
            break
        end
        redis.call('ZREM', KEYS[index], candidate[1])
        if redis.call('EXISTS', ARGV[2] .. candidate[1]) == 1 then
            redis.call('ZADD', KEYS[4], candidate[2], candidate[1])
            return {candidate[1], KEYS[index]}
        end
        orphans_removed = orphans_removed + 1
        if orphans_removed >= orphan_limit then
            return {}
        end
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
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError
            snapshot_awaitable = self._pool._delivery_snapshot(self._run_id)
            snapshot = (
                await snapshot_awaitable
                if remaining is None
                else await asyncio.wait_for(snapshot_awaitable, timeout=remaining)
            )
            remaining = None if deadline is None else deadline - monotonic()
            if remaining is not None and remaining <= 0:
                raise asyncio.TimeoutError
            if snapshot.state is QueueDeliveryState.complete:
                if self._pool.redis is None:
                    raise RuntimeError("Redis disconnected while waiting for queue result")
                # Result keys are global to the arq job identity; queue name is
                # irrelevant once the result exists.
                result_awaitable = Job(
                    self._run_id,
                    redis=self._pool.redis,
                    _queue_name=self._pool._queue_name,
                ).result(timeout=0, poll_delay=poll_delay)
                result = (
                    await result_awaitable
                    if remaining is None
                    else await asyncio.wait_for(result_awaitable, timeout=remaining)
                )
                return cast(object, result)
            if snapshot.state is QueueDeliveryState.missing:
                raise ResultNotFound(
                    "Not waiting for job result because the delivery is not in "
                    "any configured priority queue"
                )
            if remaining is None:
                await asyncio.sleep(poll_delay)
            else:
                await asyncio.sleep(min(poll_delay, remaining))


class _PriorityWorker(Worker):
    """Single arq Worker with weighted admission from three intake queues."""

    # Within one poll, memory is sampled at most once and a deferral counted
    # at most once; outside a poll every start check samples afresh.
    _polling = False
    _poll_memory_available: bool | None = None
    _poll_deferred = False

    def __init__(
        self, *args: Any, priority_queues: dict[str, str],
        memory_available: Callable[[], bool], **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.priority_queues = priority_queues
        self.priority_policy = WeightedPriorityPolicy()
        self.memory_available = memory_available

    async def close(self) -> None:
        """Close arq 0.26.x without its deprecated redis-py compatibility call."""
        if not self._handle_signals:
            self.handle_sig(signal.SIGUSR1)
        if not self._pool:
            return
        # A job whose result could not be written, because Redis went away,
        # ends with that error: it is logged, and does not fail the close.
        outcomes = await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for job_id, outcome in zip(list(self.tasks), outcomes, strict=True):
            if isinstance(outcome, Exception):
                logger.warning(
                    "Queue delivery %s ended with %s while the worker closed",
                    job_id,
                    type(outcome).__name__,
                )
        try:
            await self.pool.delete(self.health_check_key)
        except (RedisError, OSError) as exc:
            # The key expires by itself; the connections must still be closed.
            logger.warning(
                "Could not remove the worker's health key while closing: %s",
                type(exc).__name__,
            )
        if self.on_shutdown:
            await self.on_shutdown(self.ctx)
        await self.pool.aclose(close_connection_pool=True)
        self._pool = None

    def _memory_available_for_start(self) -> bool:
        """Sample memory only when a start check needs it, once per poll."""
        available = self._poll_memory_available
        if available is None:
            available = self.memory_available()
            if self._polling:
                self._poll_memory_available = available
        if not available and not self._poll_deferred:
            MEMORY_DEFERRALS.labels("worker").inc()
            self._poll_deferred = self._polling
        return available

    async def _poll_iteration(self) -> None:
        self._polling = True
        try:
            # A full worker starts nothing, so it neither samples memory nor
            # counts a deferral.
            if (
                self.allow_pick_jobs
                and self.job_counter < self.max_jobs
                and self._memory_available_for_start()
            ):
                # arq 0.26.x keeps a running job in the execution queue until
                # finish_job() removes it. These ready members therefore already
                # include job_counter and must only be counted once.
                ready_execution_members = await self.pool.zcount(
                    self.queue_name,
                    min=float("-inf"),
                    max=timestamp_ms(),
                )
                available = max(0, self.max_jobs - ready_execution_members)
                for _ in range(available):
                    if not await self._admit_one():
                        break
            await super()._poll_iteration()
        finally:
            self._polling = False
            self._poll_memory_available = None
            self._poll_deferred = False
        mark_last_success("worker")

    async def start_jobs(self, job_ids: list[bytes]) -> None:
        # Jobs this process runs stay in the execution queue until they finish;
        # arq would spend four Redis round trips per poll rediscovering each.
        job_ids = [job_id for job_id in job_ids if not self._running_here(job_id)]
        if job_ids and not self._memory_available_for_start():
            # arq acknowledges queued aborts in run_job before calling the
            # scrape handler. Let those acknowledgements drain under pressure.
            scores = await self.pool.zmscore(abort_jobs_ss, [job_id.decode() for job_id in job_ids])
            job_ids = [job_id for job_id, score in zip(job_ids, scores, strict=True) if score is not None]
            if not job_ids:
                return
        await super().start_jobs(job_ids)

    def _running_here(self, job_id: bytes) -> bool:
        task = self.tasks.get(job_id.decode())
        return task is not None and not task.done()

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
                job_key_prefix,
                "100",
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
        *,
        run_id: str,
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
        payload_ttl_seconds: int = 604800,
        job_timeout_seconds: float = 300.0,
        browser_memory_reserve_mb: int = 512,
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
        self._payload_ttl_ms = payload_ttl_seconds * 1000
        self._job_timeout_seconds = job_timeout_seconds

        self._browser_limiter = BrowserExecutionLimiter(
            max_browsers,
            memory_limit_mb=memory_limit_mb,
            memory_reserve_mb=browser_memory_reserve_mb,
        )
        self._redis: ArqRedis | None = None
        self._worker: Worker | None = None
        self._runner_task: asyncio.Task[None] | None = None
        self._active_tasks = 0
        self._started = False
        self._stopping = False
        # Called once, with what ended it, when the embedded worker ends
        # outside stop(): the process then starts no queued run again.
        self.on_runner_exit: Callable[[BaseException | None], None] | None = None

    def _check_memory(self) -> bool:
        """Return True if more work may start below the memory ceilings."""
        return memory_headroom_mb(self._memory_limit_mb) > 0

    async def start(self) -> None:
        """Start the Redis connection and embedded arq worker."""
        if self._stopping:
            raise RuntimeError(
                "WorkerPool cannot start while a previous shutdown is unresolved"
            )
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

        try:
            await self._release_abandoned_deliveries(redis)
        except Exception:
            await redis.aclose(close_connection_pool=True)
            raise
        self._redis = redis
        self._worker = _PriorityWorker(
            functions=[func(cast(Any, self._run_job), name=_ARQ_FUNCTION_NAME, keep_result=_KEEP_RESULT_SECONDS)],
            redis_pool=self._redis,
            queue_name=self._queue_name,
            priority_queues=self._priority_queues,
            memory_available=self._check_memory,
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
        self._runner_task.add_done_callback(self._runner_finished)
        self._started = True

    async def _release_abandoned_deliveries(self, redis: ArqRedis) -> None:
        # A delivery the previous process was running keeps arq's in-progress
        # key for the job timeout when its end could not be written: Redis
        # was gone (which now ends the process, TD-89) or the process was
        # killed. Admission counts those deliveries as running, so a full set
        # of them would admit nothing new until the keys expired. One process
        # holds the queue (the instance guard), so none of them is running:
        # each is released to be delivered again, and the task handler skips
        # a run that is no longer queued.
        members = await redis.zrange(self._queue_name, 0, -1)
        keys = [
            f"{in_progress_key_prefix}{member.decode() if isinstance(member, bytes) else member}"
            for member in members
        ]
        if not keys:
            return
        released = int(await redis.delete(*keys))
        if released:
            logger.warning(
                "Released %d queue deliveries a previous process left in progress "
                "queue=%s",
                released,
                self._queue_name,
            )

    def _runner_finished(self, task: asyncio.Task[None]) -> None:
        # stop() ends the runner itself, and a cancellation from anywhere
        # else is the event loop closing; neither leaves work stranded.
        if self._stopping or self._runner_task is not task or task.cancelled():
            return
        error = task.exception()
        logger.critical(
            "Embedded worker ended outside a shutdown; no queued run can start "
            "in this process: %s",
            self.background_detail,
            exc_info=error,
        )
        if self.on_runner_exit is not None:
            self.on_runner_exit(error)

    async def stop(self, *, timeout: float | None = None) -> None:
        """Stop picking new jobs and close the embedded worker."""
        if not self._started:
            return

        if self._worker is None:
            raise RuntimeError("WorkerPool.stop() called but worker was never started")

        # A runner that had already ended was reported by _runner_finished.
        runner_ended_before_stop = (
            self._runner_task is not None and self._runner_task.done()
        )
        self._stopping = True
        self._worker.allow_pick_jobs = False
        grace_seconds = (
            get_settings().workers_shutdown_grace_seconds
            if timeout is None
            else max(0.0, timeout)
        )
        deadline = MonotonicDeadline(grace_seconds)
        cancellation_reserve = min(
            _CANCELLATION_RESERVE_MAX_SECONDS,
            grace_seconds * _CANCELLATION_RESERVE_FRACTION,
        )
        drain_seconds = grace_seconds - cancellation_reserve
        drain_timed_out = False
        unresolved_phases: list[str] = []
        pending = [
            task for task in self._worker.tasks.values()
            if not task.done()
        ]
        if pending:
            # asyncio.wait never cancels on timeout, so each job is cancelled
            # exactly once and its cancellation handler is not interrupted.
            _done, still_running = await asyncio.wait(pending, timeout=drain_seconds)
            if still_running:
                drain_timed_out = True
                for task in still_running:
                    task.cancel()
                # Cancelled jobs record their terminal state within the
                # reserve, before Redis and the worker are closed.
                await asyncio.wait(still_running, timeout=deadline.remaining)

        if self._worker.main_task is not None:
            self._worker.main_task.cancel()
        if self._runner_task is not None:
            try:
                await deadline.run(self._runner_task)
            except AwaitableCancelled:
                pass
            except asyncio.TimeoutError:
                unresolved_phases.append("runner")
            except Exception as exc:
                # The runner has ended, which is what the stop needs. What
                # ended it (Redis going away, most often) was reported if it
                # came before the stop, and is not a failure of the stop.
                if not runner_ended_before_stop:
                    logger.warning(
                        "Embedded worker ended during shutdown: %s", type(exc).__name__
                    )

        active_jobs_unresolved = any(not task.done() for task in pending)
        if active_jobs_unresolved:
            unresolved_phases.append("active_jobs")

        if unresolved_phases:
            if drain_timed_out and active_jobs_unresolved:
                logger.warning(
                    "Worker shutdown drain window expired; active-job cancellation "
                    "remains unresolved"
                )
            raise asyncio.TimeoutError(
                "Worker shutdown deadline exceeded in phase(s): "
                + ", ".join(unresolved_phases)
            )

        try:
            await deadline.run(self._worker.close())
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                "Worker shutdown deadline exceeded in phase(s): redis_close"
            ) from None

        self._redis = None
        self._worker = None
        self._runner_task = None
        self._started = False
        self._stopping = False
        if drain_timed_out:
            logger.info(
                "Worker shutdown drain window expired; active-job cancellation completed"
            )

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
        if self._stopping:
            raise RuntimeError(
                "WorkerPool cannot enqueue while a previous shutdown is unresolved"
            )
        if not self._started:
            # The lifespan owns start(); starting here would quietly reopen a
            # stopped pool, and two concurrent first calls would start twice.
            raise RuntimeError("WorkerPool.enqueue() called before start()")

        if self._redis is None:
            raise RuntimeError("WorkerPool.enqueue() called but Redis pool is not connected")
        if priority not in self._priority_queues:
            raise ValueError(f"Unknown queue priority: {priority!r}")
        if run_id is None:
            raise RuntimeError("WorkerPool.enqueue() requires a run_id")

        # The durable encrypted snapshot is already in SQLite.  Keep the
        # accepted method argument for source compatibility, but never place
        # secret-bearing YAML in Redis or its append-only persistence.
        del config_yaml

        enqueue_time_ms = timestamp_ms()
        payload = serialize_job(
            _ARQ_FUNCTION_NAME,
            (job_id, run_id),
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
                str(self._payload_ttl_ms),
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

    @property
    def shutdown_pending(self) -> bool:
        """Whether shutdown still owns live worker resources or tasks."""

        return self._stopping

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
        delivery_identity: str,
        run_id: str | None = None,
        *,
        needs_browser: bool = False,
        trigger: str = "adhoc",
    ) -> dict[str, str]:
        """Execute one queued scrape job while tracking active job count."""
        # Retain this keyword for jobs already serialized in Redis. Browser
        # concurrency is enforced per target by the injected limiter.
        del needs_browser
        effective_run_id = run_id or delivery_identity
        self._active_tasks += 1
        try:
            await self._execute(
                job_id, run_id=effective_run_id, trigger=trigger,
            )
        finally:
            self._active_tasks -= 1
            if self._active_tasks == 0:
                release_memory()
        return {"job_id": job_id}

    async def _execute(
        self,
        job_id: str,
        *,
        run_id: str,
        trigger: str = "adhoc",
    ) -> None:
        if self._task_handler is not None:
            await self._task_handler(
                job_id,
                run_id=run_id,
                trigger=trigger,
                browser_limiter=self._browser_limiter,
            )
            return
        raise RuntimeError("WorkerPool requires a task_handler to execute queued jobs")
