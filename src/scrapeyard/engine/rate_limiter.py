"""Domain rate limiter — local and Redis-backed implementations."""

from __future__ import annotations

import asyncio
import inspect
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
    """Cross-job rate limiter using an atomic Lua script.

    The Lua script atomically reads the last-request timestamp, checks
    whether *min_interval* has elapsed, and — only then — writes the new
    timestamp.  This eliminates the TOCTOU race where two workers could
    both read the old timestamp and clobber each other's SET.

    Return values from the Lua script:
        1  — acquired, proceed immediately
        0  — not yet, wait *remaining* seconds (returned as second value)
    """

    # Lua script: atomic check-and-set for rate limiting.
    # KEYS[1] = rate-limit key
    # ARGV[1] = min_interval (float, seconds)
    # ARGV[2] = now          (float, epoch seconds)
    # ARGV[3] = ttl          (int, seconds for key expiry)
    # Returns {1} on success, {0, remaining_ms} when caller must wait.
    _LUA_ACQUIRE = """\
local key = KEYS[1]
local min_interval = tonumber(ARGV[1])
local now = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local last = redis.call('GET', key)
if last == false then
    redis.call('SET', key, tostring(now), 'EX', ttl)
    return {1, 0}
end
local elapsed = now - tonumber(last)
if elapsed >= min_interval then
    redis.call('SET', key, tostring(now), 'EX', ttl)
    return {1, 0}
end
local remaining_ms = math.ceil((min_interval - elapsed) * 1000)
return {0, remaining_ms}
"""

    def __init__(self, redis: ArqRedis) -> None:
        self._redis = redis
        self._script_sha: str | None = None

    async def _ensure_script(self) -> str:
        """Load the Lua script into Redis (cached after first call)."""
        if self._script_sha is None:
            self._script_sha = await self._redis.script_load(self._LUA_ACQUIRE)
        return self._script_sha

    async def acquire(self, domain: str, min_interval: float) -> None:
        key = f"scrapeyard:rate:{domain}"
        ttl = max(int(min_interval) + 1, 2)
        sha = await self._ensure_script()

        while True:
            now = time.time()
            eval_result = self._redis.evalsha(
                sha, 1, key, str(min_interval), str(now), str(ttl),
            )
            result = await eval_result if inspect.isawaitable(eval_result) else eval_result
            acquired = int(result[0])
            if acquired:
                return
            remaining_ms = int(result[1])
            wait = remaining_ms / 1000.0
            logger.debug(
                "Domain rate limit: waiting %.1fs for %s (cross-job)",
                wait, domain,
            )
            await asyncio.sleep(wait)
