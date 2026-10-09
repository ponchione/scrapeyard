"""Page-parameter and click pagination modes."""

from __future__ import annotations

import asyncio
import gc
import time
import weakref
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from scrapling import Fetcher

from scrapeyard.config.schema import FetcherType, PaginationMode, RetryConfig, TargetConfig
from scrapeyard.engine import browser_session, pagination
from scrapeyard.engine.browser_debug import click_pagination_spec, fetch_browser_response
from scrapeyard.engine.browser_session import BrowserSession
from scrapeyard.engine.pagination import paginate_target, with_query_param
from scrapeyard.engine.scrape_models import (
    CLICK_PAGINATION_ATTRIBUTE,
    ClickPaginationResult,
    ClickPaginationSpec,
    ClickSnapshot,
    FetchOutcome,
    TargetResult,
)


def _target(pagination_config: dict[str, Any], *, fetcher: str = "basic", **extra: Any) -> TargetConfig:
    return TargetConfig.model_validate(
        {
            "url": "https://shop.example.test/list?cid=7&sort=price+asc",
            "fetcher": fetcher,
            "selectors": {"title": "h1"},
            "pagination": pagination_config,
            **extra,
        }
    )


# --- configuration ---------------------------------------------------------


def test_page_param_is_a_complete_pagination_source():
    target = _target({"page_param": "page", "max_pages": 5})

    assert target.pagination.mode is PaginationMode.link
    assert target.pagination.next is None
    assert (target.pagination.page_first, target.pagination.page_step) == (1, 1)


@pytest.mark.parametrize(
    ("config", "fetcher", "message"),
    [
        ({"max_pages": 3}, "basic", "next selector or page_param"),
        ({"mode": "click"}, "dynamic", "click pagination requires a next selector"),
        ({"mode": "click", "next": "button.next", "page_param": "page"}, "dynamic", "link pagination"),
        ({"mode": "click", "next": "button.next"}, "basic", "dynamic or stealthy fetcher"),
        ({"page_param": "page number"}, "basic", "page_param must contain only"),
        ({"page_param": "page", "page_step": 0}, "basic", "greater than or equal to 1"),
    ],
)
def test_pagination_mode_validation(config: dict[str, Any], fetcher: str, message: str):
    with pytest.raises(ValidationError, match=message):
        _target(config, fetcher=fetcher)


def test_click_pagination_spec_uses_next_and_item_selectors():
    target = _target(
        {"mode": "click", "next": {"type": "xpath", "query": "//button[@rel='next']"}, "max_pages": 4},
        fetcher="dynamic",
        item_selector="li.card",
    )

    assert click_pagination_spec(target) == ClickPaginationSpec(
        next_query="//button[@rel='next']",
        next_type="xpath",
        item_query="li.card",
        item_type="css",
        max_pages=4,
    )
    assert click_pagination_spec(_target({"next": "a.next"})) is None


@pytest.mark.asyncio
async def test_click_pagination_rejects_a_non_session_browser_fetch():
    target = _target({"mode": "click", "next": "button.next"}, fetcher="dynamic")

    with pytest.raises(ValueError, match="browser session"):
        await fetch_browser_response(object(), target.url, target, FetcherType.dynamic, {}, None)


# --- page parameter URLs ----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "name", "value", "expected"),
    [
        (
            "https://shop.example.test/list?cid=7&sort=price+asc",
            "page",
            2,
            "https://shop.example.test/list?cid=7&sort=price+asc&page=2",
        ),
        (
            "https://shop.example.test/list?page=1&cid=7&page=9#top",
            "page",
            3,
            "https://shop.example.test/list?page=3&cid=7",
        ),
        ("https://shop.example.test/list", "start", 48, "https://shop.example.test/list?start=48"),
        (
            "https://shop.example.test/list?f%5B%5D=a",
            "p[n]",
            2,
            "https://shop.example.test/list?f%5B%5D=a&p[n]=2",
        ),
    ],
)
def test_with_query_param_preserves_other_segments(url: str, name: str, value: int, expected: str):
    assert with_query_param(url, name, value) == expected


# --- page parameter pagination ---------------------------------------------


