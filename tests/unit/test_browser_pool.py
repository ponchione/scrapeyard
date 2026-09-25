from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from scrapling import PlayWrightFetcher

from scrapeyard.config.schema import ExecutionConfig, TargetConfig
from scrapeyard.engine import browser_pool
from scrapeyard.engine.browser_pool import (
    BrowserPool,
    activate_browser_pool,
    browser_group_key,
    current_browser_pool,
)
from scrapeyard.engine.browser_session import BrowserSession


def _target(url: str = "https://www.example.test/c/1", **browser: Any) -> TargetConfig:
    return TargetConfig.model_validate({
        "url": url,
        "fetcher": "dynamic",
        "browser": browser or None,
        "selectors": {"name": ".name"},
    })


@pytest.fixture()
def closed(monkeypatch: pytest.MonkeyPatch) -> list[BrowserSession]:
    closed: list[BrowserSession] = []

    async def aclose(self: BrowserSession) -> None:
        closed.append(self)
        self.routed_requests = 0

    monkeypatch.setattr(BrowserSession, "aclose", aclose)
    return closed


def test_reuse_is_off_by_default() -> None:
    assert ExecutionConfig().reuse_browser is False


def test_group_key_needs_the_same_site_fetcher_proxy_and_browser_settings() -> None:
    key = browser_group_key(_target(), None)

    assert browser_group_key(_target("https://img.example.test/c/2"), None) == key
    assert browser_group_key(_target("https://www.other-shop.test/c/1"), None) != key
    assert browser_group_key(_target(), "http://proxy.example.test:8080") != key
    assert browser_group_key(_target(stealth=True), None) != key
    # An explicit default browser block is the same settings as none at all.
    assert browser_group_key(_target(timeout_ms=60000), None) == key


async def test_targets_of_a_group_take_turns_on_one_session(closed) -> None:
    pool = BrowserPool(1)

    first = await pool.acquire(_target(), None, PlayWrightFetcher)
    await pool.release(first)
    second = await pool.acquire(_target("https://www.example.test/c/2"), None, PlayWrightFetcher)
    await pool.release(second)

    assert second is first and pool.sessions == 1 and closed == []
    await pool.aclose()
    assert closed == [first]


async def test_concurrent_targets_get_their_own_session_and_the_run_never_exceeds_capacity(
    closed,
) -> None:
    pool = BrowserPool(2)

    first, second = await asyncio.gather(
        pool.acquire(_target(), None, PlayWrightFetcher),
        pool.acquire(_target("https://www.example.test/c/2"), None, PlayWrightFetcher),
    )
    assert first is not second
    await pool.release(first)
    third = await pool.acquire(_target("https://www.example.test/c/3"), None, PlayWrightFetcher)
    assert third is first

    await pool.release(second)
    await pool.release(third)
    # Another site's target closes an idle session before opening its own.
    other = await pool.acquire(_target("https://www.other-shop.test/"), None, PlayWrightFetcher)

    assert pool.sessions == 2 and closed == [second]
    assert other not in (first, second)


async def test_a_session_is_replaced_after_many_routed_requests(closed) -> None:
    pool = BrowserPool(1)
    session = await pool.acquire(_target(), None, PlayWrightFetcher)
    session.routed_requests = browser_pool.RECYCLE_AFTER_REQUESTS

    await pool.release(session)
    fresh = await pool.acquire(_target(), None, PlayWrightFetcher)

    assert closed == [session] and fresh is not session and pool.sessions == 1


async def test_a_disconnected_idle_session_relaunches_instead_of_failing(closed) -> None:
    pool = BrowserPool(1)
    session = await pool.acquire(_target(), None, PlayWrightFetcher)
    session._context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False))
    await pool.release(session)

    assert await pool.acquire(_target(), None, PlayWrightFetcher) is session
    assert closed == [session]


async def test_closing_the_pool_closes_every_session_even_when_one_fails(monkeypatch) -> None:
    closed: list[BrowserSession] = []

    async def aclose(self: BrowserSession) -> None:
        closed.append(self)
        if len(closed) == 1:
            raise RuntimeError("driver gone")

    monkeypatch.setattr(BrowserSession, "aclose", aclose)
    pool = BrowserPool(2)
    sessions = [
        await pool.acquire(_target(f"https://www.example.test/c/{n}"), None, PlayWrightFetcher)
        for n in (1, 2)
    ]

    with pytest.raises(RuntimeError):
        await pool.aclose()
    assert sorted(map(id, closed)) == sorted(map(id, sessions)) and pool.sessions == 0


def test_the_active_pool_is_scoped_to_the_run() -> None:
    pool = BrowserPool(1)
    with activate_browser_pool(pool):
        assert current_browser_pool() is pool
    assert current_browser_pool() is None


async def test_pooled_targets_share_one_session_that_stays_open_between_them(
    monkeypatch, closed, tmp_path,
) -> None:
    from scrapling.engines.toolbelt.custom import Response

    from scrapeyard.config.schema import RetryConfig
    from scrapeyard.engine import scraper

    used: list[object] = []

    async def fetch_browser_response(fetcher_cls, url, *_args, **_kwargs):
        used.append(fetcher_cls)
        html = '<li class="p"><span class="name">A</span></li>'
        return Response(
            url=url, text=html, body=html.encode(), status=200, reason="OK", cookies={},
            headers={}, request_headers={}, encoding="utf-8",
            **PlayWrightFetcher._generate_parser_arguments(),
        ), {}

    monkeypatch.setattr(scraper, "fetch_browser_response", fetch_browser_response)
    monkeypatch.setattr(scraper, "_assert_fetch_url", _async_noop)

    async def scrape(url: str) -> None:
        result = await scraper.scrape_target(
            _target(url), False, RetryConfig(max_attempts=1), adaptive_dir=str(tmp_path),
        )
        assert [record["name"] for record in result.data] == ["A"]

    pool = BrowserPool(1)
    with activate_browser_pool(pool):
        await scrape("https://www.example.test/c/1")
        await scrape("https://www.example.test/c/2")
    assert used[0] is used[1] and closed == []
    await pool.aclose()
    assert closed == [used[0]]

    closed.clear()
    await scrape("https://www.example.test/c/1")
    assert used[2] is not used[0] and closed == [used[2]]


async def _async_noop(*_args: object, **_kwargs: object) -> None:
    return None
