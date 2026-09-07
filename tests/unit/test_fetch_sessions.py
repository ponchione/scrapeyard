"""Target-scoped transport reuse, isolation, retry, and cleanup contracts."""

import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import pytest

from scrapeyard.common.async_tools import await_cleanup
from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.config.schema import RetryConfig, TargetConfig
from scrapeyard.engine.basic_fetch import BasicSession
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