class _Page:
    def __init__(self, *, has_next: bool = True) -> None:
        self.has_next = has_next

    def css(self, selector: str):
        assert selector == "a.next"
        return [object()] if self.has_next else []


async def _run_page_param(
    target: TargetConfig,
    pages: list[tuple[_Page, list[dict[str, Any]]]],
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_data: list[dict[str, Any]] | None = None,
    first_page: _Page | None = None,
) -> tuple[TargetResult, AsyncMock]:
    monkeypatch.setattr(pagination, "_pagination_url_is_safe", AsyncMock(return_value=True))
    result = TargetResult(
        url=target.url,
        status="success",
        data=list(first_data or [{"title": "p1"}]),
        pages_scraped=1,
        debug={"final_url": target.url},
    )
    outcomes = iter(pages)
    page_data = {}

    async def fetch_page(*args: Any, **kwargs: Any) -> FetchOutcome:
        page, data = next(outcomes)
        page_data[id(page)] = data
        return FetchOutcome(page=page, debug={"final_url": args[2]})

    fetch = AsyncMock(side_effect=fetch_page)
    await paginate_target(
        page=first_page or _Page(),
        target=target,
        result=result,
        fetch_target_page=fetch,
        extract_page_data=lambda page, _target: page_data[id(page)],
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )
    return result, fetch


@pytest.mark.asyncio
async def test_page_param_builds_numbered_urls_until_an_empty_page(monkeypatch: pytest.MonkeyPatch):
    target = _target({"page_param": "page", "max_pages": 10})

    result, fetch = await _run_page_param(
        target,
        [(_Page(), [{"title": "p2"}]), (_Page(), [{"title": "p3"}]), (_Page(), [])],
        monkeypatch,
    )

    assert [call.args[2] for call in fetch.await_args_list] == [
        "https://shop.example.test/list?cid=7&sort=price+asc&page=2",
        "https://shop.example.test/list?cid=7&sort=price+asc&page=3",
        "https://shop.example.test/list?cid=7&sort=price+asc&page=4",
    ]
    assert result.data == [{"title": "p1"}, {"title": "p2"}, {"title": "p3"}]
    assert result.pages_scraped == 4
    assert result.pagination_stop_reason == "exhausted"


@pytest.mark.asyncio
async def test_page_param_supports_offsets(monkeypatch: pytest.MonkeyPatch):
    target = _target({"page_param": "start", "page_first": 0, "page_step": 24, "max_pages": 3})

    result, fetch = await _run_page_param(
        target,
        [(_Page(), [{"title": "p2"}]), (_Page(), [{"title": "p3"}])],
        monkeypatch,
    )

    assert [call.args[2].rsplit("start=", 1)[1] for call in fetch.await_args_list] == ["24", "48"]
    assert result.pagination_stop_reason == "max_pages"
    assert result.pages_scraped == 3


@pytest.mark.asyncio
async def test_page_param_stops_when_a_page_repeats_earlier_records(monkeypatch: pytest.MonkeyPatch):
    target = _target({"page_param": "page", "max_pages": 10})

    result, fetch = await _run_page_param(
        target,
        [(_Page(), [{"title": "p2"}]), (_Page(), [{"title": "p1"}])],
        monkeypatch,
    )

    assert fetch.await_count == 2
    assert result.data == [{"title": "p1"}, {"title": "p2"}]
    assert result.pages_scraped == 2
    assert result.pagination_stop_reason == "repeated_page"


@pytest.mark.asyncio
async def test_page_param_uses_next_element_only_as_a_continuation_signal(monkeypatch: pytest.MonkeyPatch):
    target = _target({"page_param": "page", "next": "a.next", "max_pages": 10})

    result, fetch = await _run_page_param(
        target,
        [(_Page(has_next=False), [{"title": "p2"}])],
        monkeypatch,
    )

    assert fetch.await_count == 1
    assert result.data == [{"title": "p1"}, {"title": "p2"}]
    assert result.pagination_stop_reason == "exhausted"


