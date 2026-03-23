"""Domain rate limiter — local and Redis-backed implementations."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from arq.connections import ArqRedis

logger = logging.getLogger(__name__)


class DomainRateLimiter(Protocol):
    """Async interface for per-domain request throttling."""

    async def acquire(self, domain: str, min_interval: float) -> None:
        """Wait until min_interval seconds have passed since the last request
        to this domain, then record a new timestamp."""
        ...


class LocalDomainRateLimiter:
    """Per-invocation rate limiter (existing behavior, for testing/fallback)."""

    def __init__(self) -> None:
        self._last_request: dict[str, float] = {}

    async def acquire(self, domain: str, min_interval: float) -> None:
        now = time.monotonic()
        last = self._last_request.get(domain, 0.0)
        wait = min_interval - (now - last)
        if wait > 0:
            logger.debug(
                "Domain rate limit: waiting %.1fs for %s (local)", wait, domain,
            )
            await asyncio.sleep(wait)
        self._last_request[domain] = time.monotonic()


class RedisDomainRateLimiter:
    """Cross-job rate limiter using Redis SET with EX for TTL."""

    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis

    async def acquire(self, domain: str, min_interval: float) -> None:
        key = f"scrapeyard:rate:{domain}"
        ttl = max(int(min_interval) + 1, 2)
        while True:
            now = time.time()
            last_raw = await self._redis.get(key)
            if last_raw is not None:
                last = float(last_raw)
                wait = min_interval - (now - last)
                if wait > 0:
                    logger.debug(
                        "Domain rate limit: waiting %.1fs for %s (cross-job)",
                        wait, domain,
                    )
                    await asyncio.sleep(wait)
                    continue
                # Interval elapsed but key hasn't expired yet — overwrite.
                await self._redis.set(key, str(time.time()), ex=ttl)
                return
            # No existing key — use NX to prevent races.
            set_result = await self._redis.set(
                key, str(time.time()), ex=ttl, nx=True,
            )
            if set_result:
                return
            # Another worker won the race — re-check.
            await asyncio.sleep(0.1)
