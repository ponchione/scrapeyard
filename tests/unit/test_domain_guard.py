"""Per-domain daily page budgets and access-denial cooldowns."""

from __future__ import annotations

import time
from typing import Any

import pytest
from redis.exceptions import NoScriptError

from scrapeyard.config.schema import FetcherType
from scrapeyard.engine.domain_guard import (
    DomainCooldownActive,
    DomainDailyLimitReached,
    LocalDomainGuard,
    RedisDomainGuard,
    RunDomainPolicy,
    activate_domain_policy,
    admit_page,
    domain_guard_host,
    effective_daily_page_limit,
    normalize_guard_host,
)
from scrapeyard.engine.fetch_classifier import classify_fetch_exception
from scrapeyard.models.job import ErrorType


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.day = "20260101"

    def time(self) -> float:
        return self.now

    def today(self) -> str:
        return self.day


def _local_guard() -> tuple[LocalDomainGuard, _Clock]:
    clock = _Clock()
    return LocalDomainGuard(clock=clock.time, day=clock.today), clock


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://www.Example.test/a", "example.test"),
        ("https://shop.example.test:8443/a", "shop.example.test"),
        ("https://example.test:443/", "example.test"),
    ],
)
def test_guard_host_matches_run_level_denial_identity(url, host):
    assert domain_guard_host(url) == host


@pytest.mark.parametrize("value", ["", "https://example.test", "example.test/path", "a b"])
def test_operator_host_must_be_a_bare_host(value):
    with pytest.raises(ValueError):
        normalize_guard_host(value)


def test_operator_host_is_normalized_like_target_urls():
    assert normalize_guard_host("WWW.Example.test") == "example.test"


@pytest.mark.parametrize(
    "service,job,expected",
    [(0, None, 0), (0, 25, 25), (100, None, 100), (100, 25, 25), (10, 25, 10)],
)
def test_job_limit_only_lowers_a_nonzero_service_limit(service, job, expected):
    assert effective_daily_page_limit(service, job) == expected


@pytest.mark.asyncio
async def test_local_daily_limit_refuses_without_counting_and_resets_next_day():
    guard, clock = _local_guard()
    assert [await guard.consume_page("example.test", 2) for _ in range(2)] == [1, 2]
    with pytest.raises(DomainDailyLimitReached):
        await guard.consume_page("example.test", 2)
    assert (await guard.status("example.test")).pages_today == 2
    assert await guard.consume_page("other.test", 2) == 1

    clock.day = "20260102"
    assert await guard.consume_page("example.test", 2) == 1


@pytest.mark.asyncio
async def test_local_unlimited_budget_still_counts_pages():
    guard, _clock = _local_guard()
    for _ in range(5):
        await guard.consume_page("example.test", 0)
    assert (await guard.status("example.test")).pages_today == 5


@pytest.mark.asyncio
async def test_local_cooldown_expires_and_clear_resets_state():
    guard, clock = _local_guard()
    await guard.start_cooldown("example.test", 60)
    assert await guard.cooldown_remaining("example.test") == pytest.approx(60)
    clock.now += 59
    assert await guard.cooldown_remaining("example.test") == pytest.approx(1)
    clock.now += 2
    assert await guard.cooldown_remaining("example.test") == 0

    await guard.start_cooldown("example.test", 60)
    await guard.consume_page("example.test", 0)
    await guard.clear("example.test")
    status = await guard.status("example.test")
    assert (status.pages_today, status.cooldown_remaining_seconds) == (0, 0)


