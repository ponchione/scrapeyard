from __future__ import annotations

import gzip
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.traffic import HostTraffic, registrable_domain, url_site
from scrapeyard.engine.basic_fetch import BasicSession
from scrapeyard.engine.scraper import _fetch_basic_with_safe_redirects
from scrapeyard.engine.url_guard import ResolvedPublicURL


def _budget() -> RunBudget:
    return RunBudget(
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


def test_report_keeps_hosts_only_and_splits_first_and_third_party() -> None:
    traffic = HostTraffic()
    site = url_site("https://www.example.test/catalog?page=2")
    traffic.request("https://www.example.test/catalog?page=2&token=abc", site)
    traffic.request("https://img.example.test/a.js", site)
    traffic.received("https://www.example.test/catalog?page=2", site, 1200)
    traffic.request("https://tags.example-ads.test/t.js?id=secret", site)
    traffic.request("https://tags.example-ads.test/t.js?id=other", site)
    traffic.blocked("https://collect.example-metrics.test/b?u=1", site)
    traffic.received("https://tags.example-ads.test/t.js", site, 300)

    report = traffic.snapshot()

    assert report["requests"] == 4
    assert report["blocked"] == 1
    assert report["bytes"] == 1500
    assert report["first_party"] == {"hosts": 2, "requests": 2, "blocked": 0, "bytes": 1200}
    assert report["third_party"] == {"hosts": 2, "requests": 2, "blocked": 1, "bytes": 300}
    assert report["hosts"] == [
        {"host": "tags.example-ads.test", "third_party": True, "requests": 2, "blocked": 0, "bytes": 300},
        {"host": "www.example.test", "third_party": False, "requests": 1, "blocked": 0, "bytes": 1200},
        {"host": "img.example.test", "third_party": False, "requests": 1, "blocked": 0, "bytes": 0},
        {"host": "collect.example-metrics.test", "third_party": True, "requests": 0, "blocked": 1, "bytes": 0},
    ]
    assert report["hosts_omitted"] == 0
    assert "?" not in repr(report) and "/" not in repr(report)


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
    responses = iter([
        SimpleNamespace(status=302, headers={"location": "https://shop.example-mall.test/final"},
                        body=b"abc", url="https://www.example.test/start"),
        SimpleNamespace(status=200, headers={}, body=b"defgh", url="https://shop.example-mall.test/final"),
    ])

    class Fetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            return next(responses)

    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())
    budget = _budget()

    await _fetch_basic_with_safe_redirects(
        Fetcher, "https://www.example.test/start", {}, {}, budget=budget, site="example.test",
    )

    hosts = {row["host"]: row for row in budget.snapshot()["traffic"]["hosts"]}
    assert hosts["www.example.test"] == {
        "host": "www.example.test", "third_party": False, "requests": 1, "blocked": 0, "bytes": 3,
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
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
    session = BasicSession()
    try:
        response = await _fetch_basic_with_safe_redirects(
            session, "http://www.example.test/catalog", {"timeout": 5}, {},
            budget=budget, site="example.test",
        )
    finally:
        await session.aclose()

    report = budget.snapshot()["traffic"]
    assert report["hosts"] == [{
        "host": "www.example.test", "third_party": False, "requests": 1, "blocked": 0,
        "bytes": len(encoded),
    }]
    assert budget.fetched_bytes == len(response.body) > len(encoded)


@pytest.mark.parametrize(
    ("status", "method", "headers", "expected_body"),
    [
        (200, "GET", {"content-type": "text/html", "content-length": "1200"}, 1200),
        (304, "GET", {"content-length": "1200"}, 0),
        (200, "HEAD", {"content-length": "1200"}, 0),
        (200, "GET", {"content-type": "text/html", "transfer-encoding": "chunked"}, None),
    ],
)
def test_browser_response_bytes_come_from_headers_when_declared(
    status: int, method: str, headers: dict[str, str], expected_body: int | None,
) -> None:
    from scrapeyard.engine.browser_session import _declared_response_bytes

    response = SimpleNamespace(status=status, headers=headers, request=SimpleNamespace(method=method))
    header_bytes = 19 + sum(len(name) + len(value) + 4 for name, value in headers.items())

    declared = _declared_response_bytes(response)

    assert declared == (None if expected_body is None else header_bytes + expected_body)
