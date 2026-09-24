"""Cross-run per-domain page budgets and access-denial cooldowns.

The guard stops Scrapeyard itself before a request is made: a host that has
used its daily top-level page budget, or that recently denied access, is not
contacted again until the budget resets (UTC midnight) or the cooldown ends.
State is shared across workers and runs through Redis when available.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from arq.connections import ArqRedis
from redis.exceptions import NoScriptError

from scrapeyard.engine.scrape_models import ScrapeStop
from scrapeyard.engine.url_guard import canonical_url_origin, url_host_label
from scrapeyard.models.job import ErrorType

_DAY_KEY_TTL_SECONDS = 2 * 86400
_HOST_PATTERN = re.compile(r"^[a-z0-9.\-]+(?::\d{1,5})?$")


def domain_guard_host(url: str) -> str:
    """Return the host identity shared by run-level and cross-run denial checks."""
    origin = canonical_url_origin(url)
    host = origin[1] if origin else url_host_label(url)
    return host.removeprefix("www.")


def normalize_guard_host(value: str) -> str:
    """Normalize an operator-supplied host (no scheme/path) to a guard identity."""
    candidate = value.strip().lower()
    if not candidate or not _HOST_PATTERN.fullmatch(candidate):
        raise ValueError(f"Invalid host {value!r}")
    return domain_guard_host(f"https://{candidate}/")


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%d")


class DomainDailyLimitReached(ScrapeStop):
    """The host has used its daily top-level page budget."""

    error_type = ErrorType.domain_daily_limit
    pagination_stop_reason = "domain_guard"

    def __init__(self, host: str, limit: int) -> None:
        super().__init__(
            f"Stopped by Scrapeyard: daily page limit {limit} reached for {host}; "
            "no request was made"
        )
        self.host = host
        self.limit = limit


class DomainCooldownActive(ScrapeStop):
    """The host recently denied access and is cooling down."""

    error_type = ErrorType.domain_cooldown
    pagination_stop_reason = "domain_guard"

    def __init__(self, host: str, remaining_seconds: float) -> None:
        super().__init__(
            f"Stopped by Scrapeyard: {host} denied access recently; cooling down for "
            f"{int(remaining_seconds + 0.999)}s; no request was made"
        )
        self.host = host
        self.remaining_seconds = remaining_seconds


@dataclass(frozen=True)
class DomainGuardStatus:
    host: str
    day: str
    pages_today: int
    cooldown_remaining_seconds: float

    def as_dict(self, *, daily_page_limit: int, cooldown_seconds: int) -> dict[str, Any]:
        cooldown_until = None
        if self.cooldown_remaining_seconds > 0:
            cooldown_until = (
                datetime.now(UTC) + timedelta(seconds=self.cooldown_remaining_seconds)
            ).isoformat()
        return {
            "host": self.host,
            "day": self.day,
            "pages_today": self.pages_today,
            "daily_page_limit": daily_page_limit,
            "cooldown_seconds": cooldown_seconds,
            "cooldown_active": self.cooldown_remaining_seconds > 0,
            "cooldown_remaining_seconds": round(self.cooldown_remaining_seconds, 3),
            "cooldown_until": cooldown_until,
        }


class DomainGuard(Protocol):
    async def consume_page(self, host: str, limit: int) -> int:
        """Count one page attempt; raise DomainDailyLimitReached when ``limit`` is used."""
        ...

    async def cooldown_remaining(self, host: str) -> float: ...

    async def start_cooldown(self, host: str, seconds: int) -> None: ...

    async def status(self, host: str) -> DomainGuardStatus: ...

    async def clear(self, host: str) -> None: ...


class LocalDomainGuard:
    """In-process guard for tests and deployments without shared Redis state."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        day: Callable[[], str] = utc_day,
    ) -> None:
        self._clock = clock
        self._day = day
        self._pages: dict[tuple[str, str], int] = {}
        self._cooldowns: dict[str, float] = {}

    async def consume_page(self, host: str, limit: int) -> int:
        key = (host, self._day())
        count = self._pages.get(key, 0)
        if limit > 0 and count >= limit:
            raise DomainDailyLimitReached(host, limit)
        self._pages[key] = count + 1
        return count + 1

    async def cooldown_remaining(self, host: str) -> float:
        until = self._cooldowns.get(host)
        if until is None:
            return 0.0
        remaining = until - self._clock()
        if remaining <= 0:
            self._cooldowns.pop(host, None)
            return 0.0
        return remaining

    async def start_cooldown(self, host: str, seconds: int) -> None:
        if seconds > 0:
            self._cooldowns[host] = self._clock() + seconds

    async def status(self, host: str) -> DomainGuardStatus:
        day = self._day()
        return DomainGuardStatus(
            host=host,
            day=day,
            pages_today=self._pages.get((host, day), 0),
            cooldown_remaining_seconds=await self.cooldown_remaining(host),
        )

    async def clear(self, host: str) -> None:
        self._cooldowns.pop(host, None)
        for key in [key for key in self._pages if key[0] == host]:
            self._pages.pop(key, None)