class _FakeRedis:
    """Implements the guard's Redis commands, including its consume script."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiry: dict[str, float] = {}
        self.script_loads = 0
        self.flush_script_once = False

    def _alive(self, key: str) -> bool:
        if key in self.expiry and self.expiry[key] <= time.monotonic():
            self.values.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.values

    async def script_load(self, script: str) -> str:
        assert "INCR" in script
        self.script_loads += 1
        return "sha"

    async def evalsha(self, sha: str, numkeys: int, key: str, limit: str, ttl: str) -> list[int]:
        assert (sha, numkeys) == ("sha", 1)
        if self.flush_script_once:
            self.flush_script_once = False
            raise NoScriptError("NOSCRIPT")
        current = int(self.values[key]) if self._alive(key) else 0
        if int(limit) > 0 and current >= int(limit):
            return [0, current]
        self.values[key] = str(current + 1)
        if current == 0:
            self.expiry[key] = time.monotonic() + int(ttl)
        return [1, current + 1]

    async def pttl(self, key: str) -> int:
        if not self._alive(key):
            return -2
        if key not in self.expiry:
            return -1
        return int((self.expiry[key] - time.monotonic()) * 1000)

    async def set(self, key: str, value: str, ex: int) -> bool:
        self.values[key] = value
        self.expiry[key] = time.monotonic() + ex
        return True

    async def get(self, key: str) -> Any:
        return self.values[key] if self._alive(key) else None

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(self.values.pop(key, None) is not None)
            self.expiry.pop(key, None)
        return removed


@pytest.mark.asyncio
async def test_redis_guard_shares_counts_and_cooldowns_by_host_and_day():
    redis = _FakeRedis()
    guard = RedisDomainGuard(redis, day=lambda: "20260101")  # type: ignore[arg-type]
    assert await guard.consume_page("example.test", 2) == 1
    assert await guard.consume_page("example.test", 2) == 2
    with pytest.raises(DomainDailyLimitReached):
        await guard.consume_page("example.test", 2)
    assert "scrapeyard:domain:pages:example.test:20260101" in redis.values
    assert redis.expiry["scrapeyard:domain:pages:example.test:20260101"] > time.monotonic()

    await guard.start_cooldown("example.test", 120)
    remaining = await guard.cooldown_remaining("example.test")
    assert 119 < remaining <= 120
    status = await guard.status("example.test")
    assert status.pages_today == 2
    assert status.as_dict(daily_page_limit=2, cooldown_seconds=120)["cooldown_active"] is True

    await guard.clear("example.test")
    status = await guard.status("example.test")
    assert (status.pages_today, status.cooldown_remaining_seconds) == (0, 0)


@pytest.mark.asyncio
async def test_redis_guard_reloads_a_flushed_script():
    redis = _FakeRedis()
    guard = RedisDomainGuard(redis, day=lambda: "20260101")  # type: ignore[arg-type]
    await guard.consume_page("example.test", 0)
    redis.flush_script_once = True
    assert await guard.consume_page("example.test", 0) == 2
    assert redis.script_loads == 2


@pytest.mark.asyncio
async def test_run_policy_refuses_cooling_hosts_and_reports_usage():
    guard, _clock = _local_guard()
    policy = RunDomainPolicy(guard=guard, daily_page_limit=1, cooldown_seconds=600)

    await policy.admit("https://www.example.test/a")
    with pytest.raises(DomainDailyLimitReached):
        await policy.admit("https://example.test/b")

    await policy.record_denial("https://blocked.test/")
    with pytest.raises(DomainCooldownActive) as denied:
        await policy.admit("https://blocked.test/next")
    assert "no request was made" in str(denied.value)

    assert policy.snapshot() == {
        "daily_page_limit": 1,
        "cooldown_seconds": 600,
        "pages_admitted": {"example.test": 1},
        "daily_limit_stops": {"example.test": 1},
        "cooldown_stops": {"blocked.test": 1},
        "cooldowns_started": ["blocked.test"],
    }


@pytest.mark.asyncio
async def test_disabled_cooldown_neither_records_nor_checks_denials():
    guard, _clock = _local_guard()
    await guard.start_cooldown("example.test", 600)
    policy = RunDomainPolicy(guard=guard, daily_page_limit=0, cooldown_seconds=0)
    await policy.record_denial("https://other.test/")
    await policy.admit("https://example.test/")
    assert await guard.cooldown_remaining("other.test") == 0


@pytest.mark.asyncio
async def test_admit_page_applies_only_the_active_policy():
    guard, _clock = _local_guard()
    policy = RunDomainPolicy(guard=guard, daily_page_limit=1, cooldown_seconds=0)
    await admit_page("https://example.test/")
    with activate_domain_policy(policy):
        await admit_page("https://example.test/")
        with pytest.raises(DomainDailyLimitReached):
            await admit_page("https://example.test/")
    await admit_page("https://example.test/")
    assert policy.usage.pages_admitted == {"example.test": 1}


@pytest.mark.parametrize(
    "stop,error_type",
    [
        (DomainDailyLimitReached("example.test", 5), ErrorType.domain_daily_limit),
        (DomainCooldownActive("example.test", 30), ErrorType.domain_cooldown),
    ],
)
def test_guard_stops_classify_as_self_imposed_without_status(stop, error_type):
    assert classify_fetch_exception(stop, FetcherType.basic) == (error_type, None, None)
