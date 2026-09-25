from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from scrapling.engines import pw as scrapling_pw_engine

from scrapeyard.config.schema import BrowserConfig, FetcherType, TargetConfig
from scrapeyard.engine import browser_debug
from scrapeyard.engine.browser_debug import fetch_browser_response
from scrapeyard.engine.subrequest_filter import SubrequestFilter, glob_matches

TARGET = "https://www.example.test/catalog"


@pytest.mark.parametrize(
    ("pattern", "url", "expected"),
    [
        ("*/collect?*", "https://m.example.test/collect?v=1", True),
        ("*/collect?*", "https://m.example.test/collector?v=1", False),
        ("https://*.example-ads.test/*", "https://tags.example-ads.test/t.js", True),
        ("https://*.example-ads.test/*", "https://example-ads.test.evil.test/t.js", False),
        ("*.js", "https://www.example.test/app.js?v=2", False),
        ("*.js*", "https://www.example.test/app.js?v=2", True),
        ("*a*a*b", "https://x.test/" + "a" * 5000, False),
        ("https://www.example.test/", "https://www.example.test/", True),
        ("*", "https://anything.test/x", True),
    ],
)
def test_glob_treats_only_star_as_a_wildcard(pattern: str, url: str, expected: bool) -> None:
    assert glob_matches(tuple(pattern.split("*")), url) is expected


def _filter(**browser: Any) -> SubrequestFilter:
    subrequest_filter = SubrequestFilter.for_target(BrowserConfig(**browser), TARGET)
    assert subrequest_filter is not None
    return subrequest_filter


def test_no_filter_without_blocking_options() -> None:
    assert SubrequestFilter.for_target(BrowserConfig(), TARGET) is None


def test_third_party_blocking_keeps_the_registrable_domain_and_allowed_hosts() -> None:
    subrequest_filter = _filter(
        block_third_party=True, third_party_allow_hosts=["example-cdn.test"],
    )

    assert not subrequest_filter.blocks("https://img.example.test/a.png")
    assert not subrequest_filter.blocks("https://example.test/api/items")
    assert not subrequest_filter.blocks("https://static.example-cdn.test/grid.js")
    assert not subrequest_filter.blocks("https://example-cdn.test/grid.js")
    assert subrequest_filter.blocks("https://tags.example-ads.test/t.js")
    assert subrequest_filter.blocks("https://example-cdn.test.example-ads.test/t.js")
    assert not subrequest_filter.blocks("data:text/plain,hello")


def test_url_patterns_apply_to_first_party_and_allowed_hosts() -> None:
    subrequest_filter = _filter(
        block_third_party=True,
        third_party_allow_hosts=["example-cdn.test"],
        block_url_patterns=["*/beacon?*", "https://static.example-cdn.test/track/*"],
    )

    assert subrequest_filter.blocks("https://www.example.test/api/beacon?e=view")
    assert subrequest_filter.blocks("https://static.example-cdn.test/track/p.js")
    assert not subrequest_filter.blocks("https://www.example.test/api/items?page=2")


def test_allow_hosts_are_normalized_and_validated() -> None:
    config = BrowserConfig(
        block_third_party=True,
        third_party_allow_hosts=["*.Static.Example-CDN.test", "static.example-cdn.test.", "bücher.test"],
    )

    assert config.third_party_allow_hosts == ["static.example-cdn.test", "xn--bcher-kva.test"]


@pytest.mark.parametrize(
    ("browser", "message"),
    [
        ({"third_party_allow_hosts": ["cdn.example.test"]}, "requires block_third_party"),
        ({"block_third_party": True, "third_party_allow_hosts": ["https://cdn.example.test/"]}, "host names"),
        ({"block_third_party": True, "third_party_allow_hosts": ["cdn.example.test:443"]}, "host names"),
        ({"block_url_patterns": ["*/collect *"]}, "whitespace"),
        ({"block_url_patterns": ["  "]}, "blank"),
        ({"block_url_patterns": ["*/a*", "*/a*"]}, "unique"),
    ],
)
def test_invalid_blocking_options_are_rejected(browser: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        BrowserConfig(**browser)


class _Route:
    def __init__(self, url: str, *, navigation: bool = False, main_frame: bool = False) -> None:
        frame = SimpleNamespace(parent_frame=None if main_frame else object())
        self.request = SimpleNamespace(
            url=url,
            resource_type="document" if navigation else "script",
            headers={},
            frame=frame,
            is_navigation_request=lambda: navigation,
        )
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self, **_kwargs: Any) -> None:
        self.continued = True


async def _route_through_guard(route: _Route, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    checked: list[str] = []

    def assert_public_url(url: str, **_kwargs: Any) -> None:
        checked.append(url)

    monkeypatch.setattr(browser_debug, "assert_public_url", assert_public_url)
    target = TargetConfig(
        url=TARGET,
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"block_third_party": True, "block_url_patterns": ["*/beacon*"]},
    )

    class RouteFetcher:
        @staticmethod
        async def async_fetch(url: str, **_kwargs: Any) -> Any:
            await scrapling_pw_engine.async_intercept_route(route)
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    await fetch_browser_response(RouteFetcher, TARGET, target, FetcherType.dynamic, {}, None)
    return checked


async def test_guard_aborts_blocked_subrequests_before_resolving_them(monkeypatch) -> None:
    for url in ("https://tags.example-ads.test/t.js", "https://www.example.test/beacon?e=1"):
        route = _Route(url)
        checked = await _route_through_guard(route, monkeypatch)
        assert (route.aborted, route.continued, checked) == (True, False, [])


@pytest.mark.parametrize(("navigation", "main_frame", "blocked"), [
    (True, True, False),
    (True, False, True),
])
async def test_guard_never_blocks_top_level_navigations(
    monkeypatch, navigation: bool, main_frame: bool, blocked: bool,
) -> None:
    route = _Route("https://checkout.example-pay.test/", navigation=navigation, main_frame=main_frame)

    await _route_through_guard(route, monkeypatch)

    assert (route.aborted, route.continued) == (blocked, not blocked)


async def test_guard_continues_first_party_subrequests(monkeypatch) -> None:
    route = _Route("https://www.example.test/app.js")

    checked = await _route_through_guard(route, monkeypatch)

    assert (route.aborted, route.continued) == (False, True)
    assert checked == ["https://www.example.test/app.js"]
