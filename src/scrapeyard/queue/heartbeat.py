"""Supervised per-run heartbeat refresh and local lease-loss signaling."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from scrapeyard.common.time import utc_now
from scrapeyard.storage.protocols import JobStore
from scrapeyard.storage.types import RunOwnershipError

logger = logging.getLogger(__name__)


class RunHeartbeatLeaseLost(RuntimeError):
    """Raised locally after the worker can no longer prove its run lease."""


class RunHeartbeat:
    """Refresh one owned run and cancel its worker when authority is lost.

    Heartbeat writes never overlap. A write exception is treated as transient
    until the running timeout has elapsed since the last successful persisted
    heartbeat. An ownership rejection is definitive and stops immediately.
    """

    def __init__(
        self,
        *,
        job_store: JobStore,
        job_id: str,
        run_id: str,
        last_success_at: datetime,
        interval_seconds: float,
        timeout_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        utc_clock: Callable[[], datetime] = utc_now,
        wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._job_store = job_store
        self.job_id = job_id
        self.run_id = run_id
        self.last_success_at = last_success_at
        self.interval_seconds = interval_seconds
        self.timeout_seconds = timeout_seconds
        self._monotonic = monotonic
        self._utc_clock = utc_clock
        self._wait = wait
        self._last_success_monotonic = monotonic()
        self._task: asyncio.Task[None] | None = None
        self._owner_task: asyncio.Task[object] | None = None
        self._lost_reason: str | None = None
        self.failure_count = 0

    @property
    def ownership_lost(self) -> bool:
        return self._lost_reason is not None

    @property
    def lost_reason(self) -> str | None:
        return self._lost_reason

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    def start(self, owner_task: asyncio.Task[object] | None = None) -> None:
        if self._task is not None:
            raise RuntimeError("Run heartbeat already started")
        self._owner_task = owner_task or asyncio.current_task()
        self._task = asyncio.create_task(
            self._run(),
            name=f"scrapeyard-heartbeat-{self.job_id}-{self.run_id}",
        )

    def ensure_owned(self) -> None:
        if self._lost_reason is not None:
            raise RunHeartbeatLeaseLost(self._lost_reason)

    async def stop(self) -> None:
        """Stop and await the heartbeat without surfacing a secondary failure."""
        task = self._task
        if task is None:
            return
        self._task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            logger.error(
                "Heartbeat shutdown failed job_id=%s run_id=%s "
                "last_heartbeat=%s failure_count=%d timeout_seconds=%s "
                "error_type=%s",
                self.job_id,
                self.run_id,
                self.last_success_at.isoformat(),
                self.failure_count,
                self.timeout_seconds,
                type(exc).__name__,
            )

    def _mark_lost(self, reason: str, *, lease_elapsed: float) -> None:
        if self._lost_reason is not None:
            return
        self._lost_reason = reason
        logger.error(
            "Run heartbeat ownership lost job_id=%s run_id=%s reason=%s "
            "last_heartbeat=%s failure_count=%d lease_elapsed_seconds=%.3f "
            "timeout_seconds=%s",
            self.job_id,
            self.run_id,
            reason,
            self.last_success_at.isoformat(),
            self.failure_count,
            lease_elapsed,
            self.timeout_seconds,
        )
        owner = self._owner_task
        if owner is not None and not owner.done():
            owner.cancel("run heartbeat ownership lost")

    async def _run(self) -> None:
        next_tick = self._monotonic() + self.interval_seconds
        while True:
            delay = max(0.0, next_tick - self._monotonic())
            try:
                await self._wait(delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                elapsed = self._monotonic() - self._last_success_monotonic
                self._mark_lost(
                    f"heartbeat scheduler failed: {type(exc).__name__}",
                    lease_elapsed=elapsed,
                )
                return

            heartbeat_at = self._utc_clock()
            try:
                await self._job_store.heartbeat_run(
                    self.job_id,
                    self.run_id,
                    heartbeat_at,
                )
            except asyncio.CancelledError:
                raise
            except RunOwnershipError:
                elapsed = self._monotonic() - self._last_success_monotonic
                self._mark_lost(
                    "storage rejected heartbeat ownership",
                    lease_elapsed=elapsed,
                )
                return
            except Exception as exc:
                self.failure_count += 1
                elapsed = self._monotonic() - self._last_success_monotonic
                remaining = max(0.0, self.timeout_seconds - elapsed)
                logger.warning(
                    "Heartbeat write failed job_id=%s run_id=%s "
                    "last_heartbeat=%s failure_count=%d lease_elapsed_seconds=%.3f "
                    "lease_remaining_seconds=%.3f timeout_seconds=%s error_type=%s",
                    self.job_id,
                    self.run_id,
                    self.last_success_at.isoformat(),
                    self.failure_count,
                    elapsed,
                    remaining,
                    self.timeout_seconds,
                    type(exc).__name__,
                )
                if elapsed >= self.timeout_seconds:
                    self._mark_lost(
                        "heartbeat persistence timeout exceeded",
                        lease_elapsed=elapsed,
                    )
                    return
            else:
                self.last_success_at = heartbeat_at
                self._last_success_monotonic = self._monotonic()
                self.failure_count = 0

            next_tick += self.interval_seconds
            now = self._monotonic()
            if next_tick <= now:
                next_tick = now + self.interval_seconds
