from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.common.host_verdicts import PublicHostVerdicts
from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.engine.basic_fetch import BasicSession
from scrapeyard.engine.browser_debug import fetch_browser_response
from scrapeyard.engine.pagination import _pagination_url_is_safe
from scrapeyard.engine.scraper import _fetch_page
from scrapeyard.engine.url_guard import (
    UnsafeURLError,
    URLResolutionError,
    assert_public_url,
    public_dns_hostname,
    resolve_public_url,
)
from scrapeyard.engine.url_verdicts import assert_public_url_in_run, resolve_public_url_in_run
from tests.unit.browser_fakes import ScriptedBrowserSession

PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.5"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Resolver:
    """Scripted ``getaddrinfo``: each name answers an address or does not resolve."""

    def __init__(self, **answers: str | None) -> None:
        self.answers: dict[str, str | None] = {
            name.replace("_", "."): address for name, address in answers.items()
        }
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.gate: threading.Event | None = None
        self.during: Any = None

    def __call__(self, host: Any, *_args: Any, **_kwargs: Any) -> list[Any]:
        self.calls.append(str(host))
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.during is not None:
            self.during()
        address = self.answers.get(str(host))
        if address is None:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    def hold(self) -> threading.Event:
        self.gate = threading.Event()
        return self.gate


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Resolver:
    scripted = Resolver(
        shop_test=PUBLIC, cdn_test=PUBLIC, internal_test=PRIVATE, proxy_test=PUBLIC,
    )
    monkeypatch.setattr(socket, "getaddrinfo", scripted)
    return scripted


def _budget(clock: Any = None, *, duration: float = 60) -> RunBudget:
    return RunBudget(
        max_duration_seconds=duration,
        max_fetched_bytes=1_000_000,
        max_extracted_records=100,
        max_serialized_result_bytes=1_000_000,
        max_browser_debug_bytes=1_000_000,
        **({} if clock is None else {"clock": clock}),
    )


async def _check(url: str, budget: RunBudget | None, **options: Any) -> None:
    await assert_public_url_in_run(assert_public_url, url, run_budget=budget, **options)


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def _started(resolver: Resolver) -> None:
    """Wait until a lookup is on a run thread and every waiter has queued behind it."""
    assert await asyncio.to_thread(resolver.entered.wait, 10)
    await _settle()


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Shop.Test./path?q=1", "shop.test"),
        ("https://bücher.test/", "xn--bcher-kva.test"),
        ("http://shop.test:8080/", "shop.test"),
        ("https://8.8.8.8/", None),
        ("https://[2001:4860:4860::8888]/", None),
        ("http://134744072/", None),
    ],
)
def test_public_dns_hostname_names_what_dns_must_confirm(url: str, expected: str | None) -> None:
    assert public_dns_hostname(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://shop.test/",
        "http://127.0.0.1/",
        "http://localhost/",
        "http://2130706433/",
        "http://shop.test\\@127.0.0.1/",
        "http://shop.test /",
        "http://[::1]/",
    ],
)
def test_public_dns_hostname_rejects_before_any_lookup(url: str, resolver: Resolver) -> None:
    with pytest.raises(UnsafeURLError):
        public_dns_hostname(url)
    with pytest.raises(UnsafeURLError):
        assert_public_url(url, allow_unresolved=False)
    assert resolver.calls == []


def test_public_dns_hostname_applies_the_callers_schemes() -> None:
    assert public_dns_hostname("socks5://proxy.test:1080", allowed_schemes=("socks5",)) == "proxy.test"
    with pytest.raises(UnsafeURLError, match="scheme"):
        public_dns_hostname("socks5://proxy.test:1080")


def test_verdict_lifetime_counts_from_when_the_lookup_began() -> None:
    clock = Clock()
    verdicts = PublicHostVerdicts(ttl_seconds=30, clock=clock)
    began = clock.now
    clock.now += 12  # A slow lookup: its answer is already 12 s old.
    verdicts.record("shop.test", began)

    clock.now = began + 29.9
    assert verdicts.is_fresh("shop.test")
    clock.now = began + 30
    assert not verdicts.is_fresh("shop.test")
    assert not verdicts.is_fresh("never-seen.test")


