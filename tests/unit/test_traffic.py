from __future__ import annotations

import gzip
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from scrapling import PlayWrightFetcher

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.traffic import HostTraffic, registrable_domain, url_hostname, url_site
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.basic_fetch import RECEIVED_BYTES_ATTRIBUTE
from scrapeyard.engine.browser_session import BrowserSession
from scrapeyard.engine.scraper import scrape_target
from scrapeyard.engine.url_guard import ResolvedPublicURL
from tests.factories import make_budget, make_html_response


def _budget() -> RunBudget:
    return make_budget(
        max_duration_seconds=60,
        max_fetched_bytes=1_000_000,
        max_extracted_records=100,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1000,
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.example.test", "example.test"),
        ("static.cdn.example-cdn.test", "example-cdn.test"),
        ("shop.example.co.uk", "example.co.uk"),
        ("WWW.Example.COM.", "example.com"),
        ("203.0.113.7", "203.0.113.7"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_domain_uses_the_bundled_suffix_list(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def _counts(
    requests: int = 0, blocked: int = 0, bytes: int = 0, cached: int = 0,
) -> dict[str, int]:
    return {"requests": requests, "blocked": blocked, "cached": cached, "bytes": bytes}


def test_a_unicode_site_is_first_party_however_its_requests_are_recorded() -> None:
    # A basic fetch records the host as configured; Chromium reports xn--.
    traffic = HostTraffic()
    site = url_site("https://www.bücher.de/katalog")
    traffic.request("https://www.bücher.de/katalog", site, "document")
    traffic.request("https://cdn.xn--bcher-kva.de/app.js", site, "script")
    traffic.request("https://cdn.xn--bcher-kva.com/app.js", site, "script")

    report = traffic.snapshot()

    assert report["first_party"]["hosts"] == 2
    assert [host["host"] for host in report["hosts"] if host["third_party"]] == [
        "cdn.xn--bcher-kva.com"
    ]


def test_report_keeps_hosts_only_and_splits_first_and_third_party() -> None:
    traffic = HostTraffic()
    site = url_site("https://www.example.test/catalog?page=2")
    traffic.request("https://www.example.test/catalog?page=2&token=abc", site, "document")
    traffic.request("https://img.example.test/a.js", site, "script")
    traffic.received("https://www.example.test/catalog?page=2", site, 1200, "document")
    traffic.request("https://tags.example-ads.test/t.js?id=secret", site, "script")
    traffic.request("https://tags.example-ads.test/t.js?id=other", site, "script")
    traffic.blocked("https://collect.example-metrics.test/b?u=1", site, "ping")
    traffic.received("https://tags.example-ads.test/t.js", site, 300, "script")

    report = traffic.snapshot()

    assert report["requests"] == 4
    assert report["blocked"] == 1
    assert report["bytes"] == 1500
    assert report["first_party"] == {"hosts": 2, **_counts(2, 0, 1200)}
    assert report["third_party"] == {"hosts": 2, **_counts(2, 1, 300)}
    assert report["hosts"] == [
        {"host": "tags.example-ads.test", "third_party": True, **_counts(2, 0, 300),
         "resource_types": {"script": _counts(2, 0, 300)}},
        {"host": "www.example.test", "third_party": False, **_counts(1, 0, 1200),
         "resource_types": {"document": _counts(1, 0, 1200)}},
        {"host": "img.example.test", "third_party": False, **_counts(1),
         "resource_types": {"script": _counts(1)}},
        {"host": "collect.example-metrics.test", "third_party": True, **_counts(0, 1),
         "resource_types": {"other": _counts(0, 1)}},
    ]
    assert report["hosts_omitted"] == 0
    assert "?" not in repr(report) and "/" not in repr(report)


def test_report_totals_every_resource_type_and_pools_the_rest_as_other() -> None:
    traffic = HostTraffic()
    for resource_type in ("document", "script", "script", "xhr", "fetch", "stylesheet", "image"):
        traffic.request("https://www.example.test/x", "example.test", resource_type)
    traffic.blocked("https://www.example.test/p.png", "example.test", "image")
    traffic.received("https://www.example.test/app.js", "example.test", 500, "script")
    traffic.cached("https://www.example.test/app.js", "example.test", "script")
    traffic.request("https://www.example.test/unknown", "example.test")

    report = traffic.snapshot()

    assert (report["requests"], report["cached"]) == (8, 1)
    assert report["first_party"]["cached"] == 1
    assert report["resource_types"] == {
        "document": _counts(1),
        "script": _counts(2, 0, 500, cached=1),
        "xhr": _counts(1),
        "fetch": _counts(1),
        "other": _counts(3, 1),
    }
    assert report["hosts"][0]["resource_types"] == report["resource_types"]
    assert sum(row["requests"] for row in report["resource_types"].values()) == report["requests"]


def test_report_is_bounded_but_totals_cover_every_host() -> None:
    traffic = HostTraffic(max_hosts=3)
    for index in range(5):
        traffic.request(f"https://h{index}.example-ads.test/", "example.test")
    traffic.request("https://late.example.test/", "example.test")

    full = traffic.snapshot(reported_hosts=2)

    assert full["requests"] == 6
    assert full["third_party"]["requests"] == 5
    assert full["first_party"]["requests"] == 1
    assert [row["host"] for row in full["hosts"]] == [
        "(other third-party hosts)",
        "(other first-party hosts)",
    ]
    assert full["hosts_omitted"] == 3


def test_a_host_first_party_for_any_target_stays_first_party() -> None:
    traffic = HostTraffic()
    traffic.request("https://cdn.example-cdn.test/x.js", "example.test")
    traffic.request("https://cdn.example-cdn.test/y.js", "example-cdn.test")

    assert traffic.snapshot()["hosts"][0]["third_party"] is False


def test_run_budget_snapshot_includes_the_traffic_report() -> None:
    budget = _budget()
    budget.traffic.request("https://www.example.test/", "example.test")

    assert budget.snapshot()["traffic"]["first_party"]["requests"] == 1


async def test_basic_redirects_are_counted_per_host(monkeypatch) -> None:
    # The streaming transport reports each response's received bytes.
    final = make_html_response("https://shop.example-mall.test/final", "<h1>ok</h1>")
    setattr(final, RECEIVED_BYTES_ATTRIBUTE, 5)
    responses = iter([
        SimpleNamespace(
            status=302,
            headers={"location": "https://shop.example-mall.test/final"},
            url="https://www.example.test/start",
            **{RECEIVED_BYTES_ATTRIBUTE: 3},
        ),
        final,
    ])

    def resolve(url: str) -> ResolvedPublicURL:
        host = url_hostname(url)
        assert host is not None
        return ResolvedPublicURL(url, host, host)

    monkeypatch.setattr(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(side_effect=lambda *_args, **_kwargs: next(responses)),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper.resolve_public_url", resolve)
    budget = _budget()
    target = TargetConfig(
        url="https://www.example.test/start",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )

    result = await scrape_target(target, False, RetryConfig(max_attempts=1), budget=budget)
    assert result.is_success

    hosts = {row["host"]: row for row in budget.snapshot()["traffic"]["hosts"]}
    assert hosts["www.example.test"] == {
        "host": "www.example.test", "third_party": False, **_counts(1, 0, 3),
        "resource_types": {"document": _counts(1, 0, 3)},
    }
    assert hosts["shop.example-mall.test"]["third_party"] is True
    assert hosts["shop.example-mall.test"]["bytes"] == 5
    assert budget.requests == 2


@pytest.fixture()
def gzip_server() -> Iterator[tuple[int, bytes]]:
    html = b"<html><body>" + b"<p>repeated product text</p>" * 400 + b"</body></html>"
    encoded = gzip.compress(html)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return None

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
    )
    thread.start()
    try:
        yield server.server_address[1], encoded
    finally:
        server.shutdown()
        server.server_close()


async def test_streamed_basic_fetch_reports_encoded_bytes_received(monkeypatch, gzip_server) -> None:
    port, encoded = gzip_server
    monkeypatch.setattr(
        "scrapeyard.engine.scraper.resolve_public_url",
        lambda _url: ResolvedPublicURL(
            f"http://127.0.0.1:{port}/catalog", "www.example.test", "www.example.test",
        ),
    )
    budget = _budget()
    target = TargetConfig(
        url="http://www.example.test/catalog",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    result = await scrape_target(target, False, RetryConfig(max_attempts=1), budget=budget)
    assert result.is_success

    report = budget.snapshot()["traffic"]
    assert report["hosts"] == [{
        "host": "www.example.test", "third_party": False, **_counts(1, 0, len(encoded)),
        "resource_types": {"document": _counts(1, 0, len(encoded))},
    }]
    assert budget.fetched_bytes > len(encoded)


@pytest.mark.parametrize(
    ("status", "method", "headers", "expected_body"),
    [
        (200, "GET", {"content-type": "text/html", "content-length": "1200"}, 1200),
        (304, "GET", {"content-length": "1200"}, 0),
        (200, "HEAD", {"content-length": "1200"}, 0),
        (200, "GET", {"content-type": "text/html", "transfer-encoding": "chunked"}, None),
    ],
)
async def test_browser_response_bytes_come_from_headers_when_declared(
    status: int, method: str, headers: dict[str, str], expected_body: int | None,
) -> None:
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
    budget = _budget()

    request = MagicMock(
        url="https://www.example.test/asset",
        method=method,
        resource_type="script",
        redirected_from=None,
    )
    request.sizes = AsyncMock(
        return_value={"responseHeadersSize": 50, "responseBodySize": 777}
    )
    response = SimpleNamespace(
        status=status,
        url="https://www.example.test/asset",
        headers=headers,
        request=request,
    )

    async def navigate(*_args, **_kwargs):
        import asyncio

        handlers = {call.args[0]: call.args[1] for call in context.on.call_args_list}
        handlers["response"](response)
        handlers["requestfinished"](request)
        await asyncio.sleep(0)
        raise RuntimeError("stop after observing response")

    page.goto = AsyncMock(side_effect=navigate)
    with pytest.raises(RuntimeError, match="stop after observing response"):
        await session.fetch("https://www.example.test/", {}, AsyncMock(), budget=budget)

    header_bytes = 19 + sum(len(name) + len(value) + 4 for name, value in headers.items())
    if expected_body is None:
        request.sizes.assert_awaited_once()
        assert budget.snapshot()["traffic"]["hosts"][0]["bytes"] == 827
    else:
        request.sizes.assert_not_awaited()
        assert budget.snapshot()["traffic"]["hosts"][0]["bytes"] == header_bytes + expected_body
