"""Target-scoped transport reuse, isolation, retry, and cleanup contracts."""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import pytest
import httpx
from scrapling import PlayWrightFetcher

from scrapeyard.common.async_tools import await_cleanup
from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.config.schema import RetryConfig, TargetConfig
from scrapeyard.engine.basic_fetch import BasicSession
from scrapeyard.engine.browser_session import BrowserSession
from scrapeyard.engine.scraper import scrape_target
from scrapeyard.engine.url_guard import ResolvedPublicURL


async def test_pagination_reuses_connections_with_logical_origin_cookie_isolation(monkeypatch):
    requests = []
    connections = 0
    closed = 0
    all_closed = asyncio.Event()

    async def handler(reader, writer):
        nonlocal connections, closed
        connections += 1
        connection = connections
        try:
            while True:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode().split("\r\n")
                path = lines[0].split()[1]
                headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
                requests.append((connection, path, headers))
                next_url = {
                    "/1": "/2", "/2": "http://other.example/3",
                    "/3": "http://origin.example/4",
                }.get(path)
                body = b"<h1>ok</h1>"
                if next_url:
                    body += f'<a href="{next_url}">next</a>'.encode()
                cookie = b"Set-Cookie: gate=ok; Path=/\r\n" if path == "/1" else b""
                writer.write(b"HTTP/1.1 200 OK\r\n" + cookie
                             + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            closed += 1
            if closed == 6:
                all_closed.set()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    validated = []

    def resolve(url):
        validated.append(url)
        parsed = urlsplit(url)
        return ResolvedPublicURL(f"http://127.0.0.1:{port}{parsed.path}", parsed.hostname, parsed.hostname)

    monkeypatch.setattr("scrapeyard.engine.scraper.resolve_public_url", resolve)
    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())
    monkeypatch.setattr("scrapeyard.engine.pagination.assert_public_url", lambda *_a, **_kw: None)
    target = TargetConfig.model_validate({
        "url": "http://origin.example/1", "selectors": {"title": "h1"},
        "pagination": {"next": "a", "max_pages": 4},
    })
    try:
        for _ in range(2):
            result = await scrape_target(target, False, RetryConfig(max_attempts=1))
            assert result.status == "success"
            assert result.pages_scraped == 4
        await asyncio.wait_for(all_closed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
    assert connections == closed == 6
    assert len(validated) == 8
    for run in (requests[:4], requests[4:]):
        assert run[0][0] == run[1][0]  # Same origin and validated endpoint reuse TCP.
        assert len({entry[0] for entry in run}) == 3  # Same IP, different origins do not.
        assert run[0][2].get("Cookie") is None
        assert run[1][2]["Cookie"] == "gate=ok"
        assert run[2][2].get("Cookie") is None
        assert run[3][2]["Cookie"] == "gate=ok"


@pytest.mark.parametrize("ending", ["success", "retry", "cancel", "budget"])
async def test_target_closes_live_transport_and_resets_failed_attempt(monkeypatch, ending):
    cookies = []
    clients = []
    connected = asyncio.Event()
    closed = asyncio.Event()

    async def handler(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            cookies.append(b"Cookie:" in request)
            connected.set()
            if ending == "cancel":
                await reader.read()
                return
            retry = ending == "retry" and len(cookies) == 1
            status = b"503 Unavailable" if retry else b"200 OK"
            writer.write(b"HTTP/1.1 " + status + b"\r\nSet-Cookie: poison=yes\r\n"
                         b"Content-Length: 11\r\n\r\n<h1>ok</h1>")
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()
            closed.set()

    original = BasicSession.client

    async def record_client(self, *args):
        client = await original(self, *args)
        clients.append(client)
        return client

    monkeypatch.setattr(BasicSession, "client", record_client)
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr("scrapeyard.engine.scraper.resolve_public_url", lambda _url:
                        ResolvedPublicURL(f"http://127.0.0.1:{port}/", "origin.example", "origin.example"))
    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())
    target = TargetConfig(url="http://origin.example/", selectors={"title": "h1"})
    budget = RunBudget(
        max_duration_seconds=10, max_fetched_bytes=1 if ending == "budget" else 1000,
        max_extracted_records=100, max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1024,
    )
    task = asyncio.create_task(scrape_target(
        target, False, RetryConfig(max_attempts=2, backoff_max=0), budget=budget,
    ))
    try:
        await asyncio.wait_for(connected.wait(), 2)
        if ending == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "budget":
            with pytest.raises(BudgetExceeded):
                await task
        else:
            assert (await task).status == "success"
        await asyncio.wait_for(closed.wait(), 2)
    finally:
        server.close()
        await server.wait_closed()
    assert all(client.is_closed for client in clients)
    assert cookies == ([False, False] if ending == "retry" else [False])
    if ending == "retry":
        assert clients[0] is not clients[1]


async def test_cleanup_retains_ownership_through_repeated_cancellation():
    release = asyncio.Event()
    cleanup = asyncio.create_task(release.wait())
    owner = asyncio.create_task(await_cleanup(cleanup))
    await asyncio.sleep(0)
    for _ in range(2):
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        assert not cleanup.cancelled()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert cleanup.done()


@pytest.mark.parametrize("ending", ["disconnect", "crash", "budget_disconnect", "cancel"])
async def test_browser_cleanup_preserves_failure_and_settles_requests(ending):
    browser = MagicMock()
    browser.is_connected.return_value = True
    page = MagicMock(frames=[])
    page.close = AsyncMock()
    context = MagicMock(browser=browser, pages=[page])
    context.route = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.unroute = AsyncMock(side_effect=RuntimeError("closed context"))

    async def offline(value):
        if value:
            raise RuntimeError("Target crashed")

    context.set_offline = AsyncMock(side_effect=offline)
    session = BrowserSession(PlayWrightFetcher)
    session._context = context
    stop_driver = AsyncMock()
    session._stack.push_async_callback(stop_driver)
    budget = RunBudget(max_duration_seconds=10, max_requests=100,
        max_fetched_bytes=1000, max_extracted_records=100,
        max_serialized_result_bytes=1000, max_browser_debug_bytes=1000)

    async def guard(route):
        await route.continue_()

    async def navigate(*args, **kwargs):
        route = MagicMock(request=MagicMock(redirected_from=None), continue_=AsyncMock())
        await context.route.call_args.args[1](route)
        browser.is_connected.return_value = "disconnect" not in ending
        if ending == "disconnect":
            raise RuntimeError("Disconnected navigation")
        if ending == "cancel":
            raise asyncio.CancelledError()
        raise BudgetExceeded(BudgetLimitName.fetched_bytes, 1000, 1001)

    page.goto = AsyncMock(side_effect=navigate)
    expected = httpx.NetworkError if ending == "disconnect" else BudgetExceeded
    if ending == "cancel":
        expected = asyncio.CancelledError
    with pytest.raises(expected):
        await session.fetch("https://fixture.example/", {}, guard, budget=budget)
    stop_driver.assert_awaited_once()
    assert session._context is None
    assert budget.snapshot()["requests"] == 1


class _Frame:
    def __init__(self, behavior: str) -> None:
        self.behavior = behavior
        self.denied = False

    def is_detached(self) -> bool:
        return False

    async def evaluate(self, _script: str) -> None:
        if self.behavior == "navigating":
            raise RuntimeError("Execution context was destroyed")
        if self.behavior == "busy":
            await asyncio.Event().wait()
        self.denied = True


async def test_page_close_tolerates_frames_that_cannot_take_request_denial() -> None:
    from scrapeyard.engine.browser_session import _close_page

    healthy = _Frame("healthy")
    page = MagicMock(frames=[_Frame("navigating"), _Frame("busy"), healthy])
    page.is_closed.return_value = False
    page.close = AsyncMock()
    browser = MagicMock()
    browser.is_connected.return_value = True

    loop = asyncio.get_running_loop()
    started = loop.time()
    await _close_page(page, browser)

    assert loop.time() - started < 1.5
    assert healthy.denied
    page.close.assert_awaited_once()


async def test_page_close_still_raises_when_the_page_cannot_close() -> None:
    from scrapeyard.engine.browser_session import _close_page

    page = MagicMock(frames=[_Frame("navigating")])
    page.is_closed.return_value = False
    page.close = AsyncMock(side_effect=RuntimeError("Target crashed"))
    browser = MagicMock()
    browser.is_connected.return_value = True

    with pytest.raises(RuntimeError, match="Target crashed"):
        await _close_page(page, browser)


class _Request:
    def __init__(self, url: str, redirected_from: "_Request | None" = None) -> None:
        self.url = url
        self.redirected_from = redirected_from
        self.resource_type = "script"
        self.method = "GET"


def _response(request: _Request, status: int) -> MagicMock:
    return MagicMock(status=status, url=request.url, request=request, headers={"content-length": "0"})


async def test_browser_returns_unused_redirect_capacity_when_each_chain_ends():
    browser = MagicMock()
    browser.is_connected.return_value = True
    page = MagicMock(frames=[])
    page.close = AsyncMock()
    context = MagicMock(browser=browser, pages=[page])
    context.route = AsyncMock()
    context.unroute = AsyncMock()
    context.set_offline = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    session = BrowserSession(PlayWrightFetcher)
    session._context = context
    budget = RunBudget(max_duration_seconds=10, max_requests=100,
        max_fetched_bytes=1000, max_extracted_records=100,
        max_serialized_result_bytes=1000, max_browser_debug_bytes=1000)

    async def guard(route):
        await route.continue_()

    async def settle() -> None:
        for _ in range(10):
            await asyncio.sleep(0)

    async def navigate(*args, **kwargs):
        handlers = {call.args[0]: call.args[1] for call in context.on.call_args_list}
        roots = [_Request(f"https://fixture.example/{n}") for n in range(4)]
        for root in roots:
            await context.route.call_args.args[1](MagicMock(request=root, continue_=AsyncMock()))
        await settle()
        assert budget.requests == 4 * 21

        handlers["response"](_response(roots[0], 200))
        handlers["response"](_response(roots[1], 302))
        hop = _Request("https://fixture.example/moved", redirected_from=roots[1])
        handlers["request"](hop)
        handlers["response"](_response(hop, 200))
        handlers["requestfailed"](roots[2])
        await settle()
        # Ended chains count only what they sent; the open one keeps its 21.
        assert budget.requests == 1 + 2 + 1 + 21
        raise RuntimeError("stop after accounting")

    page.goto = AsyncMock(side_effect=navigate)
    with pytest.raises(RuntimeError, match="stop after accounting"):
        await session.fetch("https://fixture.example/", {}, guard, budget=budget)
    # The chain still open at the end settles to the one request it sent.
    assert budget.requests == 1 + 2 + 1 + 1
