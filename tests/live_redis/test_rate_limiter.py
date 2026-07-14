"""Live Redis coverage for the atomic domain rate-limit script."""

from __future__ import annotations

import pytest

from scrapeyard.api.dependencies import get_worker_pool
from scrapeyard.engine.rate_limiter import RedisDomainRateLimiter


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_rate_limit_script_handles_equal_forward_and_decreasing_time(live_app):
    del live_app
    redis = get_worker_pool().redis
    assert redis is not None
    seconds, microseconds = await redis.time()
    now = float(seconds) + float(microseconds) / 1_000_000
    script = RedisDomainRateLimiter._LUA_ACQUIRE

    equal_key = "scrapeyard:rate:test-equal"
    await redis.set(equal_key, str(now), ex=2)
    equal = await redis.eval(script, 1, equal_key, "1", "2")
    assert int(equal[0]) == 0
    assert 0 < int(equal[1]) <= 1000

    forward_key = "scrapeyard:rate:test-forward"
    await redis.set(forward_key, str(now - 2), ex=2)
    forward = await redis.eval(script, 1, forward_key, "1", "2")
    assert [int(value) for value in forward] == [1, 0]

    decreasing_key = "scrapeyard:rate:test-decreasing"
    await redis.set(decreasing_key, str(now + 100), ex=2)
    decreasing = await redis.eval(script, 1, decreasing_key, "1", "2")
    assert [int(value) for value in decreasing] == [0, 1000]
    reset_timestamp = float(await redis.get(decreasing_key))
    assert reset_timestamp < now + 10
