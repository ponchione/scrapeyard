"""Resilience primitives: retry, validation, and circuit breaker."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from scrapeyard.common.budgets import RunBudget
from scrapeyard.config.schema import BackoffStrategy, OnEmptyAction, RetryConfig, ValidationConfig
from scrapeyard.queue.cancellation import (
    CancellationCheckpoint,
    cancellation_checkpoint,
)
from scrapeyard.runtime.metrics import RETRIES
from scrapeyard.engine.url_guard import URLResolutionError

T = TypeVar("T")


class RetryableError(Exception):
    """Raised when a retryable HTTP status is encountered."""

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        super().__init__(message or f"HTTP {status}")


class RetryHandler:
    """Wraps an async callable with configurable retry and backoff.

    Parameters
    ----------
    config:
        Retry configuration from the scrape config.
    """

    def __init__(
        self,
        config: RetryConfig,
        budget: RunBudget | None = None,
        cancellation_guard: CancellationCheckpoint | None = None,
    ) -> None:
        self._max_attempts = config.max_attempts
        self._backoff = config.backoff
        self._backoff_max = config.backoff_max
        self._budget = budget
        self._cancellation_guard = cancellation_guard

    def _delay(self, attempt: int) -> float:
        """Calculate delay in seconds for the given attempt (0-indexed)."""
        if self._backoff == BackoffStrategy.fixed:
            delay = 1.0
        elif self._backoff == BackoffStrategy.linear:
            delay = float(attempt + 1)
        else:  # exponential
            delay = float(2**attempt)
        return min(delay, self._backoff_max)

    async def execute(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Call *fn* with retries on :class:`RetryableError`."""
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            await cancellation_checkpoint(
                self._cancellation_guard,
                "before_retry_attempt",
            )
            try:
                result = await fn(*args, **kwargs)
                await cancellation_checkpoint(
                    self._cancellation_guard,
                    "after_retry_attempt",
                )
                return result
            except (RetryableError, URLResolutionError) as exc:
                last_exc = exc
                if attempt < self._max_attempts - 1:
                    RETRIES.labels("scrape", "scheduled").inc()
                    await cancellation_checkpoint(
                        self._cancellation_guard,
                        "before_retry_backoff",
                    )
                    delay = self._delay(attempt)
                    if self._budget is None:
                        await asyncio.sleep(delay)
                    else:
                        await self._budget.sleep(delay)
                    await cancellation_checkpoint(
                        self._cancellation_guard,
                        "after_retry_backoff",
                    )
        if last_exc is None:
            raise RuntimeError("RetryHandler exhausted attempts without catching an exception")
        RETRIES.labels("scrape", "exhausted").inc()
        raise last_exc


@dataclass
class ValidationResult:
    """Outcome of a result validation check."""

    passed: bool
    action: OnEmptyAction
    message: str = ""


class ResultValidator:
    """Validates scraped data against :class:`ValidationConfig` rules."""

    _HIDDEN_PRICE_VISIBILITIES = {"map", "cart_only", "call_for_price"}

    def __init__(self, config: ValidationConfig) -> None:
        self._required_fields = config.required_fields
        self._min_results = config.min_results
        self._on_empty = config.on_empty

    def _has_required_field(self, field: str, record: dict[str, Any]) -> bool:
        value = record.get(field)
        if isinstance(value, str):
            if value.strip():
                return True
        elif isinstance(value, list | tuple):
            if any(
                item is not None and (not isinstance(item, str) or item.strip()) for item in value
            ):
                return True
        elif value is not None:
            return True
        if field != "price":
            return False
        return record.get("pricing_visibility") in self._HIDDEN_PRICE_VISIBILITIES

    def validate(
        self,
        data: list[dict[str, Any]],
        *,
        budget: RunBudget | None = None,
    ) -> ValidationResult:
        if budget is not None:
            budget.check_deadline()
        if len(data) < self._min_results:
            return ValidationResult(
                passed=False,
                action=self._on_empty,
                message=f"Expected at least {self._min_results} results, got {len(data)}",
            )

        for field in self._required_fields:
            if budget is not None:
                budget.check_deadline()
            for i, record in enumerate(data):
                if budget is not None and i % 64 == 0:
                    budget.check_deadline()
                if not self._has_required_field(field, record):
                    return ValidationResult(
                        passed=False,
                        action=self._on_empty,
                        message=f"Required field {field!r} is empty in record {i}",
                    )

        return ValidationResult(passed=True, action=self._on_empty)


