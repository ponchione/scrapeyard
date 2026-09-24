"""Synthetic browser traffic contracts. Run in a network-none browser container."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from scrapling import PlayWrightFetcher, StealthyFetcher

from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.engine.browser_session import BrowserSession
from scrapeyard.engine.browser_debug import _capture_html_excerpt


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests: list[str] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        self.requests.append(path)
        destination = None
        if path.startswith("/loop/"):
            destination = f"/loop/{int(path.rsplit('/', 1)[1]) + 1}"
        elif path in ("/start", "/redirect"):
            destination = "/redirect" if path == "/start" else "/landing"
        if destination:
            self.send_response(302)
            self.send_header("Location", destination)
            body = b""
        else:
            self.send_response(200)
            body = b"<title>synthetic</title>ok"
            if path == "/large":
                body = ("<!doctype html><title>synthetic</title><p>" + "é🚀" * 200_000).encode()
            if path == "/landing":
                body = b"""<title>synthetic</title><div id="attempts"></div>
<button id="popup" onclick="window.open('/popup')">popup</button>
<script>
const attempts = document.querySelector('#attempts');
attempts.dataset.socket = 'attempted';
try { new WebSocket('ws://127.0.0.1:8080/socket'); } catch (_) {}
attempts.dataset.worker = 'attempted';
try { new Worker('/dedicated.js'); } catch (_) {}
attempts.dataset.service = 'attempted';
try { navigator.serviceWorker.register('/service.js').catch(() => {}); } catch (_) {}
fetch('/data');
</script>"""
            if path == "/cancel":
                body = b"""<title>synthetic cancellation</title><script>
setTimeout(() => fetch('/late'), 500);
addEventListener('pagehide', () => fetch('/unload', {keepalive: true}));
</script>"""
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def budget(max_requests: int) -> RunBudget:
    return RunBudget(max_duration_seconds=20, max_requests=max_requests,
                     max_fetched_bytes=1_000_000, max_extracted_records=100,
                     max_serialized_result_bytes=100_000, max_browser_debug_bytes=100_000)


async def verify(fetcher, name: str, stealth: bool, case: str) -> None:
    Fixture.requests.clear()
    session = BrowserSession(fetcher)
    run_budget = budget(21 if case == "capacity" else 1000)
    routed = []
    attempts = []
    owned_browser = None

    async def route(request):
        nonlocal owned_browser
        owned_browser = session._context.browser
        routed.append(urlsplit(request.request.url).path)
        await request.continue_()

    async def action(page):
        for key in ("socket", "worker", "service"):
            attempts.append(await page.locator("#attempts").get_attribute(f"data-{key}"))
        await page.locator("#popup").click()
        await page.wait_for_timeout(200)
        return page

    options = {"headless": True, "timeout": 5000, "disable_resources": False,
               "google_search": False, "wait": 0, "page_action": action}
    if fetcher is PlayWrightFetcher:
        options["stealth"] = stealth
    error = None
    result = None
    excerpt = {}
    entered = asyncio.Event()
    if case == "cancel":
        async def wait_for_cancel(page):
            entered.set()
            await asyncio.Event().wait()
        options["page_action"] = wait_for_cancel
    if case == "bytes":
        async def capture_excerpt(page):
            await _capture_html_excerpt(page, excerpt, run_budget)
            return page
        options["page_action"] = capture_excerpt
    try:
        try:
            task = asyncio.create_task(run_budget.wait_for_owned(session.fetch(
                "http://127.0.0.1:8080/" + ({"redirects": "loop/0", "bytes": "large", "cancel": "cancel"}.get(case, "start")),
                options, route, budget=run_budget,
            )))
            if case == "cancel":
                await asyncio.wait_for(entered.wait(), 10)
                task.cancel()
            result = await task
        except (Exception, asyncio.CancelledError) as exc:
            error = exc
        if case == "ordinary":
            assert error is None, repr(error)
            assert attempts == ["attempted"] * 3
            assert "/popup" in Fixture.requests and "/popup" in routed
            assert not {"/socket", "/dedicated.js", "/service.js"}.intersection(Fixture.requests)
        elif case == "bytes":
            assert isinstance(error, BudgetExceeded), repr(error)
            assert error.limit_name.value == "fetched_bytes"
            assert error.observed_amount > run_budget.max_fetched_bytes
            assert result is None
            assert 0 < len(excerpt["html_excerpt"]) <= 2000
        elif case == "capacity":
            assert isinstance(error, BudgetExceeded), repr(error)
            assert error.limit_name.value == "requests"
            assert len(Fixture.requests) <= 21
        elif case == "cancel":
            assert isinstance(error, asyncio.CancelledError), repr(error)
        else:
            assert error is not None, "native redirect loop did not stop"
            assert 1 < len(Fixture.requests) <= 21, Fixture.requests
        assert run_budget.requests == len(Fixture.requests), (run_budget.requests, Fixture.requests)
        if session._context is None:
            assert owned_browser._impl_obj._connection._transport._proc.returncode is not None
        else:
            assert not session._context.pages
        await session.aclose()
        await asyncio.sleep(0.6 if case == "cancel" else 0.1)
        assert run_budget.requests == len(Fixture.requests), (run_budget.requests, Fixture.requests)
        assert not {"/late", "/unload"}.intersection(Fixture.requests)
        print(json.dumps({"mode": name, "case": case, "requests": run_budget.requests,
                          "server_requests": Fixture.requests, "routed": routed,
                          "error": type(error).__name__ if error else None, "passed": True}), flush=True)
    finally:
        await session.aclose()


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8080), Fixture)
    server.handle_error = lambda *_args: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for fetcher, name, stealth in ((PlayWrightFetcher, "dynamic", False),
                                        (PlayWrightFetcher, "dynamic-stealth", True),
                                        (StealthyFetcher, "stealthy", False)):
            for case in ("ordinary", "capacity", "redirects", "bytes", "cancel"):
                await verify(fetcher, name, stealth, case)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    asyncio.run(main())
