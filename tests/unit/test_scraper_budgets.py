from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.basic_fetch import BasicSession
from scrapeyard.engine.resilience import RetryHandler
from scrapeyard.engine.scraper import _fetch_basic_with_safe_redirects, _fetch_page
from scrapeyard.engine.url_guard import ResolvedPublicURL

_ORIGIN = "http://www.example.test"


def _budget(max_fetched_bytes: int) -> RunBudget:
    return RunBudget(
        max_duration_seconds=60,
        max_fetched_bytes=max_fetched_bytes,
        max_extracted_records=100,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1000,
    )


@asynccontextmanager
async def _local_site(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[str], bytes],
) -> AsyncIterator[list[str]]:
    """Serve ``_ORIGIN`` from a loopback socket; yield the requested paths."""

    paths: list[str] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            path = request.split(b" ", 2)[1].decode()
            paths.append(path)
            writer.write(respond(path))
            await writer.drain()
        finally:
            writer.close()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        "scrapeyard.engine.scraper.resolve_public_url",
        lambda url: ResolvedPublicURL(
            url.replace(_ORIGIN, f"http://127.0.0.1:{port}", 1),
            "www.example.test",
            "www.example.test",
        ),
    )
    try:
        yield paths
    finally:
        server.close()
        await server.wait_closed()


def _http(status: str, body: bytes, *headers: str) -> bytes:
    head = [f"HTTP/1.1 {status}", f"Content-Length: {len(body)}", "Connection: close", *headers]
    return ("\r\n".join(head) + "\r\n\r\n").encode() + body


@pytest.mark.asyncio
async def test_basic_redirect_bodies_are_counted_aggregate(monkeypatch):
    def respond(path: str) -> bytes:
        if path == "/start":
            return _http("302 Found", b"abc", "Location: /final")
        return _http("200 OK", b"def")

    budget = _budget(5)
    session = BasicSession()
    try:
        async with _local_site(monkeypatch, respond) as paths:
            with pytest.raises(BudgetExceeded) as exc_info:
                await _fetch_basic_with_safe_redirects(
                    session,
                    f"{_ORIGIN}/start",
                    {"timeout": 5},
                    {},
                    budget=budget,
                )
    finally:
        await session.aclose()

    assert paths == ["/start", "/final"]
    assert exc_info.value.limit_name is BudgetLimitName.fetched_bytes
    assert exc_info.value.observed_amount == 6
    assert budget.fetched_bytes == 3


@pytest.mark.asyncio
async def test_retry_response_bodies_are_counted_aggregate(monkeypatch):
    target = TargetConfig(
        url=_ORIGIN,
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    budget = _budget(5)
    budget.sleep = AsyncMock()
    retry = RetryHandler(
        RetryConfig(max_attempts=2, backoff="fixed", backoff_max=1),
        budget=budget,
    )
    session = BasicSession()
    try:
        async with _local_site(monkeypatch, lambda _path: _http("503 Busy", b"abc")) as paths:
            with pytest.raises(BudgetExceeded) as exc_info:
                await retry.execute(
                    _fetch_page,
                    session,
                    target.url,
                    target,
                    target.fetcher,
                    False,
                    {503},
                    "/tmp/adaptive",
                    None,
                    None,
                    budget,
                )
    finally:
        await session.aclose()

    assert paths == ["/", "/"]
    assert exc_info.value.limit_name is BudgetLimitName.fetched_bytes
    assert exc_info.value.observed_amount == 6


@pytest.mark.asyncio
async def test_request_cap_stops_redirects_before_another_transport_call(monkeypatch):
    calls = []

    async def fetch(_fetcher_cls, url, call_kwargs, **_options):
        calls.append(call_kwargs["header_url"])
        return SimpleNamespace(
            status=302 if len(calls) < 3 else 200,
            headers={"location": f"/hop-{len(calls)}"},
            url=url,
        )

    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", fetch)
    budget = _budget(10000)
    budget.max_requests = 2
    with pytest.raises(BudgetExceeded) as failure:
        await _fetch_basic_with_safe_redirects(
            BasicSession(), "https://example.com/start", {}, {}, budget=budget,
        )
    assert failure.value.limit_name.value == "requests"
    assert calls == ["https://example.com/start", "https://example.com/hop-1"]
    assert budget.requests == 2