@pytest.mark.asyncio
async def test_page_param_refuses_unsafe_destinations(monkeypatch: pytest.MonkeyPatch):
    target = _target({"page_param": "page", "max_pages": 10})
    monkeypatch.setattr(pagination, "_pagination_url_is_safe", AsyncMock(return_value=False))
    result = TargetResult(url=target.url, status="success", data=[{"title": "p1"}], pages_scraped=1)
    fetch = AsyncMock()
    await paginate_target(
        page=_Page(),
        target=target,
        result=result,
        fetch_target_page=fetch,
        extract_page_data=MagicMock(),
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )

    fetch.assert_not_awaited()
    assert result.pagination_stop_reason == "unsafe_next_url"


# --- click pagination extraction -------------------------------------------


class _Snapshot:
    def __init__(self, title: str) -> None:
        self.title = title


def _click_snapshot(title: str) -> ClickSnapshot:
    return ClickSnapshot.capture(
        f"<html><body><h1>{title}</h1></body></html>",
        {
            "url": "https://shop.example.test/list",
            "status": 200,
            "reason": "OK",
            "cookies": {},
            "headers": {},
            "request_headers": {},
            **Fetcher._generate_parser_arguments(),
        },
    )


def _extract_title(page: Any, _target: TargetConfig) -> list[dict[str, Any]]:
    return [{"title": page.css("h1::text").get()}]


async def _paginate_click_snapshots(
    snapshots: list[ClickSnapshot], extract_page_data: Any = _extract_title,
) -> tuple[TargetResult, AsyncMock]:
    target = _target({"mode": "click", "next": "button.next", "max_pages": 5}, fetcher="dynamic")
    first = _Snapshot("p1")
    setattr(
        first,
        CLICK_PAGINATION_ATTRIBUTE,
        ClickPaginationResult(pages=snapshots, stop_reason="exhausted"),
    )
    result = TargetResult(url=target.url, status="success", data=[{"title": "p1"}], pages_scraped=1)
    fetch = AsyncMock()

    await paginate_target(
        page=first,
        target=target,
        result=result,
        fetch_target_page=fetch,
        extract_page_data=extract_page_data,
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )
    return result, fetch


@pytest.mark.asyncio
async def test_click_mode_extracts_each_rendered_snapshot_without_fetching():
    result, fetch = await _paginate_click_snapshots([_click_snapshot("p2"), _click_snapshot("p3")])

    fetch.assert_not_awaited()
    assert result.data == [{"title": "p1"}, {"title": "p2"}, {"title": "p3"}]
    assert result.pages_scraped == 3
    assert result.pagination_stop_reason == "exhausted"


@pytest.mark.asyncio
async def test_click_mode_frees_each_parse_tree_before_parsing_the_next_snapshot():
    parsed: list[weakref.ref[Any]] = []
    alive_at_parse: list[int] = []

    def extract(page: Any, target: TargetConfig) -> list[dict[str, Any]]:
        alive_at_parse.append(sum(ref() is not None for ref in parsed))
        parsed.append(weakref.ref(page))
        return _extract_title(page, target)

    # Reference counting alone must free each tree: no cyclic collection runs.
    gc.disable()
    try:
        result, _fetch = await _paginate_click_snapshots(
            [_click_snapshot(f"p{number}") for number in range(2, 6)], extract,
        )
    finally:
        gc.enable()

    assert alive_at_parse == [0, 0, 0, 0]
    assert all(ref() is None for ref in parsed)
    assert [row["title"] for row in result.data] == ["p1", "p2", "p3", "p4", "p5"]


def test_click_snapshot_keeps_compressed_html_until_parsed():
    html = "<html><body>" + "<li class='card'>Scope 3-9x40</li>" * 2_000 + "</body></html>"
    snapshot = ClickSnapshot.capture(html, {"url": "https://shop.example.test/list"})

    assert snapshot.html == html
    assert len(snapshot.compressed_html) < len(html) // 10


@pytest.mark.asyncio
async def test_click_mode_without_captured_snapshots_reports_unknown_coverage():
    target = _target({"mode": "click", "next": "button.next"}, fetcher="dynamic")
    result = TargetResult(url=target.url, status="success", data=[{"title": "p1"}], pages_scraped=1)

    await paginate_target(
        page=_Snapshot("p1"),
        target=target,
        result=result,
        fetch_target_page=AsyncMock(),
        extract_page_data=MagicMock(),
        retry_handler=MagicMock(spec=RetryConfig),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
    )

    assert result.pages_scraped == 1
    assert result.pagination_stop_reason == "unknown"