class RedisDomainGuard:
    """Guard shared by every worker through atomic Redis operations."""

    # KEYS[1] = daily page counter, ARGV[1] = limit (0 = unlimited), ARGV[2] = ttl.
    # Returns {1, count} when admitted, {0, count} when the limit is exhausted.
    _LUA_CONSUME = """\
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if limit > 0 and current >= limit then
    return {0, current}
end
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return {1, count}
"""

    def __init__(self, redis: ArqRedis, *, day: Callable[[], str] = utc_day) -> None:
        self._redis = redis
        self._day = day
        self._script_sha: str | None = None

    @staticmethod
    def _pages_key(host: str, day: str) -> str:
        return f"scrapeyard:domain:pages:{host}:{day}"

    @staticmethod
    def _cooldown_key(host: str) -> str:
        return f"scrapeyard:domain:cooldown:{host}"

    async def _call(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    async def consume_page(self, host: str, limit: int) -> int:
        key = self._pages_key(host, self._day())
        while True:
            if self._script_sha is None:
                self._script_sha = await self._redis.script_load(self._LUA_CONSUME)
            try:
                result = await self._call(
                    self._redis.evalsha(
                        self._script_sha, 1, key, str(max(limit, 0)), str(_DAY_KEY_TTL_SECONDS),
                    )
                )
            except NoScriptError:
                self._script_sha = None
                continue
            if not int(result[0]):
                raise DomainDailyLimitReached(host, limit)
            return int(result[1])

    async def cooldown_remaining(self, host: str) -> float:
        remaining_ms = int(await self._call(self._redis.pttl(self._cooldown_key(host))))
        return remaining_ms / 1000.0 if remaining_ms > 0 else 0.0

    async def start_cooldown(self, host: str, seconds: int) -> None:
        if seconds > 0:
            await self._call(self._redis.set(self._cooldown_key(host), "1", ex=seconds))

    async def status(self, host: str) -> DomainGuardStatus:
        day = self._day()
        raw = await self._call(self._redis.get(self._pages_key(host, day)))
        return DomainGuardStatus(
            host=host,
            day=day,
            pages_today=int(raw) if raw is not None else 0,
            cooldown_remaining_seconds=await self.cooldown_remaining(host),
        )

    async def clear(self, host: str) -> None:
        await self._call(
            self._redis.delete(self._cooldown_key(host), self._pages_key(host, self._day()))
        )


@dataclass
class DomainGuardUsage:
    """Per-run record of guard outcomes, reported in ``run_budget``."""

    pages_admitted: dict[str, int] = field(default_factory=dict)
    daily_limit_stops: dict[str, int] = field(default_factory=dict)
    cooldown_stops: dict[str, int] = field(default_factory=dict)
    cooldowns_started: list[str] = field(default_factory=list)

    def snapshot(self, *, daily_page_limit: int, cooldown_seconds: int) -> dict[str, Any]:
        return {
            "daily_page_limit": daily_page_limit,
            "cooldown_seconds": cooldown_seconds,
            "pages_admitted": dict(self.pages_admitted),
            "daily_limit_stops": dict(self.daily_limit_stops),
            "cooldown_stops": dict(self.cooldown_stops),
            "cooldowns_started": list(self.cooldowns_started),
        }


@dataclass
class RunDomainPolicy:
    """Guard settings and outcomes for one run."""

    guard: DomainGuard
    daily_page_limit: int
    cooldown_seconds: int
    usage: DomainGuardUsage = field(default_factory=DomainGuardUsage)

    async def admit(self, url: str) -> None:
        host = domain_guard_host(url)
        if self.cooldown_seconds > 0:
            remaining = await self.guard.cooldown_remaining(host)
            if remaining > 0:
                self.usage.cooldown_stops[host] = self.usage.cooldown_stops.get(host, 0) + 1
                raise DomainCooldownActive(host, remaining)
        try:
            await self.guard.consume_page(host, self.daily_page_limit)
        except DomainDailyLimitReached:
            self.usage.daily_limit_stops[host] = self.usage.daily_limit_stops.get(host, 0) + 1
            raise
        self.usage.pages_admitted[host] = self.usage.pages_admitted.get(host, 0) + 1

    async def record_denial(self, url: str) -> None:
        if self.cooldown_seconds <= 0:
            return
        host = domain_guard_host(url)
        await self.guard.start_cooldown(host, self.cooldown_seconds)
        if host not in self.usage.cooldowns_started:
            self.usage.cooldowns_started.append(host)

    def snapshot(self) -> dict[str, Any]:
        return self.usage.snapshot(
            daily_page_limit=self.daily_page_limit,
            cooldown_seconds=self.cooldown_seconds,
        )


def effective_daily_page_limit(service_limit: int, job_limit: int | None) -> int:
    """Job limits only lower a nonzero service limit; alone they apply as given."""
    if job_limit is None:
        return service_limit
    if service_limit <= 0:
        return job_limit
    return min(service_limit, job_limit)


_RUN_DOMAIN_POLICY: ContextVar[RunDomainPolicy | None] = ContextVar(
    "scrapeyard_run_domain_policy",
    default=None,
)


@contextmanager
def activate_domain_policy(policy: RunDomainPolicy | None) -> Iterator[None]:
    token = _RUN_DOMAIN_POLICY.set(policy)
    try:
        yield
    finally:
        _RUN_DOMAIN_POLICY.reset(token)


async def admit_page(url: str) -> None:
    """Admit one top-level page attempt under the active run's domain policy."""
    policy = _RUN_DOMAIN_POLICY.get()
    if policy is not None:
        await policy.admit(url)