def test_verdicts_are_bounded_and_drop_the_oldest() -> None:
    clock = Clock()
    verdicts = PublicHostVerdicts(max_hosts=3, clock=clock)
    for name in ("a.test", "b.test"):
        verdicts.record(name, clock.now)
    verdicts.record("a.test", clock.now)  # Refreshed: b.test is now the oldest.
    for name in ("c.test", "d.test"):
        verdicts.record(name, clock.now)

    fresh = {name for name in ("a.test", "b.test", "c.test", "d.test") if verdicts.is_fresh(name)}
    assert fresh == {"a.test", "c.test", "d.test"}


def test_a_run_remembers_at_most_1024_hostnames_for_30_seconds() -> None:
    clock = Clock()
    verdicts = PublicHostVerdicts(clock=clock)
    for index in range(1025):
        verdicts.record(f"host-{index}.test", clock.now)

    assert not verdicts.is_fresh("host-0.test")
    assert all(verdicts.is_fresh(f"host-{index}.test") for index in range(1, 1025))
    clock.now += 30
    assert not verdicts.is_fresh("host-1024.test")


def test_a_verdict_can_be_dropped() -> None:
    verdicts = PublicHostVerdicts()
    for name in ("a.test", "b.test", "c.test"):
        verdicts.record(name, verdicts.now())

    verdicts.forget("a.test")
    verdicts.forget("never-seen.test")
    assert [verdicts.is_fresh(name) for name in ("a.test", "b.test", "c.test")] == [False, True, True]
    verdicts.clear()
    assert not any(verdicts.is_fresh(name) for name in ("b.test", "c.test"))


async def test_a_public_host_is_resolved_once_while_its_verdict_is_fresh(resolver: Resolver) -> None:
    clock = Clock()
    budget = _budget(clock)

    for url in ("https://shop.test/", "https://SHOP.test./p/2?page=2", "http://shop.test:8080/x"):
        await _check(url, budget)
    assert resolver.calls == ["shop.test"]

    clock.now += 29
    await _check("https://shop.test/late", budget, allow_unresolved=False)
    assert resolver.calls == ["shop.test"]

    clock.now += 2
    await _check("https://shop.test/later", budget)
    assert resolver.calls == ["shop.test", "shop.test"]


async def test_a_slow_lookup_shortens_the_life_of_its_verdict(resolver: Resolver) -> None:
    clock = Clock()
    budget = _budget(clock, duration=600)
    began = clock.now

    def twelve_seconds() -> None:
        clock.now += 12

    resolver.during = twelve_seconds
    await _check("https://shop.test/", budget)
    await resolve_public_url_in_run(resolve_public_url, "https://cdn.test/", run_budget=budget)
    resolver.during = None
    assert clock.now == began + 24

    # shop.test was asked at +0 and cdn.test at +12; each lives 30 s from then.
    clock.now = began + 29.9
    await _check("https://shop.test/", budget)
    assert resolver.calls == ["shop.test", "cdn.test"]
    clock.now = began + 30
    await _check("https://shop.test/", budget)
    await _check("https://cdn.test/", budget)
    assert resolver.calls == ["shop.test", "cdn.test", "shop.test"]
    clock.now = began + 42
    await _check("https://cdn.test/", budget)
    assert resolver.calls == ["shop.test", "cdn.test", "shop.test", "cdn.test"]


async def test_a_fresh_verdict_is_still_a_deadline_checkpoint(resolver: Resolver) -> None:
    budget = _budget()
    await _check("https://shop.test/", budget)

    budget.deadline_monotonic = budget.started_monotonic
    with pytest.raises(BudgetExceeded):
        await _check("https://shop.test/", budget)
    assert resolver.calls == ["shop.test"]


async def test_a_fresh_verdict_never_skips_the_lexical_checks(resolver: Resolver) -> None:
    budget = _budget()
    await _check("https://shop.test/", budget)

    for url in (
        "ftp://shop.test/",
        "https://shop.test\\@127.0.0.1/",
        "https://shop.test/a b",
        "https://shop.test:99999/",
    ):
        with pytest.raises(UnsafeURLError):
            await _check(url, budget)
    # A proxy endpoint is held to its own schemes even on a known host.
    await _check("https://proxy.test:8443", budget, allowed_schemes=("http", "https"))
    with pytest.raises(UnsafeURLError, match="scheme"):
        await _check("socks5://proxy.test:1080", budget, allowed_schemes=("http", "https"))
    await _check("socks5://proxy.test:1080", budget, allowed_schemes=("socks5",))
    assert resolver.calls == ["shop.test", "proxy.test"]


