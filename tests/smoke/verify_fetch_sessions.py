"""Real-browser session checks; use the benchmark container/network invocation.

Both session.public.test and session-other.public.test must resolve to this container.
The loopback destination is deliberately forbidden by the production URL guard.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from scrapling import PlayWrightFetcher, StealthyFetcher

from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.config.schema import RetryConfig, TargetConfig
from scrapeyard.engine import browser_debug
from scrapeyard.engine.browser_session import BrowserSession
from scrapeyard.engine.resilience import RetryHandler
from scrapeyard.engine.scraper import _fetch_target_page

ORIGIN = "http://session.public.test:8080"
OTHER = "http://session-other.public.test:8080"


class Fixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    requests = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        type(self).requests.append((path, dict(self.headers)))
        content = '<title>session fixture</title><h1 id="state"></h1>'
        content += "<script>document.querySelector('h1').textContent = localStorage.getItem('gate') || 'empty';"
        if path == "/first":
            content += "localStorage.setItem('gate', 'present');"
        if path == "/second":
            content += "fetch('http://127.0.0.1:8080/private').catch(() => {});"
        content += "</script>"
        payload = content.encode()
        self.send_response(200)
        if path == "/first":
            self.send_header("Set-Cookie", "gate=present; Path=/")
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def budget(duration=30):
    return RunBudget(
        max_duration_seconds=duration, max_fetched_bytes=1_000_000,
        max_extracted_records=100, max_serialized_result_bytes=100_000,
        max_browser_debug_bytes=100_000,
    )


async def verify(fetcher, fetcher_type, data_dir, *, stealth=False):
    Fixture.requests.clear()
    target = TargetConfig.model_validate({
        "url": ORIGIN + "/first", "fetcher": fetcher_type,
        "selectors": {"title": "h1"},
        "browser": {"extra_headers": {"X-Test-Secret": "target-only"}, "stealth": stealth},
    })
    session = BrowserSession(fetcher)
    budgets_seen = []
    guard = browser_debug._guarded_async_intercept_route

    async def observed_guard(route):
        budgets_seen.append((route.request.url, browser_debug._BROWSER_RUN_BUDGET.get()))
        await guard(route)

    async def fetch(url, run_budget):
        return await _fetch_target_page(
            RetryHandler(RetryConfig(max_attempts=2, backoff_max=0)),
            session, url, target, False, {503}, data_dir, None, None, run_budget,
        )

    try:
        with patch.object(browser_debug, "_guarded_async_intercept_route", observed_guard):
            first_budget, second_budget = budget(), budget()
            first = await fetch(ORIGIN + "/first", first_budget)
            assert first.page.css("h1::text").get() == "empty"
            context = session._context
            browser = context.browser
            second = await fetch(ORIGIN + "/second", second_budget)
            assert second.page.css("h1::text").get() == "present"
            assert session._context is context
            assert not context.pages  # No earlier page callbacks remain alive.
            assert "blocked_requests" not in first.debug
            assert second.debug["blocked_requests"] == [
                {"kind": "request", "url": "http://127.0.0.1:8080/private"},
            ]
            assert all(b is first_budget for url, b in budgets_seen if url.endswith("/first"))
            assert all(b is second_budget for url, b in budgets_seen if url.endswith(("/second", "/private")))
            other = await fetch(OTHER + "/other", budget())
            assert other.page.css("h1::text").get() == "empty"
            headers = {name.lower(): value for name, value in Fixture.requests[-1][1].items()}
            assert "x-test-secret" not in headers
            assert "cookie" not in headers
            assert all(path != "/private" for path, _ in Fixture.requests)
            assert any(headers.get("Cookie") == "gate=present" for path, headers in Fixture.requests if path == "/second")

            # A disconnected browser consumes retry policy, then rebuilds state.
            await browser.close()
            recovered = await fetch(ORIGIN + "/recovered", budget())
            assert recovered.page.css("h1::text").get() == "empty"
            assert session._context is not context
            await session.aclose()
            session = BrowserSession(fetcher)
            clean = await fetch(ORIGIN + "/clean", budget())
            assert clean.page.css("h1::text").get() == "empty"
            assert "Cookie" not in Fixture.requests[-1][1]

            # Deadline and cancellation must close the existing browser before returning.
            for ending in ("budget", "cancel"):
                await fetch(ORIGIN + "/first", budget())
                owned_browser = session._context.browser
                entered = asyncio.Event()

                async def blocked_action(*_args, entered=entered, **_kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                run_budget = budget(duration=2 if ending == "budget" else 30)
                with patch.object(browser_debug, "capture_browser_state", blocked_action):
                    task = asyncio.create_task(fetch(ORIGIN + "/wait", run_budget))
                    await asyncio.wait_for(entered.wait(), 10)
                    if ending == "cancel":
                        task.cancel()
                        expected = asyncio.CancelledError
                    else:
                        expected = BudgetExceeded
                    try:
                        await task
                        raise AssertionError("fetch did not stop")
                    except expected:
                        pass
                assert not owned_browser.is_connected()
                assert session._context is None

            # Interrupt startup after launch, before the adapter receives the
            # browser/context. The registered manager must still stop the driver.
            if stealth:
                from rebrowser_playwright.async_api import BrowserType
            else:
                from playwright.async_api import BrowserType
            launch = BrowserType.launch
            started = asyncio.Event()
            launched = []

            async def interrupted_launch(*args, **kwargs):
                launched.append(await launch(*args, **kwargs))
                started.set()
                await asyncio.Event().wait()

            with patch.object(BrowserType, "launch", interrupted_launch):
                task = asyncio.create_task(fetch(ORIGIN + "/first", budget()))
                await asyncio.wait_for(started.wait(), 15)
                task.cancel()
                try:
                    await task
                    raise AssertionError("startup did not stop")
                except asyncio.CancelledError:
                    pass
            # is_connected() can retain its last value when driver shutdown
            # precedes the browser's close event. Verify the actual driver exit.
            driver = launched[0]._impl_obj._connection._transport._proc
            assert driver.returncode is not None
            assert session._context is None
    finally:
        await session.aclose()


async def main():
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with TemporaryDirectory() as data_dir:
            for fetcher, name, stealth in (
                (PlayWrightFetcher, "dynamic", False),
                (PlayWrightFetcher, "dynamic", True),
                (StealthyFetcher, "stealthy", False),
            ):
                await verify(fetcher, name, data_dir, stealth=stealth)
                print(json.dumps({"fetcher": name, "stealth": stealth, "session_checks": "passed"}), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    asyncio.run(main())