# --- click pagination in the live browser session --------------------------


class _Listing:
    """A client-rendered listing whose next control swaps the visible items."""

    def __init__(
        self,
        pages: list[str],
        *,
        stuck_after: int | None = None,
        click_error_after: int | None = None,
        disabled_on_last: bool = False,
    ) -> None:
        self.pages = pages
        self.index = 0
        self.stuck_after = stuck_after
        self.click_error_after = click_error_after
        self.disabled_on_last = disabled_on_last
        self.clicks = 0
        self.url = "https://shop.example.test/list"
        self.listeners: dict[str, list[Any]] = {}

    def on(self, event: str, handler: Any) -> None:
        self.listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self.listeners[event].remove(handler)

    def locator(self, query: str) -> _Locator:
        assert query == "button.next"
        return _Locator(self)

    async def evaluate(self, script: str, argument: Any) -> dict[str, Any]:
        assert argument == ["li.card", "css"]
        return {"count": 1, "hash": self.pages[self.index]}

    async def wait_for_timeout(self, _ms: float) -> None:
        return None

    async def wait_for_function(
        self, _script: str, *, arg: list[Any], polling: float, timeout: float,
    ) -> _Handle:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        query, selector_type, count, fingerprint = arg
        current = await self.evaluate(_script, [query, selector_type])
        if (current["count"], current["hash"]) == (count, fingerprint):
            raise PlaywrightTimeoutError(f"items unchanged after {timeout}ms")
        return _Handle(current)

    async def wait_for_load_state(self, _state: str) -> None:
        return None

    async def content(self) -> str:
        return f"<html><body><li class='card'>{self.pages[self.index]}</li></body></html>"


class _Handle:
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value

    async def json_value(self) -> dict[str, Any]:
        return self.value

    async def dispose(self) -> None:
        return None


class _Locator:
    def __init__(self, listing: _Listing) -> None:
        self.listing = listing

    @property
    def first(self) -> _Locator:
        return self

    async def count(self) -> int:
        last = self.listing.index == len(self.listing.pages) - 1
        return 0 if last and not self.listing.disabled_on_last else 1

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def get_attribute(self, name: str) -> str | None:
        assert name == "aria-disabled"
        last = self.listing.index == len(self.listing.pages) - 1
        return "true" if last and self.listing.disabled_on_last else None

    async def click(self, timeout: float) -> None:
        listing = self.listing
        listing.clicks += 1
        if listing.click_error_after is not None and listing.clicks > listing.click_error_after:
            raise RuntimeError("element is covered by another element")
        if listing.stuck_after is not None and listing.clicks > listing.stuck_after:
            return
        listing.index += 1


def _session_response_parts() -> tuple[MagicMock, MagicMock, MagicMock]:
    context = MagicMock()
    context.cookies = AsyncMock(return_value=[])
    engine = MagicMock(timeout=300, wait_selector=None, wait=0, adaptor_arguments={})
    response = MagicMock(status=200, status_text="OK", headers={"content-type": "text/html"})
    response.all_headers = AsyncMock(return_value={})
    response.request.all_headers = AsyncMock(return_value={})
    return context, engine, response


async def _click_through(listing: _Listing, max_pages: int) -> ClickPaginationResult:
    context, engine, response = _session_response_parts()
    spec = ClickPaginationSpec(
        next_query="button.next",
        next_type="css",
        item_query="li.card",
        item_type="css",
        max_pages=max_pages,
    )
    session = BrowserSession(MagicMock())
    return await session._click_through_pages(listing, context, engine, response, response, None, spec)


def _titles(result: ClickPaginationResult) -> list[str]:
    titles = []
    for snapshot in result.pages:
        assert isinstance(snapshot, ClickSnapshot)
        with snapshot.parsed() as page:
            titles.append(page.css("li.card::text").get())
    return titles


@pytest.mark.asyncio
async def test_click_through_captures_each_page_until_next_disappears():
    result = await _click_through(_Listing(["p1", "p2", "p3"]), max_pages=10)

    assert _titles(result) == ["p2", "p3"]
    assert result.stop_reason == "exhausted"