async def test_a_non_public_host_is_resolved_and_refused_every_time(resolver: Resolver) -> None:
    budget = _budget()

    for _ in range(3):
        with pytest.raises(UnsafeURLError, match="non-public address 10.0.0.5"):
            await _check("https://internal.test/admin", budget)
    assert resolver.calls == ["internal.test"] * 3

    # Once it is public it is remembered; a refusal never was.
    resolver.answers["internal.test"] = PUBLIC
    await _check("https://internal.test/admin", budget)
    await _check("https://internal.test/admin", budget)
    assert resolver.calls == ["internal.test"] * 4


async def test_an_unresolved_host_is_never_remembered(resolver: Resolver) -> None:
    budget = _budget()

    # Direct fetches may release it, as before, but each request asks again...
    await _check("https://dead.test/pixel", budget)
    await _check("https://dead.test/pixel", budget)
    assert resolver.calls == ["dead.test"] * 2
    # ...so the request after it starts resolving to a private address is refused.
    resolver.answers["dead.test"] = PRIVATE
    with pytest.raises(UnsafeURLError, match="non-public"):
        await _check("https://dead.test/pixel", budget)

    resolver.answers["dead.test"] = None
    for _ in range(2):
        with pytest.raises(URLResolutionError, match="could not be resolved"):
            await _check("https://dead.test/pixel", budget, allow_unresolved=False)
    assert resolver.calls == ["dead.test"] * 5


async def test_a_host_that_turns_private_is_refused_once_its_verdict_ages_out(
    resolver: Resolver,
) -> None:
    clock = Clock()
    budget = _budget(clock)
    await _check("https://shop.test/", budget)
    resolver.answers["shop.test"] = PRIVATE

    clock.now += 30
    with pytest.raises(UnsafeURLError, match="non-public"):
        await _check("https://shop.test/", budget)


async def test_a_refusal_drops_the_verdict_the_run_still_held(resolver: Resolver) -> None:
    budget = _budget()
    await _check("https://shop.test/", budget)
    resolver.answers["shop.test"] = PRIVATE

    # The pinned basic fetch resolves for itself and sees the private address...
    with pytest.raises(UnsafeURLError, match="non-public"):
        await resolve_public_url_in_run(resolve_public_url, "https://shop.test/", run_budget=budget)
    # ...so the guard stops trusting what it saw a moment ago.
    with pytest.raises(UnsafeURLError, match="non-public"):
        await _check("https://shop.test/app.js", budget)

    resolver.answers["shop.test"] = PUBLIC
    await _check("https://shop.test/", budget)
    resolver.answers["shop.test"] = None
    with pytest.raises(URLResolutionError):
        await resolve_public_url_in_run(resolve_public_url, "https://shop.test/", run_budget=budget)
    assert not budget.public_hosts.is_fresh("shop.test")


async def test_a_refusal_outlasts_a_verdict_recorded_during_its_lookup(resolver: Resolver) -> None:
    budget = _budget()
    gate = resolver.hold()

    refused = asyncio.ensure_future(_check("https://internal.test/", budget))
    await _started(resolver)
    # Another path of the run saw the name as public while this lookup ran.
    budget.public_hosts.record("internal.test", budget.public_hosts.now())
    gate.set()

    with pytest.raises(UnsafeURLError, match="non-public"):
        await refused
    assert not budget.public_hosts.is_fresh("internal.test")


