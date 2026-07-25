from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Browser, async_playwright

from scrapeyard.engine.browser_debug import _BrowserNetworkGuard
from scrapeyard.engine.url_guard import UnsafeURLError, canonical_url_origin


pytestmark = [
    pytest.mark.live_browser,
    pytest.mark.skipif(
        os.environ.get("SCRAPEYARD_RUN_LIVE_BROWSER") != "1",
        reason="set SCRAPEYARD_RUN_LIVE_BROWSER=1 to run real Chromium security tests",
    ),
]


class _HTTPServer:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.routes: dict[str, tuple[int, dict[str, str], str]] = {}
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        socket = self._server.sockets[0]
        return int(socket.getsockname()[1])

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def close(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            lines = request.decode("latin-1").split("\r\n")
            path = lines[0].split(" ", 2)[1]
            headers = {
                name.strip().lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for name, value in [line.split(":", 1)]
            }
            self.requests.append((path, headers))
            status, response_headers, body = self.routes.get(
                path,
                (200, {"Content-Type": "text/html"}, "<html>ok</html>"),
            )
            payload = body.encode()
            reason = {200: "OK", 204: "No Content", 302: "Found"}[status]
            rendered_headers = {
                "Content-Length": str(len(payload)),
                "Connection": "close",
                **response_headers,
            }
            writer.write(f"HTTP/1.1 {status} {reason}\r\n".encode())
            for name, value in rendered_headers.items():
                writer.write(f"{name}: {value}\r\n".encode())
            writer.write(b"\r\n" + payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()


async def _wait_for_path(server: _HTTPServer, path: str, timeout: float = 3) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not any(request_path == path for request_path, _headers in server.requests):
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail(f"Browser did not request {path}; observed {server.requests!r}")
        await asyncio.sleep(0.01)


def _headers_for(server: _HTTPServer, path: str) -> dict[str, str]:
    return next(headers for request_path, headers in server.requests if request_path == path)


@pytest.fixture
async def chromium() -> Browser:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            await browser.close()


@pytest.fixture
async def http_servers() -> tuple[_HTTPServer, _HTTPServer]:
    target = _HTTPServer()
    protected = _HTTPServer()
    await target.start()
    await protected.start()
    try:
        yield target, protected
    finally:
        await target.close()
        await protected.close()


def _allow_all_local_urls(*_args: object, **_kwargs: object) -> None:
    return None


async def _guarded_context(
    browser: Browser,
    *,
    target_url: str,
    block_resources: bool,
    validator: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
    extra_headers: dict[str, str] | None = None,
) -> tuple[object, _BrowserNetworkGuard]:
    monkeypatch.setattr("scrapeyard.engine.browser_debug.assert_public_url", validator)
    context = await browser.new_context(service_workers="block")
    guard = _BrowserNetworkGuard(
        block_resources=block_resources,
        require_resolved_dns=True,
        budget=None,
        blocked_requests=[],
        target_origin=canonical_url_origin(target_url),
        extra_headers=extra_headers or {},
    )
    await guard.install(context)
    return context, guard


@pytest.mark.asyncio
async def test_headers_are_scoped_across_subresources_redirects_and_popups(
    chromium: Browser,
    http_servers: tuple[_HTTPServer, _HTTPServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, other = http_servers
    target.routes["/page"] = (
        200,
        {"Content-Type": "text/html"},
        "<script>fetch('/subresource')</script>",
    )
    target.routes["/subresource"] = (204, {}, "")
    target.routes["/redirect"] = (
        302,
        {"Location": f"{other.origin}/redirect-target"},
        "",
    )
    target.routes["/popup"] = (
        200,
        {"Content-Type": "text/html"},
        f"<script>window.open('{other.origin}/popup-target')</script>",
    )
    context, _guard = await _guarded_context(
        chromium,
        target_url=f"{target.origin}/page",
        block_resources=False,
        validator=_allow_all_local_urls,
        monkeypatch=monkeypatch,
        extra_headers={"X-Target-Secret": "sentinel"},
    )
    try:
        page = await context.new_page()
        await page.goto(f"{target.origin}/page")
        await _wait_for_path(target, "/subresource")
        await page.goto(f"{target.origin}/redirect")
        await _wait_for_path(other, "/redirect-target")
        await page.goto(f"{target.origin}/popup")
        await _wait_for_path(other, "/popup-target")
    finally:
        await context.close()

    assert _headers_for(target, "/page")["x-target-secret"] == "sentinel"
    assert _headers_for(target, "/subresource")["x-target-secret"] == "sentinel"
    assert "x-target-secret" not in _headers_for(other, "/redirect-target")
    assert "x-target-secret" not in _headers_for(other, "/popup-target")


@pytest.mark.asyncio
async def test_private_popup_and_service_worker_fetch_cannot_reach_listener(
    chromium: Browser,
    http_servers: tuple[_HTTPServer, _HTTPServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, protected = http_servers
    target.routes["/security"] = (
        200,
        {"Content-Type": "text/html"},
        (
            f"<script>window.open('{protected.origin}/private-popup');"
            "navigator.serviceWorker.register('/worker.js').catch(() => {});</script>"
        ),
    )
    target.routes["/worker.js"] = (
        200,
        {"Content-Type": "application/javascript"},
        (
            "self.addEventListener('install', event => "
            f"event.waitUntil(fetch('{protected.origin}/service-worker')));"
        ),
    )

    def allow_only_target(url: str, **_kwargs: object) -> None:
        if urlsplit(url).port != target.port:
            raise UnsafeURLError("protected local destination")

    context, guard = await _guarded_context(
        chromium,
        target_url=f"{target.origin}/security",
        block_resources=False,
        validator=allow_only_target,
        monkeypatch=monkeypatch,
    )
    try:
        page = await context.new_page()
        await page.goto(f"{target.origin}/security")
        await page.wait_for_timeout(750)
    finally:
        await context.close()

    protected_paths = {path for path, _headers in protected.requests}
    assert "/private-popup" not in protected_paths
    assert "/service-worker" not in protected_paths
    assert any(
        entry["kind"] == "request" and "/private-popup" in entry["url"]
        for entry in guard.blocked_requests
    )


@pytest.mark.asyncio
async def test_disabled_websocket_never_reaches_listener(
    chromium: Browser,
    http_servers: tuple[_HTTPServer, _HTTPServer],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target, protected = http_servers
    target.routes["/socket-page"] = (
        200,
        {"Content-Type": "text/html"},
        f"<script>new WebSocket('ws://127.0.0.1:{protected.port}/socket')</script>",
    )
    context, guard = await _guarded_context(
        chromium,
        target_url=f"{target.origin}/socket-page",
        block_resources=True,
        validator=_allow_all_local_urls,
        monkeypatch=monkeypatch,
    )
    try:
        page = await context.new_page()
        await page.goto(f"{target.origin}/socket-page")
        await page.wait_for_timeout(500)
    finally:
        await context.close()

    assert not any(path == "/socket" for path, _headers in protected.requests)
    assert any(entry["kind"] == "websocket" for entry in guard.blocked_requests)