@pytest.mark.asyncio
async def test_click_through_treats_aria_disabled_next_as_exhausted():
    result = await _click_through(_Listing(["p1", "p2"], disabled_on_last=True), max_pages=10)

    assert _titles(result) == ["p2"]
    assert result.stop_reason == "exhausted"


@pytest.mark.asyncio
async def test_click_through_stops_at_the_page_cap_with_next_still_available():
    listing = _Listing(["p1", "p2", "p3", "p4"])

    result = await _click_through(listing, max_pages=2)

    assert _titles(result) == ["p2"]
    assert listing.clicks == 1
    assert result.stop_reason == "max_pages"


@pytest.mark.asyncio
async def test_click_through_reports_a_click_that_never_changes_the_items():
    result = await _click_through(_Listing(["p1", "p2", "p3"], stuck_after=1), max_pages=10)

    assert _titles(result) == ["p2"]
    assert result.stop_reason == "repeated_page"


@pytest.mark.asyncio
async def test_click_through_keeps_captured_pages_when_a_click_fails():
    result = await _click_through(_Listing(["p1", "p2", "p3"], click_error_after=1), max_pages=10)

    assert _titles(result) == ["p2"]
    assert result.stop_reason == "unknown"


@pytest.mark.asyncio
async def test_click_change_wait_rearms_after_a_navigation_replaces_the_document():
    listing = _Listing(["p1", "p2"])
    wait_for_function = listing.wait_for_function
    calls = 0

    async def navigating(script: str, **kwargs: Any) -> _Handle:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Execution context was destroyed by a navigation")
        return await wait_for_function(script, **kwargs)

    listing.wait_for_function = navigating  # type: ignore[method-assign]

    result = await _click_through(listing, max_pages=10)

    assert _titles(result) == ["p2"]
    assert calls == 2


@pytest.mark.asyncio
async def test_click_through_removes_its_request_listeners():
    listing = _Listing(["p1", "p2"])

    await _click_through(listing, max_pages=10)

    assert all(not handlers for handlers in listing.listeners.values())
    assert set(listing.listeners) == {"request", "requestfinished", "requestfailed"}


@pytest.mark.asyncio
async def test_click_through_with_a_single_page_cap_only_inspects_next():
    listing = _Listing(["p1", "p2"])

    result = await _click_through(listing, max_pages=1)

    assert result.pages == []
    assert listing.clicks == 0
    assert result.stop_reason == "max_pages"


class _LivePage(_Listing):
    """Enough of a Playwright page for one BrowserSession.fetch call."""

    def __init__(self, pages: list[str]) -> None:
        super().__init__(pages)
        self.frames: list[object] = []
        self.first_response = MagicMock(status=200, status_text="OK", headers={"content-type": "text/html"})
        self.first_response.all_headers = AsyncMock(return_value={})
        self.first_response.request.all_headers = AsyncMock(return_value={})
        self.first_response.request.redirected_from = None

    def set_default_navigation_timeout(self, _timeout: float) -> None:
        return None

    def set_default_timeout(self, _timeout: float) -> None:
        return None

    async def goto(self, url: str, referer: str | None = None) -> MagicMock:
        return self.first_response

    async def close(self) -> None:
        return None

    def is_closed(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_browser_session_fetch_attaches_click_pagination_snapshots():
    from scrapling import PlayWrightFetcher

    listing = _LivePage(["p1", "p2", "p3"])
    browser = MagicMock()
    browser.is_connected.return_value = True
    context = MagicMock(browser=browser, pages=[listing])
    context.route = AsyncMock()
    context.unroute = AsyncMock()
    context.set_offline = AsyncMock()
    context.new_page = AsyncMock(return_value=listing)
    context.cookies = AsyncMock(return_value=[])
    session = BrowserSession(PlayWrightFetcher)
    session._context = context
    spec = ClickPaginationSpec(
        next_query="button.next",
        next_type="css",
        item_query="li.card",
        item_type="css",
        max_pages=5,
    )

    async def guard(route: Any) -> None:
        await route.continue_()

    response = await session.fetch(
        listing.url, {"click_pagination": spec, "timeout": 300}, guard,
    )

    assert response.css("li.card::text").get() == "p1"
    click_result = getattr(response, CLICK_PAGINATION_ATTRIBUTE)
    assert _titles(click_result) == ["p2", "p3"]
    assert click_result.stop_reason == "exhausted"
    context.set_offline.assert_awaited_with(True)


# --- settling after a click -------------------------------------------------


class _MutationState:
    def __init__(self, page: _SettlingPage) -> None:
        self.page = page

    async def evaluate(self, script: str) -> int | None:
        if self.page.observe_error is not None:
            raise self.page.observe_error
        if "disconnect" in script:
            self.page.disconnected = True
            return None
        return self.page.mutations

    async def dispose(self) -> None:
        self.page.disposed = True


class _SettlingPage:
    """Counts DOM mutations the way the in-page MutationObserver does."""

    def __init__(self, *, observe_error: Exception | None = None) -> None:
        self.mutations = 0
        self.observe_error = observe_error
        self.waited: list[float] = []
        self.disconnected = False
        self.disposed = False

    async def evaluate_handle(self, script: str) -> _MutationState:
        assert "MutationObserver" in script
        return _MutationState(self)

    async def wait_for_timeout(self, ms: float) -> None:
        self.waited.append(ms)
        await asyncio.sleep(ms / 1000)


async def _timed_settle(page: Any, wait_ms: float, in_flight: set[Any]) -> float:
    started = time.monotonic()
    await BrowserSession._settle_after_click(page, wait_ms, in_flight)
    return time.monotonic() - started


@pytest.fixture
def _short_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_session, "_CLICK_SETTLE_QUIET_MS", 100)