async def test_a_refusal_with_no_hostname_read_here_drops_every_verdict(resolver: Resolver) -> None:
    budget = _budget()
    for url in ("https://shop.test/", "https://cdn.test/"):
        await _check(url, budget)

    # A refused IP literal or scheme names no hostname: nothing to drop.
    for url in ("http://127.0.0.1/", "ftp://shop.test/"):
        with pytest.raises(UnsafeURLError):
            await _check(url, budget)
        with pytest.raises(UnsafeURLError):
            await resolve_public_url_in_run(resolve_public_url, url, run_budget=budget)
    assert budget.public_hosts.is_fresh("shop.test")

    # A URL too long to scan here might name any of them.
    with pytest.raises(UnsafeURLError, match="non-public"):
        await _check("https://internal.test/?d=" + "a" * 2048, budget)
    assert not budget.public_hosts.is_fresh("shop.test")
    assert not budget.public_hosts.is_fresh("cdn.test")

    await _check("https://shop.test/", budget)
    with pytest.raises(URLResolutionError):
        await resolve_public_url_in_run(
            resolve_public_url, "https://dead.test/\u00fc", run_budget=budget,
        )
    assert not budget.public_hosts.is_fresh("shop.test")


async def test_verdicts_belong_to_one_run(resolver: Resolver) -> None:
    first, second = _budget(), _budget()

    await _check("https://shop.test/", first)
    await _check("https://shop.test/", second)
    await _check("https://shop.test/", first)

    assert resolver.calls == ["shop.test"] * 2


@pytest.mark.parametrize("budget", [None, MagicMock(), SimpleNamespace()])
async def test_without_a_run_every_check_resolves(resolver: Resolver, budget: Any) -> None:
    if budget is not None:
        async def wait_for(awaitable: Any) -> Any:
            return await awaitable

        budget.wait_for = wait_for

    for _ in range(2):
        await _check("https://shop.test/", budget)
        with pytest.raises(UnsafeURLError):
            await _check("https://internal.test/", budget)

    assert resolver.calls == ["shop.test", "internal.test"] * 2


async def test_an_ip_literal_is_judged_without_a_lookup(resolver: Resolver) -> None:
    budget = _budget()

    await _check("https://8.8.8.8/dns", budget)
    with pytest.raises(UnsafeURLError, match="non-public IP"):
        await _check("http://169.254.169.254/latest/meta-data/", budget)

    assert resolver.calls == []
    assert not budget.public_hosts.is_fresh("8.8.8.8")


async def test_a_long_url_is_scanned_off_the_event_loop_and_resolved(resolver: Resolver) -> None:
    budget = _budget()
    long_url = "https://shop.test/collect?d=" + "a" * 2048

    await _check("https://shop.test/", budget)
    await _check(long_url, budget)
    await _check("https://shop.test/collect?d=" + "a" * 2020, budget)
    # The pinned resolution of a long URL is likewise not remembered.
    await resolve_public_url_in_run(resolve_public_url, long_url.replace("shop", "cdn"), run_budget=budget)

    assert resolver.calls == ["shop.test", "shop.test", "cdn.test"]
    assert not budget.public_hosts.is_fresh("cdn.test")


async def test_a_non_ascii_url_is_scanned_off_the_event_loop_and_resolved(
    resolver: Resolver,
) -> None:
    budget = _budget()
    resolver.answers["xn--bcher-kva.test"] = PUBLIC

    # Python's IDNA encoder is quadratic in a label and runs before the length
    # check, so a page could otherwise stall the loop with one long hostname.
    for _ in range(2):
        await _check("https://b\u00fccher.test/", budget)
        await _check("https://shop.test/caf\u00e9", budget)
    assert resolver.calls == ["xn--bcher-kva.test", "shop.test"] * 2

    # The spelling a browser uses for the same name does get a verdict.
    for _ in range(2):
        await _check("https://xn--bcher-kva.test/", budget)
    assert resolver.calls.count("xn--bcher-kva.test") == 3
    with pytest.raises(UnsafeURLError, match="invalid"):
        await _check("https://" + "\u4e00" * 70 + ".test/", budget)


async def test_the_callers_check_decides_everything_but_a_fresh_verdict() -> None:
    budget = _budget()
    seen: list[tuple[str, dict[str, Any]]] = []

    def check(url: str, **options: Any) -> None:
        seen.append((url, options))

    # A caller's replacement is asked about a URL the lexical checks refuse...
    await assert_public_url_in_run(check, "http://127.0.0.1:8080/", run_budget=budget)
    # ...and once per hostname otherwise, always with resolution required.
    await assert_public_url_in_run(check, "https://shop.test/a", run_budget=budget)
    await assert_public_url_in_run(check, "https://shop.test/b", run_budget=budget)

    assert seen == [
        ("http://127.0.0.1:8080/", {"allow_unresolved": True}),
        ("https://shop.test/a", {"allow_unresolved": False}),
    ]


