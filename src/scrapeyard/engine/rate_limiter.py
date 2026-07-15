"""Domain rate limiter — local and Redis-backed implementations."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Protocol

from arq.connections import ArqRedis
from redis.exceptions import NoScriptError

from scrapeyard.runtime.metrics import observe_rate_limit_state, observe_rate_limit_wait

logger = logging.getLogger(__name__)
_DEFAULT_LOCAL_RETENTION_SECONDS = 3600.0


class DomainRateLimiter(Protocol):
    """Async interface for per-domain request throttling."""

    async def acquire(self, domain: str, min_interval: float) -> None:
        """Wait until min_interval seconds have passed since the last request
        to this domain, then record a new timestamp."""
        ...


class LocalDomainRateLimiter:
    """Per-invocation rate limiter (existing behavior, for testing/fallback)."""

    def __init__(
        self,
        retention_seconds: float = _DEFAULT_LOCAL_RETENTION_SECONDS,
        max_domains: int = 10000,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        if max_domains < 1:
            raise ValueError("max_domains must be positive")
        self._last_request: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._domain_users: dict[str, int] = {}
        self._last_prune_at = 0.0
        self._retention_seconds = retention_seconds
        self._max_domains = max_domains
        self._state_condition = asyncio.Condition()

    async def acquire(self, domain: str, min_interval: float) -> None:
        if min_interval <= 0:
            return
        domain_lock = await self._acquire_domain_lock(domain)
        try:
            now = time.monotonic()
            last = self._last_request.get(domain, 0.0)
            wait = min_interval - (now - last)
            if wait > 0:
                logger.debug(
                    "Domain rate limit: waiting %.1fs for %s (local)", wait, domain,
                )
                observe_rate_limit_wait("domain", wait)
                await asyncio.sleep(wait)
            self._last_request[domain] = time.monotonic()
        finally:
            domain_lock.release()
            async with self._state_condition:
                self._domain_users[domain] -= 1
                self._state_condition.notify_all()

    async def _acquire_domain_lock(self, domain: str) -> asyncio.Lock:
        while True:
            wait_for_lock: asyncio.Lock | None = None
            async with self._state_condition:
                self._prune_stale_domains(time.monotonic())
                lock = self._locks.get(domain)
                if lock is not None:
                    self._domain_users[domain] += 1
                    if lock.locked():
                        wait_for_lock = lock
                    else:
                        await lock.acquire()
                        return lock
                else:
                    if len(self._locks) >= self._max_domains:
                        idle = [
                            (self._last_request.get(name, float("inf")), name, candidate)
                            for name, candidate in self._locks.items()
                            if not candidate.locked()
                            and self._domain_users.get(name, 0) == 0
                        ]
                        if not idle:
                            observe_rate_limit_state("domain", "saturated")
                            await self._state_condition.wait()
                            continue
                        _last_request, evicted, _candidate = min(idle)
                        self._last_request.pop(evicted, None)
                        self._locks.pop(evicted, None)
                        self._domain_users.pop(evicted, None)
                        observe_rate_limit_state("domain", "evicted")
                    lock = asyncio.Lock()
                    self._locks[domain] = lock
                    self._domain_users[domain] = 1
                    await lock.acquire()
                    return lock
            assert wait_for_lock is not None
            try:
                await wait_for_lock.acquire()
                return wait_for_lock
            except BaseException:
                async with self._state_condition:
                    self._domain_users[domain] -= 1
                    self._state_condition.notify_all()
                raise

    def _prune_stale_domains(self, now: float) -> None:
        if now < self._last_prune_at + self._retention_seconds:
            return
        self._last_prune_at = now
        cutoff = now - self._retention_seconds
        for domain, last_request in list(self._last_request.items()):
            lock = self._locks.get(domain)
            if (
                last_request <= cutoff
                and (lock is None or not lock.locked())
                and self._domain_users.get(domain, 0) == 0
            ):
                self._last_request.pop(domain, None)
                self._locks.pop(domain, None)
                self._domain_users.pop(domain, None)
                observe_rate_limit_state("domain", "expired")


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
    # ARGV[2] = ttl          (int, seconds for key expiry)
    # Returns {1} on success, {0, remaining_ms} when caller must wait.
    _LUA_ACQUIRE = """\
local key = KEYS[1]
local min_interval = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local redis_time = redis.call('TIME')
local now = tonumber(redis_time[1]) + tonumber(redis_time[2]) / 1000000
local last = redis.call('GET', key)
if last == false then
    redis.call('SET', key, tostring(now), 'EX', ttl)
    return {1, 0}
end
local elapsed = now - tonumber(last)
if elapsed < 0 then
    redis.call('SET', key, tostring(now), 'EX', ttl)
    return {0, math.ceil(min_interval * 1000)}
end
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
        if min_interval <= 0:
            return
        key = f"scrapeyard:rate:{domain}"
        ttl = max(int(min_interval) + 1, 2)
        sha = await self._ensure_script()

        while True:
            try:
                eval_result = self._redis.evalsha(
                    sha, 1, key, str(min_interval), str(ttl),
                )
                result = await eval_result if inspect.isawaitable(eval_result) else eval_result
            except NoScriptError:
                self._script_sha = None
                sha = await self._ensure_script()
                continue
            acquired = int(result[0])
            if acquired:
                return
            remaining_ms = int(result[1])
            wait = min(min_interval, max(0.0, remaining_ms / 1000.0))
            logger.debug(
                "Domain rate limit: waiting %.1fs for %s (cross-job)",
                wait, domain,
            )
            observe_rate_limit_wait("domain", wait)
            await asyncio.sleep(wait)