@pytest.mark.asyncio
async def test_click_settle_ends_the_wait_once_the_page_is_quiet(_short_quiet: None):
    page = _SettlingPage()

    elapsed = await _timed_settle(page, 2_000, set())

    assert elapsed < 1.0
    assert page.waited == []
    assert (page.disconnected, page.disposed) == (True, True)


@pytest.mark.asyncio
async def test_click_settle_waits_while_the_document_keeps_changing(_short_quiet: None):
    page = _SettlingPage()

    async def render_late_content() -> None:
        for _ in range(10):
            await asyncio.sleep(0.05)
            page.mutations += 1

    rendering = asyncio.create_task(render_late_content())
    elapsed = await _timed_settle(page, 2_000, set())
    await rendering

    assert 0.6 <= elapsed < 1.9


@pytest.mark.asyncio
async def test_click_settle_waits_while_a_page_request_is_in_flight(_short_quiet: None):
    page = _SettlingPage()
    in_flight: set[Any] = {"price-request"}

    async def finish_request() -> None:
        await asyncio.sleep(0.5)
        in_flight.clear()

    finishing = asyncio.create_task(finish_request())
    elapsed = await _timed_settle(page, 2_000, in_flight)
    await finishing

    assert 0.6 <= elapsed < 1.9


@pytest.mark.asyncio
async def test_click_settle_never_waits_longer_than_configured(_short_quiet: None):
    page = _SettlingPage()

    elapsed = await _timed_settle(page, 400, {"long-poll"})

    assert 0.4 <= elapsed < 1.0


@pytest.mark.asyncio
async def test_click_settle_keeps_short_waits_unchanged():
    page = _SettlingPage()

    await BrowserSession._settle_after_click(page, 250, set())

    assert page.waited == [250]
    assert not page.disposed


@pytest.mark.asyncio
async def test_click_settle_waits_out_the_time_when_the_document_is_replaced(_short_quiet: None):
    page = _SettlingPage(observe_error=RuntimeError("Execution context was destroyed"))

    elapsed = await _timed_settle(page, 400, set())

    assert len(page.waited) == 1 and 300 < page.waited[0] <= 400
    assert elapsed >= 0.35