async def test_requests_arriving_during_a_lookup_share_its_public_answer(
    resolver: Resolver,
) -> None:
    budget = _budget()
    gate = resolver.hold()

    checks = [
        asyncio.ensure_future(_check(f"https://cdn.test/asset-{index}.js", budget))
        for index in range(25)
    ]
    await _started(resolver)
    assert not any(check.done() for check in checks)
    gate.set()
    await asyncio.gather(*checks)

    assert resolver.calls == ["cdn.test"]
    assert budget.public_hosts.lookups == {}


async def test_requests_arriving_during_a_lookup_share_its_refusal(resolver: Resolver) -> None:
    budget = _budget()
    gate = resolver.hold()

    checks = [
        asyncio.ensure_future(_check(f"https://internal.test/{index}", budget))
        for index in range(5)
    ]
    await _started(resolver)
    gate.set()
    outcomes = await asyncio.gather(*checks, return_exceptions=True)

    assert resolver.calls == ["internal.test"]
    assert [str(outcome) for outcome in outcomes] == [
        "Hostname 'internal.test' resolves to non-public address 10.0.0.5"
    ] * 5
    assert all(isinstance(outcome, UnsafeURLError) for outcome in outcomes)
    # One instance each: a fetch attaches its own diagnostics to what it raises.
    assert len({id(outcome) for outcome in outcomes}) == 5
    # Nothing was remembered: the next request resolves again.
    with pytest.raises(UnsafeURLError):
        await _check("https://internal.test/again", budget)
    assert resolver.calls == ["internal.test"] * 2


async def test_an_unresolved_answer_is_shared_by_each_requests_own_rule(
    resolver: Resolver,
) -> None:
    budget = _budget()
    gate = resolver.hold()

    lenient = [asyncio.ensure_future(_check("https://dead.test/a", budget)) for _ in range(2)]
    strict = [
        asyncio.ensure_future(_check("https://dead.test/b", budget, allow_unresolved=False))
        for _ in range(2)
    ]
    await _started(resolver)
    gate.set()
    outcomes = await asyncio.gather(*lenient, *strict, return_exceptions=True)

    assert resolver.calls == ["dead.test"]
    assert outcomes[:2] == [None, None]
    assert all(isinstance(outcome, URLResolutionError) for outcome in outcomes[2:])
    assert outcomes[2] is not outcomes[3]


async def test_a_strict_request_leading_an_unresolved_lookup_still_fails(
    resolver: Resolver,
) -> None:
    budget = _budget()
    gate = resolver.hold()

    strict = asyncio.ensure_future(_check("https://dead.test/b", budget, allow_unresolved=False))
    await _started(resolver)
    lenient = asyncio.ensure_future(_check("https://dead.test/a", budget))
    await _settle()
    gate.set()

    with pytest.raises(URLResolutionError, match="could not be resolved"):
        await strict
    await lenient
    assert resolver.calls == ["dead.test"]


async def test_waiting_requests_resolve_for_themselves_when_the_first_is_cancelled(
    resolver: Resolver,
) -> None:
    budget = _budget()
    gate = resolver.hold()

    first = asyncio.ensure_future(_check("https://cdn.test/a.js", budget))
    await _started(resolver)
    waiting = [
        asyncio.ensure_future(_check(f"https://cdn.test/b-{index}.js", budget)) for index in range(3)
    ]
    await _settle()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await _settle()
    # One of them leads a new lookup; the others, having waited once, do not
    # queue behind it but ask for themselves, so nothing resolves one by one.
    assert len(budget.public_hosts.lookups) == 1
    gate.set()
    await asyncio.wait_for(asyncio.gather(*waiting), 10)

    assert resolver.calls == ["cdn.test"] * 4
    assert budget.public_hosts.is_fresh("cdn.test")
    assert budget.public_hosts.lookups == {}


