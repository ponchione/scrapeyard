"""Real-browser fixture runs: per-host traffic report and subrequest blocking.

A local HTTP server answers for every ``*.test`` host; Chromium resolves those
names to it, so first- and third-party requests are observable without network
access. Skipped when Playwright's Chromium build is not installed.
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import playwright
import pytest
from scrapling.engines.pw import PlaywrightEngine

from scrapeyard.common.budgets import RunBudget
from scrapeyard.config.schema import RetryConfig, TargetConfig
from scrapeyard.engine.scraper import TargetStatus, scrape_target


def _chromium_installed() -> bool:
    try:
        manifest = Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
        browsers = json.loads(manifest.read_text())["browsers"]
        revision = next(b["revision"] for b in browsers if b["name"] == "chromium-headless-shell")
    except (OSError, KeyError, StopIteration, ValueError):
        return False
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    return (root / f"chromium_headless_shell-{revision}").is_dir()


pytestmark = pytest.mark.skipif(not _chromium_installed(), reason="Playwright Chromium not installed")

PRODUCTS = 20
_CARDS = "".join(
    f'<li class="product"><span class="name">Item {n}</span><span class="price">${n}.99</span></li>'
    for n in range(1, PRODUCTS + 1)
)
# Synchronous XHR keeps each beacon inside page load, so request counts are exact.
_BEACON = (
    "try {{ const x = new XMLHttpRequest(); x.open('GET', '{url}', false); x.send(); }}"
    " catch (e) {{}}"
)
PAGES: dict[tuple[str, str], tuple[str, str]] = {
    ("www.example.test", "/catalog"): ("text/html", (
        "<!doctype html><html><head><title>Catalog</title>"
        '<link rel="stylesheet" href="/style.css">'
        '<script src="/app.js"></script>'
        '<script src="http://tags.example-ads.test/tag.js"></script>'
        f'</head><body><ul id="grid">{_CARDS}</ul>'
        '<iframe src="http://widgets.example-ads.test/frame.html"></iframe>'
        "</body></html>"
    )),
    ("www.example.test", "/style.css"): ("text/css", "li { color: black; }"),
    ("www.example.test", "/app.js"): ("text/javascript", _BEACON.format(url="/api/beacon?e=view")),
    ("www.example.test", "/api/beacon"): ("application/json", "{}"),
    ("tags.example-ads.test", "/tag.js"): (
        "text/javascript", _BEACON.format(url="http://collect.example-metrics.test/b?e=1"),
    ),
    ("collect.example-metrics.test", "/b"): ("application/json", "{}"),
    ("widgets.example-ads.test", "/frame.html"): ("text/html", "<html><body>ad</body></html>"),
    # Cards rendered by a script from a third-party CDN.
    ("www.example.test", "/cdn-catalog"): ("text/html", (
        "<!doctype html><html><head><title>Catalog</title>"
        '<script src="http://tags.example-ads.test/tag.js"></script>'
        '</head><body><ul id="grid"></ul>'
        '<script src="http://static.example-cdn.test/grid.js"></script>'
        "</body></html>"
    )),
    ("static.example-cdn.test", "/grid.js"): (
        "text/javascript", f"document.getElementById('grid').innerHTML = {json.dumps(_CARDS)};",
    ),
}
THIRD_PARTY = ("tags.example-ads.test", "collect.example-metrics.test", "widgets.example-ads.test")


class FixtureSite:
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return None

            def do_GET(self) -> None:
                host = (self.headers.get("Host") or "").split(":")[0]
                path = self.path.split("?")[0]
                site.hits[host] += 1
                content_type, body = PAGES.get((host, path), ("text/plain", ""))
                payload = body.encode()
                self.send_response(200 if (host, path) in PAGES else 404)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture()
def fixture_site(monkeypatch: pytest.MonkeyPatch) -> Iterator[FixtureSite]:
    site = FixtureSite()
    launch_kwargs = PlaywrightEngine._PlaywrightEngine__launch_kwargs  # type: ignore[attr-defined]

    def mapped_launch_kwargs(engine: Any) -> dict[str, Any]:
        kwargs = launch_kwargs(engine)
        rule = f"--host-resolver-rules=MAP *.test 127.0.0.1:{site.port}"
        return {**kwargs, "args": [*kwargs.get("args", []), rule]}

    monkeypatch.setattr(PlaywrightEngine, "_PlaywrightEngine__launch_kwargs", mapped_launch_kwargs)
    try:
        yield site
    finally:
        site.close()


async def scrape_fixture(
    tmp_path: Path, path: str = "/catalog", **browser: Any,
) -> tuple[Any, dict[str, Any], RunBudget]:
    target = TargetConfig.model_validate({
        "url": f"http://www.example.test{path}",
        "fetcher": "dynamic",
        "browser": {"timeout_ms": 20_000, **browser},
        "item_selector": "li.product",
        "selectors": {"name": ".name::text", "price": ".price::text"},
    })
    budget = RunBudget(
        max_duration_seconds=45,
        max_fetched_bytes=10_000_000,
        max_extracted_records=1000,
        max_serialized_result_bytes=1_000_000,
        max_browser_debug_bytes=1_000_000,
    )
    result = await scrape_target(
        target, adaptive=False, retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path), budget=budget,
    )
    assert result.status is TargetStatus.success, result.errors
    return result, budget.snapshot()["traffic"], budget


async def test_browser_run_reports_requests_and_bytes_per_host(fixture_site, tmp_path) -> None:
    result, traffic, budget = await scrape_fixture(tmp_path)

    assert len(result.data) == PRODUCTS
    hosts = {row["host"]: row for row in traffic["hosts"]}
    assert set(hosts) == {
        "www.example.test", "tags.example-ads.test",
        "collect.example-metrics.test", "widgets.example-ads.test",
    }
    first = hosts["www.example.test"]
    # Catalog, app.js and its beacon are sent; the stylesheet is blocked.
    assert (first["third_party"], first["requests"], first["blocked"]) == (False, 3, 1)
    assert first["bytes"] > len(PAGES["www.example.test", "/catalog"][1])
    for host in THIRD_PARTY:
        assert hosts[host]["third_party"] is True
        assert hosts[host]["requests"] == fixture_site.hits[host] == 1
        assert hosts[host]["bytes"] > 0
    assert traffic["third_party"]["requests"] == 3
    assert traffic["requests"] == budget.requests == sum(fixture_site.hits.values())


async def test_blocking_third_party_requests_leaves_extraction_unchanged(
    fixture_site, tmp_path,
) -> None:
    baseline, _, _ = await scrape_fixture(tmp_path)
    fixture_site.hits.clear()

    blocked, traffic, _ = await scrape_fixture(tmp_path, block_third_party=True)

    assert blocked.data == baseline.data and len(blocked.data) == PRODUCTS
    assert traffic["third_party"]["requests"] == 0
    assert all(fixture_site.hits[host] == 0 for host in THIRD_PARTY)
    hosts = {row["host"]: row for row in traffic["hosts"]}
    # The tag script and the iframe are aborted; the tag's beacon is never issued.
    assert hosts["tags.example-ads.test"]["blocked"] == 1
    assert hosts["widgets.example-ads.test"]["blocked"] == 1
    assert "collect.example-metrics.test" not in hosts
    assert hosts["www.example.test"]["requests"] == 3


@pytest.mark.parametrize("allow", [True, False])
async def test_allow_listed_cdn_still_renders_the_items(fixture_site, tmp_path, allow: bool) -> None:
    browser: dict[str, Any] = {"block_third_party": True}
    if allow:
        browser["third_party_allow_hosts"] = ["example-cdn.test"]

    result, traffic, _ = await scrape_fixture(tmp_path, "/cdn-catalog", **browser)

    assert len(result.data) == (PRODUCTS if allow else 0)
    assert fixture_site.hits["static.example-cdn.test"] == int(allow)
    assert fixture_site.hits["tags.example-ads.test"] == 0
    assert traffic["third_party"]["requests"] == int(allow)


async def test_url_patterns_block_matching_first_party_requests(fixture_site, tmp_path) -> None:
    result, traffic, _ = await scrape_fixture(tmp_path, block_url_patterns=["*/api/beacon?*"])

    assert len(result.data) == PRODUCTS
    first = next(row for row in traffic["hosts"] if row["host"] == "www.example.test")
    # The catalog and app.js are sent; the stylesheet and the beacon are blocked.
    assert (first["requests"], first["blocked"]) == (2, 2)
    assert fixture_site.hits["collect.example-metrics.test"] == 1
