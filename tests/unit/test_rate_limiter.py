"""Unit tests for domain rate limiter implementations."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from scrapeyard.engine.rate_limiter import (
    LocalDomainRateLimiter,
    RedisDomainRateLimiter,
)


@pytest.mark.asyncio
async def test_local_first_acquire_is_immediate():
    """First call for a domain should not wait."""
    limiter = LocalDomainRateLimiter()
    start = time.monotonic()
    await limiter.acquire("example.com", 5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_local_second_acquire_waits():
    """Second call within the interval should wait."""
    limiter = LocalDomainRateLimiter()
    await limiter.acquire("example.com", 0.3)
    start = time.monotonic()
    await limiter.acquire("example.com", 0.3)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2  # allow small timing tolerance


@pytest.mark.asyncio
async def test_local_different_domains_independent():
    """Different domains do not block each other."""
    limiter = LocalDomainRateLimiter()
    await limiter.acquire("a.com", 5.0)
    start = time.monotonic()
    await limiter.acquire("b.com", 5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_local_expired_interval_no_wait():
    """After the interval has elapsed, no wait needed."""
    limiter = LocalDomainRateLimiter()
    await limiter.acquire("example.com", 0.1)
    await asyncio.sleep(0.15)
    start = time.monotonic()
    await limiter.acquire("example.com", 0.1)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


# --- RedisDomainRateLimiter (mock-Redis) ---


def _mock_redis():
    """Return an AsyncMock that simulates Redis GET/SET semantics."""
    store: dict[str, str] = {}
    redis = AsyncMock()

    async def _get(key):
        return store.get(key)

    async def _set(key, value, *, ex=None, nx=False):
        if nx and key in store:
            return None  # NX fails if key exists
        store[key] = value
        return True

    redis.get = AsyncMock(side_effect=_get)
    redis.set = AsyncMock(side_effect=_set)
    redis._store = store  # expose for test manipulation
    return redis


@pytest.mark.asyncio
async def test_redis_first_acquire_sets_key():
    """First acquire for a domain should SET the key via NX and return immediately."""
    redis = _mock_redis()
    limiter = RedisDomainRateLimiter(redis)
    start = time.monotonic()
    await limiter.acquire("example.com", 3.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1
    redis.set.assert_called_once()
    call_kwargs = redis.set.call_args
    assert call_kwargs.kwargs.get("nx") is True
    assert call_kwargs.kwargs.get("ex") == 4  # int(3.0) + 1


@pytest.mark.asyncio
async def test_redis_second_acquire_waits_when_interval_not_elapsed():
    """Second acquire should wait when the interval hasn't elapsed."""
    redis = _mock_redis()
    limiter = RedisDomainRateLimiter(redis)
    await limiter.acquire("example.com", 0.3)

    start = time.monotonic()
    await limiter.acquire("example.com", 0.3)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2  # waited for interval


@pytest.mark.asyncio
async def test_redis_overwrite_path_when_interval_elapsed():
    """When the interval has elapsed but key exists, should overwrite and return."""
    redis = _mock_redis()
    limiter = RedisDomainRateLimiter(redis)
    # Pre-seed a key with an old timestamp.
    redis._store["scrapeyard:rate:example.com"] = str(time.time() - 10.0)

    start = time.monotonic()
    await limiter.acquire("example.com", 3.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1  # no wait — interval already elapsed
    # Should have called set without nx (overwrite path).
    last_set_call = redis.set.call_args
    assert last_set_call.kwargs.get("nx", False) is False


@pytest.mark.asyncio
async def test_redis_nx_race_retries():
    """If another worker wins the NX race, limiter should retry and take the overwrite path."""
    redis = AsyncMock()
    get_count = 0

    async def _get(key):
        nonlocal get_count
        get_count += 1
        if get_count <= 1:
            return None  # first GET: key absent
        # second GET: another worker set it, but interval already elapsed
        return str(time.time() - 10.0)

    async def _set(key, value, *, ex=None, nx=False):
        if nx and get_count <= 1:
            return None  # NX fails — another worker won the race
        return True  # overwrite succeeds on retry

    redis.get = AsyncMock(side_effect=_get)
    redis.set = AsyncMock(side_effect=_set)

    limiter = RedisDomainRateLimiter(redis)
    await limiter.acquire("example.com", 0.15)
    # Should have called GET at least twice (retry after NX failure).
    assert redis.get.call_count >= 2


@pytest.mark.asyncio
async def test_redis_different_domains_independent():
    """Different domains should not interfere with each other."""
    redis = _mock_redis()
    limiter = RedisDomainRateLimiter(redis)
    await limiter.acquire("a.com", 5.0)
    start = time.monotonic()
    await limiter.acquire("b.com", 5.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_redis_ttl_minimum_is_two():
    """TTL should be at least 2, even for very small intervals."""
    redis = _mock_redis()
    limiter = RedisDomainRateLimiter(redis)
    await limiter.acquire("example.com", 0.1)
    call_kwargs = redis.set.call_args
    assert call_kwargs.kwargs.get("ex") == 2
