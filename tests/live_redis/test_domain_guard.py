"""Live Redis coverage for the shared per-domain page budget and cooldown."""

from __future__ import annotations

import asyncio

import pytest

from scrapeyard.api.dependencies import get_worker_pool
from scrapeyard.engine.domain_guard import DomainDailyLimitReached, RedisDomainGuard


@pytest.mark.asyncio
@pytest.mark.live_redis
async def test_concurrent_workers_never_exceed_the_daily_page_limit(live_app):
    del live_app
    redis = get_worker_pool().redis
    assert redis is not None
    workers = [RedisDomainGuard(redis, day=lambda: "20260101") for _ in range(4)]

    async def attempt(guard: RedisDomainGuard) -> bool:
        try:
            await guard.consume_page("example.test", 10)
        except DomainDailyLimitReached:
            return False
        return True

    outcomes = await asyncio.gather(*(attempt(workers[i % 4]) for i in range(40)))
    assert sum(outcomes) == 10
    key = "scrapeyard:domain:pages:example.test:20260101"
    assert int(await redis.get(key)) == 10
    assert 0 < await redis.ttl(key) <= 172800

    await workers[0].start_cooldown("example.test", 60)
    assert 59 < await workers[1].cooldown_remaining("example.test") <= 60

    await redis.script_flush()
    assert await workers[2].consume_page("other.test", 0) == 1

    await workers[3].clear("example.test")
    status = await workers[0].status("example.test")
    assert (status.pages_today, status.cooldown_remaining_seconds) == (0, 0)