async def test_a_cancelled_waiter_leaves_the_lookup_to_the_others(resolver: Resolver) -> None:
    budget = _budget()
    gate = resolver.hold()

    first = asyncio.ensure_future(_check("https://cdn.test/a.js", budget))
    await _started(resolver)
    waiting = [
        asyncio.ensure_future(_check(f"https://cdn.test/b-{index}.js", budget)) for index in range(2)
    ]
    await _settle()

    waiting[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting[0]
    assert not first.done()
    gate.set()
    await asyncio.wait_for(asyncio.gather(first, waiting[1]), 10)

    assert resolver.calls == ["cdn.test"]
    assert budget.public_hosts.lookups == {}


async def test_an_unexpected_lookup_failure_is_not_shared(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch,
) -> None:
    budget = _budget()
    gate = resolver.hold()

    def broken(*_args: Any, **_kwargs: Any) -> list[Any]:
        resolver.calls.append("broken")
        resolver.entered.set()
        assert gate.wait(10)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(socket, "getaddrinfo", broken)
    checks = [asyncio.ensure_future(_check("https://cdn.test/", budget)) for _ in range(3)]
    await _started(resolver)
    gate.set()
    outcomes = await asyncio.wait_for(asyncio.gather(*checks, return_exceptions=True), 10)

    # Each request reports its own lookup's failure, as each did before.
    assert [type(outcome) for outcome in outcomes] == [OSError] * 3
    assert resolver.calls == ["broken"] * 3
    assert budget.public_hosts.lookups == {}


async def test_waiting_requests_stop_at_the_run_deadline(resolver: Resolver) -> None:
    budget = _budget(duration=1)
    gate = resolver.hold()

    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(
                *(_check(f"https://cdn.test/{index}.js", budget) for index in range(3)),
                return_exceptions=True,
            ),
            10,
        )
    finally:
        gate.set()

    assert all(isinstance(outcome, BudgetExceeded) for outcome in outcomes)
    # The lookup had begun, and nothing was started after the deadline.
    assert resolver.calls == ["cdn.test"]
    assert not budget.public_hosts.is_fresh("cdn.test")
    assert budget.public_hosts.lookups == {}


class _Route:
    def __init__(self, url: str, *, navigation: bool = False) -> None:
        self.request = SimpleNamespace(
            url=url,
            resource_type="document" if navigation else "script",
            headers={},
            frame=SimpleNamespace(parent_frame=None if navigation else object()),
            is_navigation_request=lambda: navigation,
        )
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self, **_kwargs: Any) -> None:
        self.continued = True


def _browser_target() -> TargetConfig:
    return TargetConfig(
        url="https://shop.test/list",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"disable_resources": False},
    )


@pytest.mark.parametrize("require_resolved_dns", [False, True])
async def test_the_route_guard_resolves_each_host_of_a_page_once(
    resolver: Resolver, require_resolved_dns: bool,
) -> None:
    target = _browser_target()
    document = _Route(target.url, navigation=True)
    assets = [_Route(f"https://{host}/app-{index}.js") for index in range(4) for host in ("shop.test", "cdn.test")]
    internal = [_Route(f"https://internal.test/probe-{index}") for index in range(3)]
    dead = [_Route(f"https://dead.test/pixel-{index}.gif") for index in range(2)]
    literal = _Route("http://169.254.169.254/latest/meta-data/")

    async def route_fetch(url: str, _kwargs: dict[str, Any], route_handler: Any) -> Any:
        await route_handler(document)
        for route in (*assets, *internal, *dead, literal):
            try:
                await route_handler(route)
            except UnsafeURLError:
                pass
        return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    _response, capture = await fetch_browser_response(
        ScriptedBrowserSession(route_fetch), target.url, target, FetcherType.dynamic, {}, None,
        require_resolved_dns=require_resolved_dns, budget=_budget(),
    )

    assert (document.aborted, document.continued) == (False, True)
    assert all((route.aborted, route.continued) == (False, True) for route in assets)
    assert all((route.aborted, route.continued) == (True, False) for route in (*internal, literal))
    # An unresolvable subrequest is released on a direct fetch and dropped behind a proxy.
    assert all(route.continued is not require_resolved_dns for route in dead)
    assert all(route.aborted is require_resolved_dns for route in dead)
    assert resolver.calls == ["shop.test", "cdn.test", *["internal.test"] * 3, *["dead.test"] * 2]
    blocked = [(entry["kind"], entry["url"]) for entry in capture["blocked_requests"]]
    assert blocked == [
        *[("request", f"https://internal.test/probe-{index}") for index in range(3)],
        *[
            ("unresolved_request", f"https://dead.test/pixel-{index}.gif")
            for index in range(2)
            if require_resolved_dns
        ],
        ("request", "http://169.254.169.254/latest/meta-data/"),
    ]