class _Request:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class _LatePriceListing(_Listing):
    """Each clicked page renders its items at once and its price after a request."""

    def __init__(self, pages: list[str], price_delay: float, *, resource_type: str = "fetch") -> None:
        super().__init__(pages)
        self.price_delay = price_delay
        self.resource_type = resource_type
        self.priced = 0
        self.mutations = 0

    async def content(self) -> str:
        price = "priced" if self.priced == self.index else "pending"
        return (
            f"<html><body><li class='card'>{self.pages[self.index]}</li>"
            f"<span class='price'>{price}</span></body></html>"
        )

    async def evaluate_handle(self, script: str) -> Any:
        listing = self

        class _State:
            async def evaluate(self, script: str) -> int | None:
                return None if "disconnect" in script else listing.mutations

            async def dispose(self) -> None:
                return None

        return _State()

    def emit(self, event: str, request: object) -> None:
        for handler in list(self.listeners.get(event, [])):
            handler(request)

    async def load_price(self, index: int) -> None:
        request = _Request(self.resource_type)
        self.emit("request", request)
        await asyncio.sleep(self.price_delay)
        self.priced = index
        self.mutations += 1
        self.emit("requestfinished", request)


async def _click_through_late_prices(listing: _LatePriceListing) -> tuple[ClickPaginationResult, float]:
    locator_click = _Locator.click
    loading: list[asyncio.Task[None]] = []

    async def click(self: _Locator, timeout: float) -> None:
        await locator_click(self, timeout)
        loading.append(asyncio.create_task(listing.load_price(listing.index)))

    context, engine, response = _session_response_parts()
    engine.wait = 3_000
    spec = ClickPaginationSpec(
        next_query="button.next", next_type="css", item_query="li.card", item_type="css",
        max_pages=10,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_Locator, "click", click)
        started = time.monotonic()
        result = await BrowserSession(MagicMock())._click_through_pages(
            listing, context, engine, response, response, None, spec,
        )
        elapsed = time.monotonic() - started
    await asyncio.gather(*loading)
    return result, elapsed


def _price(snapshot: ClickSnapshot) -> str | None:
    with snapshot.parsed() as page:
        return page.css("span.price::text").get()


@pytest.mark.asyncio
async def test_click_settle_captures_a_price_that_renders_after_the_items(_short_quiet: None):
    result, elapsed = await _click_through_late_prices(_LatePriceListing(["p1", "p2"], price_delay=0.4))

    assert _price(result.pages[0]) == "priced"
    assert 0.5 <= elapsed < 2.5


@pytest.mark.asyncio
async def test_click_settle_does_not_wait_for_presentation_requests(_short_quiet: None):
    listing = _LatePriceListing(["p1", "p2"], price_delay=1.0, resource_type="image")

    result, elapsed = await _click_through_late_prices(listing)

    # The image never changes the extracted page, so the click settles early.
    assert _price(result.pages[0]) == "pending"
    assert elapsed < 0.9


# --- click activation fallback ----------------------------------------------


class _InterceptedControl:
    def __init__(self, *, intercepted: bool) -> None:
        self.intercepted = intercepted
        self.clicks: list[float] = []
        self.dispatched: list[str] = []

    async def click(self, *, timeout: float) -> None:
        self.clicks.append(timeout)
        if self.intercepted:
            from playwright.async_api import TimeoutError as PlaywrightTimeoutError

            raise PlaywrightTimeoutError("element is covered by another element")

    async def dispatch_event(self, event: str) -> None:
        self.dispatched.append(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("intercepted,dispatched", [(False, []), (True, ["click"])])
async def test_click_activation_dispatches_when_an_overlay_intercepts(intercepted, dispatched):
    from scrapeyard.engine.browser_session import BrowserSession

    control = _InterceptedControl(intercepted=intercepted)

    await BrowserSession._activate_control(control, 90_000)

    assert control.clicks == [10_000]
    assert control.dispatched == dispatched


@pytest.mark.asyncio
async def test_click_activation_matches_timeouts_from_other_playwright_builds():
    from scrapeyard.engine.browser_session import BrowserSession

    class TimeoutError(Exception):  # noqa: A001 - mirrors a vendored Playwright build
        pass

    class _Control(_InterceptedControl):
        async def click(self, *, timeout: float) -> None:
            self.clicks.append(timeout)
            raise TimeoutError("covered")

    control = _Control(intercepted=True)
    await BrowserSession._activate_control(control, 5_000)
    assert control.clicks == [5_000]
    assert control.dispatched == ["click"]
