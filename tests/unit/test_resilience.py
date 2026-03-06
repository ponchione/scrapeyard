"""Tests for retry, validation, and circuit breaker resilience primitives."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from scrapeyard.config.schema import BackoffStrategy, OnEmptyAction, RetryConfig, ValidationConfig
from scrapeyard.engine.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    ResultValidator,
    RetryHandler,
    RetryableError,
)


# --- RetryHandler ---


class TestRetryHandler:
    def _config(self, **overrides) -> RetryConfig:
        defaults = {"max_attempts": 3, "backoff": BackoffStrategy.exponential, "backoff_max": 30}
        defaults.update(overrides)
        return RetryConfig(**defaults)

    async def test_succeeds_first_try(self):
        handler = RetryHandler(self._config())
        fn = AsyncMock(return_value="ok")
        result = await handler.execute(fn)
        assert result == "ok"
        assert fn.call_count == 1

    async def test_retries_on_retryable_error(self):
        handler = RetryHandler(self._config(max_attempts=3, backoff=BackoffStrategy.fixed))
        fn = AsyncMock(side_effect=[RetryableError(503), RetryableError(503), "ok"])
        result = await handler.execute(fn)
        assert result == "ok"
        assert fn.call_count == 3

    async def test_exhausts_retries(self):
        handler = RetryHandler(self._config(max_attempts=2, backoff=BackoffStrategy.fixed))
        fn = AsyncMock(side_effect=RetryableError(503))
        with pytest.raises(RetryableError):
            await handler.execute(fn)
        assert fn.call_count == 2

    async def test_exponential_backoff_timing(self):
        handler = RetryHandler(self._config(max_attempts=3, backoff=BackoffStrategy.exponential))
        fn = AsyncMock(side_effect=[RetryableError(503), RetryableError(503), "ok"])
        start = asyncio.get_event_loop().time()
        await handler.execute(fn)
        elapsed = asyncio.get_event_loop().time() - start
        # 1s + 2s = 3s minimum
        assert elapsed >= 2.5

    async def test_linear_backoff(self):
        handler = RetryHandler(self._config(max_attempts=3, backoff=BackoffStrategy.linear))
        fn = AsyncMock(side_effect=[RetryableError(503), RetryableError(503), "ok"])
        start = asyncio.get_event_loop().time()
        await handler.execute(fn)
        elapsed = asyncio.get_event_loop().time() - start
        # 1s + 2s = 3s minimum
        assert elapsed >= 2.5

    async def test_backoff_capped_at_max(self):
        handler = RetryHandler(
            self._config(max_attempts=2, backoff=BackoffStrategy.exponential, backoff_max=1)
        )
        fn = AsyncMock(side_effect=[RetryableError(503), "ok"])
        start = asyncio.get_event_loop().time()
        await handler.execute(fn)
        elapsed = asyncio.get_event_loop().time() - start
        assert elapsed < 2.0

    async def test_non_retryable_error_propagates(self):
        handler = RetryHandler(self._config())
        fn = AsyncMock(side_effect=ValueError("bad"))
        with pytest.raises(ValueError, match="bad"):
            await handler.execute(fn)
        assert fn.call_count == 1


# --- ResultValidator ---


class TestResultValidator:
    def test_passes_with_sufficient_data(self):
        v = ResultValidator(ValidationConfig(min_results=1, required_fields=["title"]))
        result = v.validate([{"title": "hello"}])
        assert result.passed is True

    def test_fails_min_results(self):
        v = ResultValidator(ValidationConfig(min_results=5))
        result = v.validate([{"a": 1}])
        assert result.passed is False
        assert "at least 5" in result.message

    def test_fails_required_field_missing(self):
        v = ResultValidator(ValidationConfig(required_fields=["price"]))
        result = v.validate([{"title": "hello"}])
        assert result.passed is False
        assert "price" in result.message

    def test_fails_required_field_empty(self):
        v = ResultValidator(ValidationConfig(required_fields=["title"]))
        result = v.validate([{"title": ""}])
        assert result.passed is False

    def test_action_is_on_empty(self):
        v = ResultValidator(ValidationConfig(on_empty=OnEmptyAction.fail))
        result = v.validate([])
        assert result.action == OnEmptyAction.fail

    def test_passes_empty_config(self):
        v = ResultValidator(ValidationConfig())
        result = v.validate([])
        assert result.passed is True


# --- CircuitBreaker ---


class TestCircuitBreaker:
    def test_closed_by_default(self):
        cb = CircuitBreaker(max_consecutive_failures=3, cooldown_seconds=60)
        cb.check("example.com")  # should not raise

    def test_trips_after_max_failures(self):
        cb = CircuitBreaker(max_consecutive_failures=3, cooldown_seconds=60)
        cb.record_failure("example.com")
        cb.record_failure("example.com")
        cb.record_failure("example.com")
        with pytest.raises(CircuitOpenError):
            cb.check("example.com")

    def test_does_not_trip_below_threshold(self):
        cb = CircuitBreaker(max_consecutive_failures=3, cooldown_seconds=60)
        cb.record_failure("example.com")
        cb.record_failure("example.com")
        cb.check("example.com")  # should not raise

    def test_success_resets_counter(self):
        cb = CircuitBreaker(max_consecutive_failures=2, cooldown_seconds=60)
        cb.record_failure("example.com")
        cb.record_success("example.com")
        cb.record_failure("example.com")
        cb.check("example.com")  # should not raise — only 1 consecutive

    def test_cooldown_expires(self):
        cb = CircuitBreaker(max_consecutive_failures=1, cooldown_seconds=0)
        cb.record_failure("example.com")
        # Cooldown is 0s, so it should expire immediately
        time.sleep(0.01)
        cb.check("example.com")  # should not raise

    def test_isolates_domains(self):
        cb = CircuitBreaker(max_consecutive_failures=1, cooldown_seconds=60)
        cb.record_failure("bad.com")
        with pytest.raises(CircuitOpenError):
            cb.check("bad.com")
        cb.check("good.com")  # different domain, should not raise
