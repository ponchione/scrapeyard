"""Resilience primitives: retry, validation, and circuit breaker."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from scrapeyard.config.schema import BackoffStrategy, OnEmptyAction, RetryConfig, ValidationConfig

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

    def __init__(self, config: RetryConfig) -> None:
        self._max_attempts = config.max_attempts
        self._backoff = config.backoff
        self._backoff_max = config.backoff_max
        self._retryable_status = set(config.retryable_status)

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
            try:
                return await fn(*args, **kwargs)
            except RetryableError as exc:
                last_exc = exc
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(self._delay(attempt))
        assert last_exc is not None, "RetryHandler exhausted attempts without catching an exception"
        raise last_exc


@dataclass
class ValidationResult:
    """Outcome of a result validation check."""

    passed: bool
    action: OnEmptyAction
    message: str = ""


class ResultValidator:
    """Validates scraped data against :class:`ValidationConfig` rules."""

    def __init__(self, config: ValidationConfig) -> None:
        self._required_fields = config.required_fields
        self._min_results = config.min_results
        self._on_empty = config.on_empty

    def validate(self, data: list[dict[str, Any]]) -> ValidationResult:
        if len(data) < self._min_results:
            return ValidationResult(
                passed=False,
                action=self._on_empty,
                message=f"Expected at least {self._min_results} results, got {len(data)}",
            )

        for field in self._required_fields:
            for i, record in enumerate(data):
                value = record.get(field)
                if value is None or value == "":
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


class CircuitBreaker:
    """Per-domain circuit breaker that trips after consecutive failures.

    Parameters
    ----------
    max_consecutive_failures:
        Number of consecutive failures before tripping.
    cooldown_seconds:
        How long to stay open before allowing a probe request.
    """

    def __init__(self, max_consecutive_failures: int, cooldown_seconds: int) -> None:
        self._max_failures = max_consecutive_failures
        self._cooldown = cooldown_seconds
        self._failures: dict[str, int] = {}
        self._tripped_at: dict[str, float] = {}

    def check(self, domain: str) -> None:
        """Raise :class:`CircuitOpenError` if the breaker is open for *domain*."""
        if domain in self._tripped_at:
            elapsed = time.monotonic() - self._tripped_at[domain]
            if elapsed < self._cooldown:
                raise CircuitOpenError(domain, self._cooldown - elapsed)
            # Cooldown expired — allow probe, reset state.
            del self._tripped_at[domain]
            self._failures.pop(domain, None)

    def record_success(self, domain: str) -> None:
        """Reset failure counter for *domain*."""
        self._failures.pop(domain, None)
        self._tripped_at.pop(domain, None)

    def record_failure(self, domain: str) -> None:
        """Increment failure counter; trip the breaker if threshold reached."""
        self._failures[domain] = self._failures.get(domain, 0) + 1
        if self._failures[domain] >= self._max_failures:
            self._tripped_at[domain] = time.monotonic()
