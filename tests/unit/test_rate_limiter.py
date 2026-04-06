"""Unit tests for engine/rate_limiter.py — Local and Redis implementations."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from scrapeyard.engine.rate_limiter import (
    LocalDomainRateLimiter,
    RedisDomainRateLimiter,
)


# ---------------------------------------------------------------------------
# LocalDomainRateLimiter
# ---------------------------------------------------------------------------


class TestLocalDomainRateLimiter:
    @pytest.mark.asyncio
    async def test_first_acquire_returns_immediately(self) -> None:
        limiter = LocalDomainRateLimiter()
        start = time.monotonic()
        await limiter.acquire("example.com", 1.0)
        assert time.monotonic() - start < 0.1

    @pytest.mark.asyncio
    async def test_second_acquire_waits(self) -> None:
        limiter = LocalDomainRateLimiter()
        await limiter.acquire("example.com", 0.2)
        start = time.monotonic()
        await limiter.acquire("example.com", 0.2)
        elapsed = time.monotonic() - start
        assert elapsed >= 0.15  # allow small tolerance

    @pytest.mark.asyncio
    async def test_different_domains_independent(self) -> None:
        limiter = LocalDomainRateLimiter()
        await limiter.acquire("a.com", 5.0)
        start = time.monotonic()
        await limiter.acquire("b.com", 5.0)
        assert time.monotonic() - start < 0.1


# ---------------------------------------------------------------------------
# RedisDomainRateLimiter
# ---------------------------------------------------------------------------


class TestRedisDomainRateLimiter:
    """Tests use a mock Redis — no real Redis needed."""

    def _make_limiter(
        self, evalsha_side_effect: list[list[int]] | None = None,
    ) -> tuple[RedisDomainRateLimiter, AsyncMock]:
        redis = AsyncMock()
        redis.script_load = AsyncMock(return_value="fake-sha")
        if evalsha_side_effect is not None:
            redis.evalsha = AsyncMock(side_effect=evalsha_side_effect)
        else:
            # Default: always acquired immediately.
            redis.evalsha = AsyncMock(return_value=[1, 0])
        limiter = RedisDomainRateLimiter(redis)
        return limiter, redis

    @pytest.mark.asyncio
    async def test_acquire_immediate(self) -> None:
        """Lua returns acquired=1 — should return immediately."""
        limiter, redis = self._make_limiter()
        await limiter.acquire("example.com", 2.0)
        redis.script_load.assert_awaited_once()
        redis.evalsha.assert_awaited_once()
        # Verify correct key format.
        call_args = redis.evalsha.call_args
        assert call_args[0][0] == "fake-sha"  # sha
        assert call_args[0][1] == 1  # numkeys
        assert call_args[0][2] == "scrapeyard:rate:example.com"

    @pytest.mark.asyncio
    async def test_acquire_waits_then_succeeds(self) -> None:
        """Lua returns wait-needed first, then acquired on retry."""
        limiter, redis = self._make_limiter(
            evalsha_side_effect=[
                [0, 100],  # wait 100ms
                [1, 0],    # acquired
            ],
        )
        with patch("scrapeyard.engine.rate_limiter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter.acquire("example.com", 2.0)
            mock_sleep.assert_awaited_once_with(0.1)  # 100ms / 1000
        assert redis.evalsha.await_count == 2

    @pytest.mark.asyncio
    async def test_script_load_cached(self) -> None:
        """script_load is called once even across multiple acquire() calls."""
        limiter, redis = self._make_limiter()
        await limiter.acquire("a.com", 1.0)
        await limiter.acquire("b.com", 1.0)
        redis.script_load.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ttl_minimum_is_2(self) -> None:
        """TTL should be at least 2 regardless of min_interval."""
        limiter, redis = self._make_limiter()
        await limiter.acquire("example.com", 0.5)
        call_args = redis.evalsha.call_args[0]
        ttl_str = call_args[5]  # 6th positional arg
        assert int(ttl_str) == 2

    @pytest.mark.asyncio
    async def test_ttl_scales_with_interval(self) -> None:
        """TTL = int(min_interval) + 1 when that exceeds 2."""
        limiter, redis = self._make_limiter()
        await limiter.acquire("example.com", 5.0)
        call_args = redis.evalsha.call_args[0]
        ttl_str = call_args[5]
        assert int(ttl_str) == 6  # int(5.0) + 1

    @pytest.mark.asyncio
    async def test_multiple_waits_before_acquire(self) -> None:
        """Multiple wait cycles before final acquisition."""
        limiter, redis = self._make_limiter(
            evalsha_side_effect=[
                [0, 200],  # wait 200ms
                [0, 50],   # wait 50ms
                [1, 0],    # acquired
            ],
        )
        with patch("scrapeyard.engine.rate_limiter.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await limiter.acquire("example.com", 2.0)
            assert mock_sleep.await_count == 2
            mock_sleep.assert_any_await(0.2)
            mock_sleep.assert_any_await(0.05)
        assert redis.evalsha.await_count == 3