class CircuitOpenError(Exception):
    """Raised when a circuit breaker is open for a domain."""

    def __init__(self, domain: str, cooldown_remaining: float) -> None:
        self.domain = domain
        self.cooldown_remaining = cooldown_remaining
        super().__init__(f"Circuit open for {domain} ({cooldown_remaining:.0f}s remaining)")


class CircuitState(str, Enum):
    """Explicit lifecycle states for one domain circuit."""

    closed = "closed"
    open = "open"
    half_open = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitProbe:
    """Capability granted to the sole caller admitted while half-open."""

    domain: str
    generation: int


@dataclass(slots=True)
class _DomainCircuit:
    state: CircuitState = CircuitState.closed
    failures: int = 0
    opened_at: float = 0.0
    generation: int = 0


class CircuitBreaker:
    """Per-domain circuit breaker that trips after consecutive failures.

    Parameters
    ----------
    max_consecutive_failures:
        Number of consecutive failures before tripping.
    cooldown_seconds:
        How long to stay open before allowing a probe request.
    """

    def __init__(
        self,
        max_consecutive_failures: int,
        cooldown_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_consecutive_failures < 1:
            raise ValueError("max_consecutive_failures must be positive")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds cannot be negative")
        self._max_failures = max_consecutive_failures
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._circuits: dict[str, _DomainCircuit] = {}
        self._lock = threading.RLock()

    def check(self, domain: str) -> CircuitProbe | None:
        """Admit closed work or atomically grant the sole half-open probe."""

        with self._lock:
            circuit = self._circuits.get(domain)
            if circuit is None or circuit.state is CircuitState.closed:
                return None
            elapsed = self._clock() - circuit.opened_at
            if circuit.state is CircuitState.open and elapsed >= self._cooldown:
                circuit.state = CircuitState.half_open
                circuit.generation += 1
                return CircuitProbe(domain, circuit.generation)
            remaining = max(0.0, self._cooldown - elapsed)
            raise CircuitOpenError(domain, remaining)

    def record_success(self, domain: str, probe: CircuitProbe | None = None) -> None:
        """Close a circuit after ordinary success or its authorized probe."""

        with self._lock:
            circuit = self._circuits.get(domain)
            if circuit is None:
                return
            if circuit.state is CircuitState.closed:
                self._circuits.pop(domain, None)
                return
            if circuit.state is CircuitState.half_open and self._probe_matches(
                circuit,
                domain,
                probe,
            ):
                self._circuits.pop(domain, None)

    def record_failure(self, domain: str, probe: CircuitProbe | None = None) -> None:
        """Count a transient failure or reopen after a failed probe."""

        with self._lock:
            circuit = self._circuits.setdefault(domain, _DomainCircuit())
            if circuit.state is CircuitState.half_open:
                if not self._probe_matches(circuit, domain, probe):
                    return
                circuit.state = CircuitState.open
                circuit.failures = self._max_failures
                circuit.opened_at = self._clock()
                return
            if circuit.state is CircuitState.open:
                return
            circuit.failures += 1
            if circuit.failures >= self._max_failures:
                circuit.state = CircuitState.open
                circuit.opened_at = self._clock()

    def abort_probe(self, domain: str, probe: CircuitProbe | None) -> None:
        """Release a cancelled/local-failure probe without penalizing the domain."""

        with self._lock:
            circuit = self._circuits.get(domain)
            if circuit is None or not self._probe_matches(circuit, domain, probe):
                return
            circuit.state = CircuitState.open
            # Preserve the already-expired cooldown so another caller may probe.
            circuit.opened_at = self._clock() - self._cooldown

    def state(self, domain: str) -> CircuitState:
        """Return one domain's current state for diagnostics and tests."""

        with self._lock:
            circuit = self._circuits.get(domain)
            return CircuitState.closed if circuit is None else circuit.state

    @staticmethod
    def _probe_matches(
        circuit: _DomainCircuit,
        domain: str,
        probe: CircuitProbe | None,
    ) -> bool:
        return bool(
            circuit.state is CircuitState.half_open
            and probe is not None
            and probe.domain == domain
            and probe.generation == circuit.generation
        )