async def test_a_browser_page_and_its_pagination_resolve_the_target_once(
    resolver: Resolver,
) -> None:
    target = _browser_target()
    budget = _budget()

    async def route_fetch(url: str, _kwargs: dict[str, Any], route_handler: Any) -> Any:
        await route_handler(_Route(url, navigation=True))
        await route_handler(_Route("https://shop.test/app.js"))
        return SimpleNamespace(status=200, url=url, text="<html><h1>ok</h1></html>")

    session = ScriptedBrowserSession(route_fetch)
    for page in (1, 2):
        url = f"{target.url}?page={page}"
        await _fetch_page(
            session, url, target, FetcherType.dynamic, adaptive=False, retryable_status={500},
            adaptive_dir="/tmp/adaptive", proxy_url="https://proxy.test:8443", budget=budget,
        )
        assert await _pagination_url_is_safe(
            f"{target.url}?page={page + 1}", budget=budget, cancellation_guard=None,
        )

    # Without the run's verdicts this was five lookups of shop.test and one of
    # the proxy for every page.
    assert resolver.calls == ["proxy.test", "shop.test"]


async def test_a_direct_basic_fetch_resolves_once_per_request_and_still_pins(
    resolver: Resolver, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = TargetConfig(url="https://shop.test/list", fetcher=FetcherType.basic, selectors={"title": "h1"})
    budget = _budget()
    connected: list[tuple[str, Any]] = []

    async def fetch_basic_response(_fetcher: Any, url: str, kwargs: dict[str, Any], **_options: Any) -> Any:
        connected.append((url, kwargs["headers"]["Host"]))
        return SimpleNamespace(status=200, url=url, headers={}, text="<h1>ok</h1>")

    monkeypatch.setattr(
        "scrapeyard.engine.scraper.get_settings",
        lambda: SimpleNamespace(basic_fetch_timeout_seconds=12.5),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", fetch_basic_response)

    for page in (1, 2):
        outcome = await _fetch_page(
            BasicSession(), f"{target.url}?page={page}", target, FetcherType.basic,
            adaptive=False, retryable_status={500}, adaptive_dir="/tmp/adaptive", budget=budget,
        )
        assert outcome.debug["final_url"] == f"{target.url}?page={page}"
        assert await _pagination_url_is_safe(
            f"{target.url}?page={page + 1}", budget=budget, cancellation_guard=None,
        )

    # Every connection is pinned to an address resolved for it; the final-URL
    # and pagination checks no longer resolve the same name again.
    assert connected == [
        (f"https://{PUBLIC}/list?page={page}", "shop.test") for page in (1, 2)
    ]
    assert resolver.calls == ["shop.test", "shop.test"]


async def test_a_pinned_resolution_is_remembered_only_when_it_succeeds(resolver: Resolver) -> None:
    budget = _budget()

    with pytest.raises(UnsafeURLError, match="non-public"):
        await resolve_public_url_in_run(resolve_public_url, "https://internal.test/", run_budget=budget)
    with pytest.raises(URLResolutionError):
        await resolve_public_url_in_run(resolve_public_url, "https://dead.test/", run_budget=budget)
    pinned = await resolve_public_url_in_run(resolve_public_url, "https://8.8.8.8/", run_budget=budget)
    assert pinned.connect_url == "https://8.8.8.8/"
    assert not any(budget.public_hosts.is_fresh(name) for name in ("internal.test", "dead.test", "8.8.8.8"))

    resolved = await resolve_public_url_in_run(resolve_public_url, "https://shop.test/x", run_budget=budget)
    unbudgeted = await resolve_public_url_in_run(resolve_public_url, "https://shop.test/x", run_budget=None)
    assert resolved == unbudgeted
    assert resolved.connect_url == f"https://{PUBLIC}/x"
    assert budget.public_hosts.is_fresh("shop.test")
