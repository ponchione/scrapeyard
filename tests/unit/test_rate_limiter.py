"""Unit tests for domain rate limiter implementations."""

from __future__ import annotations

import asyncio
import time

import pytest

from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter


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
