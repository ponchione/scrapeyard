"""Run-scoped execution and output budget enforcement."""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, NoReturn, TypeVar

from scrapeyard.common.async_tools import cancel_task_nowait


T = TypeVar("T")
NumericLimit = int | float
_DEFAULT_MAX_DURATION_SECONDS = 900.0
_DEFAULT_MAX_FETCHED_BYTES = 104857600
_DEFAULT_MAX_EXTRACTED_RECORDS = 100000
_DEFAULT_MAX_SERIALIZED_RESULT_BYTES = 52428800
_DEFAULT_MAX_BROWSER_DEBUG_BYTES = 26214400


def _numeric_setting(settings: Any, name: str, default: NumericLimit) -> NumericLimit:
    value = getattr(settings, name, default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return default


class BudgetLimitName(str, Enum):
    """Stable names used in structured budget diagnostics."""

    run_duration_seconds = "run_duration_seconds"
    fetched_bytes = "fetched_bytes"
    extracted_records = "extracted_records"
    serialized_result_bytes = "serialized_result_bytes"
    browser_debug_bytes = "browser_debug_bytes"


@dataclass(eq=False)
class BudgetExceeded(Exception):
    """Raised when a hard per-run service limit is exceeded."""

    limit_name: BudgetLimitName
    configured_limit: NumericLimit
    observed_amount: NumericLimit

    def __post_init__(self) -> None:
        super().__init__(
            f"Run budget exceeded: {self.limit_name.value} "
            f"configured={self.configured_limit} observed={self.observed_amount}"
        )

    def as_dict(self) -> dict[str, NumericLimit | str]:
        """Return the stable structured representation used by APIs and results."""
        return {
            "limit_name": self.limit_name.value,
            "configured_limit": self.configured_limit,
            "observed_amount": self.observed_amount,
        }


class RunBudget:
    """Concurrency-safe aggregate counters plus one monotonic run deadline."""

    def __init__(
        self,
        *,
        max_duration_seconds: float,
        max_fetched_bytes: int,
        max_extracted_records: int,
        max_serialized_result_bytes: int,
        max_browser_debug_bytes: int,
        clock: Any = time.monotonic,
    ) -> None:
        self.max_duration_seconds = max_duration_seconds
        self.max_fetched_bytes = max_fetched_bytes
        self.max_extracted_records = max_extracted_records
        self.max_serialized_result_bytes = max_serialized_result_bytes
        self.max_browser_debug_bytes = max_browser_debug_bytes
        self._clock = clock
        self.started_monotonic = float(clock())
        self.deadline_monotonic = self.started_monotonic + max_duration_seconds
        self._fetched_bytes = 0
        self._extracted_records = 0
        self._browser_debug_bytes = 0
        self._estimated_result_bytes = 0
        self._counter_lock = asyncio.Lock()
        self._record_counter_lock = threading.Lock()
        self._result_counter_lock = threading.Lock()
        self._exhausted: BudgetExceeded | None = None

    @classmethod
    def from_settings(cls, settings: Any, *, clock: Any = time.monotonic) -> RunBudget:
        """Create a budget from the central service settings object."""
        return cls(
            max_duration_seconds=float(
                _numeric_setting(
                    settings,
                    "run_max_duration_seconds",
                    _DEFAULT_MAX_DURATION_SECONDS,
                )
            ),
            max_fetched_bytes=int(
                _numeric_setting(settings, "run_max_fetched_bytes", _DEFAULT_MAX_FETCHED_BYTES)
            ),
            max_extracted_records=int(
                _numeric_setting(
                    settings,
                    "run_max_extracted_records",
                    _DEFAULT_MAX_EXTRACTED_RECORDS,
                )
            ),
            max_serialized_result_bytes=int(
                _numeric_setting(
                    settings,
                    "run_max_serialized_result_bytes",
                    _DEFAULT_MAX_SERIALIZED_RESULT_BYTES,
                )
            ),
            max_browser_debug_bytes=int(
                _numeric_setting(
                    settings,
                    "run_max_browser_debug_bytes",
                    _DEFAULT_MAX_BROWSER_DEBUG_BYTES,
                )
            ),
            clock=clock,
        )

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, float(self._clock()) - self.started_monotonic)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - float(self._clock()))

    @property
    def fetched_bytes(self) -> int:
        return self._fetched_bytes

    @property
    def extracted_records(self) -> int:
        return self._extracted_records

    @property
    def browser_debug_bytes(self) -> int:
        return self._browser_debug_bytes

    @property
    def estimated_result_bytes(self) -> int:
        return self._estimated_result_bytes

    @property
    def remaining_browser_debug_bytes(self) -> int:
        return max(0, self.max_browser_debug_bytes - self._browser_debug_bytes)

    @property
    def remaining_fetched_bytes(self) -> int:
        return max(0, self.max_fetched_bytes - self._fetched_bytes)

    def _duration_error(self) -> BudgetExceeded:
        return BudgetExceeded(
            BudgetLimitName.run_duration_seconds,
            self.max_duration_seconds,
            self.elapsed_seconds,
        )

    def check_deadline(self) -> None:
        """Raise immediately if a hard budget or the monotonic deadline is exhausted."""
        if self._exhausted is not None:
            raise self._exhausted
        if float(self._clock()) >= self.deadline_monotonic:
            self._exhausted = self._duration_error()
            raise self._exhausted

    def exhaust_duration(self) -> NoReturn:
        """Persist and raise the stable duration failure after transport cleanup."""

        if self._exhausted is None:
            self._exhausted = self._duration_error()
        raise self._exhausted

    async def check_fetched_capacity(self, amount: int) -> None:
        """Reject a trustworthy declared body size without reserving it."""

        if amount < 0:
            raise ValueError("Fetched byte amount cannot be negative")
        self.check_deadline()
        async with self._counter_lock:
            observed = self._fetched_bytes + amount
            if observed > self.max_fetched_bytes:
                self._exhausted = BudgetExceeded(
                    BudgetLimitName.fetched_bytes,
                    self.max_fetched_bytes,
                    observed,
                )
                raise self._exhausted

    async def wait_for(self, awaitable: Awaitable[T]) -> T:
        """Await work only for the remaining overall run duration.

        ``asyncio.wait`` is used instead of ``wait_for`` so an inner operation's
        own ``TimeoutError`` is not mistaken for overall deadline exhaustion.
        """
        try:
            self.check_deadline()
        except BaseException:
            if isinstance(awaitable, asyncio.Future):
                cancel_task_nowait(awaitable)
            elif inspect.iscoroutine(awaitable):
                awaitable.close()
            raise

        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.remaining_seconds)
        except BaseException:
            if not task.done():
                cancel_task_nowait(task)
            raise
        if task in done:
            return task.result()
        cancel_task_nowait(task)
        self._exhausted = self._duration_error()
        raise self._exhausted

    async def wait_for_owned(self, awaitable: Awaitable[T]) -> T:
        """Enforce the deadline while retaining ownership through cancellation.

        Use this for browser work whose external concurrency permit must not be
        released until the browser coroutine has acknowledged cancellation.
        """

        try:
            self.check_deadline()
        except BaseException:
            if isinstance(awaitable, asyncio.Future):
                await self._cancel_owned(awaitable)
            elif inspect.iscoroutine(awaitable):
                awaitable.close()
            raise

        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.remaining_seconds)
        except BaseException:
            if not task.done():
                await self._cancel_owned(task)
            raise
        if task in done:
            return task.result()
        await self._cancel_owned(task)
        self._exhausted = self._duration_error()
        raise self._exhausted

    @staticmethod
    async def _cancel_owned(task: asyncio.Future[Any]) -> None:
        """Wait for cancellation acknowledgement despite repeated caller cancels."""

        task.cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            with suppress(Exception):
                task.result()

    async def sleep(self, delay_seconds: float) -> None:
        """Sleep under the one overall deadline."""
        if delay_seconds <= 0:
            self.check_deadline()
            return
        await self.wait_for(asyncio.sleep(delay_seconds))

    async def consume_fetched_bytes(self, amount: int) -> None:
        """Atomically account a reliably measured basic-fetch response body."""
        if amount < 0:
            raise ValueError("Fetched byte amount cannot be negative")
        self.check_deadline()
        async with self._counter_lock:
            self.check_deadline()
            observed = self._fetched_bytes + amount
            if observed > self.max_fetched_bytes:
                self._exhausted = BudgetExceeded(
                    BudgetLimitName.fetched_bytes,
                    self.max_fetched_bytes,
                    observed,
                )
                raise self._exhausted
            self._fetched_bytes = observed

    async def consume_extracted_records(self, amount: int) -> None:
        """Compatibility wrapper for synchronous record reservation."""

        self.reserve_extracted_records(amount)

    def reserve_extracted_records(self, amount: int) -> None:
        """Synchronously reserve records before worker-thread extraction.

        A page is accepted or rejected as one unit. Callers reserve its match
        count before constructing selector dictionaries so concurrent targets
        cannot materialize records beyond the aggregate run ceiling.
        """

        if amount < 0:
            raise ValueError("Extracted record amount cannot be negative")
        self.check_deadline()
        with self._record_counter_lock:
            self.check_deadline()
            observed = self._extracted_records + amount
            if observed > self.max_extracted_records:
                self._exhausted = BudgetExceeded(
                    BudgetLimitName.extracted_records,
                    self.max_extracted_records,
                    observed,
                )
                raise self._exhausted
            self._extracted_records = observed

    def enforce_serialized_result_bytes(self, amount: int) -> None:
        """Reject an exact serialized payload size above the configured ceiling."""
        self.check_deadline()
        if amount > self.max_serialized_result_bytes:
            self._exhausted = BudgetExceeded(
                BudgetLimitName.serialized_result_bytes,
                self.max_serialized_result_bytes,
                amount,
            )
            raise self._exhausted

    def reserve_estimated_result_bytes(self, amount: int) -> None:
        """Reserve extracted JSON bytes before retaining another value.

        Extraction is synchronous today, but a regular lock keeps this counter
        safe if extraction later moves to worker threads.  Exact serialization
        remains the final authority for non-extraction metadata and escaping.
        """

        if amount < 0:
            raise ValueError("Estimated result byte amount cannot be negative")
        self.check_deadline()
        with self._result_counter_lock:
            self.check_deadline()
            observed = self._estimated_result_bytes + amount
            if observed > self.max_serialized_result_bytes:
                self._exhausted = BudgetExceeded(
                    BudgetLimitName.serialized_result_bytes,
                    self.max_serialized_result_bytes,
                    observed,
                )
                raise self._exhausted
            self._estimated_result_bytes = observed

    async def reserve_browser_debug_bytes(
        self,
        requested: int,
        *,
        allow_partial: bool = False,
    ) -> int:
        """Reserve supplementary debug space without failing the run.

        Returns the granted byte count. Screenshots request all-or-nothing
        reservations; excerpts may request a partial reservation for truncation.
        """
        if requested < 0:
            raise ValueError("Browser debug byte amount cannot be negative")
        self.check_deadline()
        async with self._counter_lock:
            self.check_deadline()
            remaining = self.max_browser_debug_bytes - self._browser_debug_bytes
            granted = min(requested, remaining) if allow_partial else (
                requested if requested <= remaining else 0
            )
            self._browser_debug_bytes += granted
            return granted

    async def release_browser_debug_bytes(self, amount: int) -> None:
        """Return a reservation when an artifact could not be written."""
        if amount < 0:
            raise ValueError("Browser debug byte amount cannot be negative")
        async with self._counter_lock:
            self._browser_debug_bytes = max(0, self._browser_debug_bytes - amount)
