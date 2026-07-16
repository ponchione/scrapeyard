from __future__ import annotations

import logging
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from urllib.parse import urlparse

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapling import Fetcher

from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.adaptive_diagnostics import (
    log_adaptive_selector_gap,
    missing_adaptive_selectors,
)
from scrapeyard.engine.browser_debug import browser_fetch_kwargs, default_debug_blob, response_title
from scrapeyard.engine.basic_fetch import _request_headers
from scrapeyard.engine.resilience import RetryHandler
from scrapeyard.engine.scraper import (
    _fetch_basic_with_safe_redirects,
    _fetch_page,
    _fetch_target_page,
)
from scrapeyard.engine.url_guard import URLResolutionError, UnsafeURLError


@pytest.mark.asyncio
async def test_fetch_page_passes_explicit_timeout_to_basic_fetcher(monkeypatch):
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    captured_kwargs: dict[str, object] = {}

    async def _fetch_basic_response(_fetcher_cls, url, call_kwargs):
        captured_kwargs.update(call_kwargs)
        return SimpleNamespace(status=200, url=url, text="<h1>ok</h1>")

    monkeypatch.setattr(
        "scrapeyard.engine.scraper.get_settings",
        lambda: SimpleNamespace(basic_fetch_timeout_seconds=12.5),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _fetch_basic_response)

    await _fetch_page(
        object(),
        target.url,
        target,
        FetcherType.basic,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
    )

    assert captured_kwargs["timeout"] == 12.5
    assert captured_kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_fetch_page_records_basic_final_url_from_response(monkeypatch):
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )

    async def _fetch_basic_response(_fetcher_cls, url, _call_kwargs):
        return SimpleNamespace(status=200, url=f"{url}/canonical", text="<h1>ok</h1>")

    monkeypatch.setattr(
        "scrapeyard.engine.scraper.get_settings",
        lambda: SimpleNamespace(basic_fetch_timeout_seconds=12.5),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _fetch_basic_response)

    outcome = await _fetch_page(
        object(),
        target.url,
        target,
        FetcherType.basic,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
    )

    assert outcome.debug["final_url"] == "https://example.com/canonical"


@pytest.mark.asyncio
async def test_fetch_page_follows_safe_basic_redirects(monkeypatch):
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    seen_urls: list[str] = []

    async def _fetch_basic_response(_fetcher_cls, url, _call_kwargs):
        seen_urls.append(url)
        if len(seen_urls) == 1:
            return SimpleNamespace(
                status=302,
                url=url,
                headers={"Location": "/next"},
                text="",
            )
        return SimpleNamespace(status=200, url=url, headers={}, text="<h1>ok</h1>")

    monkeypatch.setattr(
        "scrapeyard.engine.scraper.get_settings",
        lambda: SimpleNamespace(basic_fetch_timeout_seconds=12.5),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _fetch_basic_response)

    outcome = await _fetch_page(
        object(),
        target.url,
        target,
        FetcherType.basic,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
    )

    assert seen_urls == ["https://example.com", "https://example.com/next"]
    assert outcome.debug["redirects"] == ["https://example.com/next"]
    assert outcome.debug["final_url"] == "https://example.com/next"


@pytest.mark.asyncio
async def test_basic_redirect_hops_rate_limit_actual_destination_hosts(monkeypatch):
    responses = iter(
        [
            SimpleNamespace(
                status=302,
                url="https://example.com/start",
                headers={"Location": "https://other.example/next"},
            ),
            SimpleNamespace(
                status=200,
                url="https://other.example/next",
                headers={},
            ),
        ]
    )
    limiter = AsyncMock()
    monkeypatch.setattr(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(side_effect=lambda *_args, **_kwargs: next(responses)),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())

    await _fetch_basic_with_safe_redirects(
        object(),
        "https://example.com/start",
        {},
        {},
        rate_limiter=limiter,
        domain_rate_limit=7,
    )

    assert limiter.acquire.await_args_list == [
        call("example.com", 7),
        call("other.example", 7),
    ]


@pytest.mark.asyncio
async def test_basic_redirect_validates_each_requested_url_once(monkeypatch):
    responses = iter(
        [
            SimpleNamespace(
                status=302,
                url="https://example.com/start",
                headers={"Location": "/next"},
            ),
            SimpleNamespace(
                status=200,
                url="https://example.com/next",
                headers={},
            ),
        ]
    )

    async def fetch_response(*_args, **_kwargs):
        return next(responses)

    validate = AsyncMock()
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", fetch_response)
    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", validate)

    await _fetch_basic_with_safe_redirects(
        object(),
        "https://example.com/start",
        {},
        {},
    )

    assert [call.args[0] for call in validate.await_args_list] == [
        "https://example.com/start",
        "https://example.com/next",
    ]


@pytest.mark.asyncio
async def test_production_basic_fetch_pins_connection_to_validated_ip(monkeypatch):
    target = TargetConfig(
        url="https://example.com/products",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    fetch = AsyncMock(
        return_value=SimpleNamespace(
            status=200,
            url="https://93.184.216.34/products",
            headers={},
            text="<h1>ok</h1>",
        )
    )
    monkeypatch.setattr(
        "scrapeyard.engine.scraper.resolve_public_url",
        lambda _url: SimpleNamespace(
            connect_url="https://93.184.216.34/products",
            host_header="example.com",
            sni_hostname="example.com",
        ),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", fetch)

    outcome = await _fetch_page(
        Fetcher,
        target.url,
        target,
        FetcherType.basic,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
    )

    assert fetch.await_args.args[1] == "https://93.184.216.34/products"
    assert fetch.await_args.args[2]["headers"]["Host"] == "example.com"
    assert fetch.await_args.args[2]["extensions"]["sni_hostname"] == "example.com"
    assert fetch.await_args.args[2]["header_url"] == target.url
    assert outcome.debug["final_url"] == target.url


@pytest.mark.asyncio
async def test_production_basic_redirects_generate_headers_from_each_logical_url(monkeypatch):
    logical_urls = [
        "https://shop.acme.com/start",
        "https://catalog.widgets.org/products",
    ]
    resolved_urls = [
        "https://93.184.216.34/start",
        "https://93.184.216.35/products",
    ]
    responses = iter(
        [
            SimpleNamespace(
                status=302,
                url=resolved_urls[0],
                headers={"Location": logical_urls[1]},
            ),
            SimpleNamespace(status=200, url=resolved_urls[1], headers={}),
        ]
    )
    requests: list[tuple[str, str, str, str, str]] = []

    def resolve(url: str) -> SimpleNamespace:
        index = logical_urls.index(url)
        host = urlparse(url).hostname
        return SimpleNamespace(
            connect_url=resolved_urls[index],
            host_header=host,
            sni_hostname=host,
        )

    async def fetch(_fetcher_cls, url, kwargs, **_options):
        headers = _request_headers(
            kwargs["header_url"],
            kwargs["headers"],
            stealthy=True,
        )
        requests.append(
            (
                url,
                kwargs["header_url"],
                headers["referer"],
                headers["Host"],
                kwargs["extensions"]["sni_hostname"],
            )
        )
        return next(responses)

    monkeypatch.setattr("scrapeyard.engine.scraper.resolve_public_url", resolve)
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", fetch)

    await _fetch_basic_with_safe_redirects(Fetcher, logical_urls[0], {}, {})

    assert requests == [
        (
            resolved_urls[0],
            logical_urls[0],
            "https://www.google.com/search?q=acme",
            "shop.acme.com",
            "shop.acme.com",
        ),
        (
            resolved_urls[1],
            logical_urls[1],
            "https://www.google.com/search?q=widgets",
            "catalog.widgets.org",
            "catalog.widgets.org",
        ),
    ]


def test_generated_basic_headers_preserve_explicit_referer() -> None:
    headers = _request_headers(
        "https://shop.example/products",
        {"Referer": "https://caller.example/source"},
        stealthy=True,
    )

    assert headers["Referer"] == "https://caller.example/source"
    assert "referer" not in headers


@pytest.mark.asyncio
async def test_fetch_page_blocks_basic_redirects_to_non_public_destinations(monkeypatch):
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )

    async def _fetch_basic_response(_fetcher_cls, url, _call_kwargs):
        return SimpleNamespace(
            status=302,
            url=url,
            headers={"Location": "http://127.0.0.1/private"},
            text="",
        )

    monkeypatch.setattr(
        "scrapeyard.engine.scraper.get_settings",
        lambda: SimpleNamespace(basic_fetch_timeout_seconds=12.5),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _fetch_basic_response)

    with pytest.raises(UnsafeURLError, match="non-public"):
        await _fetch_page(
            object(),
            target.url,
            target,
            FetcherType.basic,
            adaptive=False,
            retryable_status={500},
            adaptive_dir="/tmp/adaptive",
        )


@pytest.mark.asyncio
async def test_fetch_page_requires_resolved_dns_when_proxy_can_resolve_remotely(monkeypatch):
    target = TargetConfig(
        url="https://unresolved.example",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    fetch_called = False

    def _raise_gaierror(*_args, **_kwargs):
        raise socket.gaierror

    async def _fetch_basic_response(_fetcher_cls, _url, _call_kwargs):
        nonlocal fetch_called
        fetch_called = True
        return SimpleNamespace(status=200, url=target.url, text="<h1>ok</h1>")

    monkeypatch.setattr("scrapeyard.engine.url_guard.socket.getaddrinfo", _raise_gaierror)
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _fetch_basic_response)

    with pytest.raises(URLResolutionError, match="could not be resolved"):
        await _fetch_page(
            object(),
            target.url,
            target,
            FetcherType.basic,
            adaptive=False,
            retryable_status={500},
            adaptive_dir="/tmp/adaptive",
            proxy_url="http://proxy.example:8080",
        )

    assert fetch_called is False


@pytest.mark.asyncio
async def test_fetch_page_still_allows_unresolved_dns_for_direct_basic_fetch(monkeypatch):
    target = TargetConfig(
        url="https://unresolved.example",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )

    def _raise_gaierror(*_args, **_kwargs):
        raise socket.gaierror

    async def _fetch_basic_response(_fetcher_cls, url, _call_kwargs):
        return SimpleNamespace(status=200, url=url, text="<h1>ok</h1>")

    monkeypatch.setattr("scrapeyard.engine.url_guard.socket.getaddrinfo", _raise_gaierror)
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", _fetch_basic_response)

    outcome = await _fetch_page(
        object(),
        target.url,
        target,
        FetcherType.basic,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
    )

    assert outcome.debug["final_url"] == target.url


@pytest.mark.asyncio
async def test_browser_fetch_performs_one_navigation_without_http_preflight(monkeypatch):
    target = TargetConfig(
        url="https://example.com/products",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    browser_fetch = AsyncMock(
        return_value=(
            SimpleNamespace(status=200, url=target.url, text="<h1>ok</h1>"),
            {},
        )
    )
    basic_fetch = AsyncMock()
    responses_observed = 0

    def observe_response() -> None:
        nonlocal responses_observed
        responses_observed += 1

    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_browser_response", browser_fetch)
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_basic_response", basic_fetch)

    await _fetch_page(
        object(),
        target.url,
        target,
        FetcherType.dynamic,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        response_observer=observe_response,
    )

    browser_fetch.assert_awaited_once()
    assert browser_fetch.await_args.args[1] == target.url
    basic_fetch.assert_not_awaited()
    assert responses_observed == 1


@pytest.mark.asyncio
async def test_browser_navigation_rate_limits_immediately_before_fetch(monkeypatch):
    target = TargetConfig(
        url="https://example.com/products",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    events: list[str] = []
    limiter = AsyncMock()

    async def acquire(*_args):
        events.append("acquire")

    async def browser_fetch(*_args, **_kwargs):
        events.append("navigate")
        return SimpleNamespace(status=200, url=target.url), {}

    limiter.acquire.side_effect = acquire
    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())
    monkeypatch.setattr("scrapeyard.engine.scraper.fetch_browser_response", browser_fetch)

    await _fetch_page(
        object(),
        target.url,
        target,
        FetcherType.dynamic,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        rate_limiter=limiter,
        domain_rate_limit=4,
    )

    assert events == ["acquire", "navigate"]
    limiter.acquire.assert_awaited_once_with("example.com", 4)


@pytest.mark.asyncio
async def test_browser_navigation_timeout_retries_the_complete_adapter_boundary(monkeypatch):
    target = TargetConfig(
        url="https://example.com/products",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    limiter = AsyncMock()
    attempts = 0

    class TimeoutThenSuccessFetcher:
        @classmethod
        async def async_fetch(cls, url, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PlaywrightTimeoutError("page.goto timed out")
            return SimpleNamespace(status=200, url=url, text="<h1>ok</h1>")

    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())

    outcome = await _fetch_target_page(
        RetryHandler(RetryConfig(max_attempts=2, backoff_max=0)),
        TimeoutThenSuccessFetcher,
        target.url,
        target,
        adaptive=False,
        retryable_status={500},
        adaptive_dir="/tmp/adaptive",
        proxy_url=None,
        artifacts_dir=None,
        rate_limiter=limiter,
        domain_rate_limit=4,
    )

    assert outcome.page.status == 200
    assert attempts == 2
    assert limiter.acquire.await_args_list == [call("example.com", 4)] * 2


def test_browser_fetch_kwargs_uses_defaults_when_browser_config_missing():
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )

    kwargs = browser_fetch_kwargs(target, FetcherType.dynamic, proxy_url=None)

    assert kwargs == {
        "timeout": 60000,
        "disable_resources": True,
        "network_idle": False,
        "stealth": False,
        "hide_canvas": False,
        "real_chrome": False,
        "nstbrowser_mode": False,
    }


def test_default_debug_blob_uses_browser_config_defaults_when_missing():
    target = TargetConfig(
        url="https://example.com/products",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )

    debug = default_debug_blob(FetcherType.dynamic, target, target.url)

    assert debug["browser_settings"] == {
        "timeout_ms": 60000,
        "disable_resources": True,
        "network_idle": False,
        "stealth": False,
        "hide_canvas": False,
        "real_chrome": False,
        "cdp_url": None,
        "nstbrowser_mode": False,
        "humanize": None,
        "os_randomize": False,
        "geoip": False,
        "disable_ads": False,
        "additional_arguments": {},
        "useragent": None,
        "extra_headers": {},
        "click_selector": None,
        "click_timeout_ms": 3000,
        "click_wait_ms": None,
        "wait_for_selector": None,
        "wait_ms": None,
        "actions": [],
    }


def test_browser_fetch_kwargs_includes_optional_browser_overrides_and_proxy():
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        browser={
            "timeout_ms": 90000,
            "disable_resources": False,
            "network_idle": True,
            "stealth": True,
            "hide_canvas": True,
            "real_chrome": True,
            "cdp_url": "ws://browser.example/devtools/browser/abc",
            "nstbrowser_mode": True,
            "useragent": "ua-test",
            "extra_headers": {"X-Test": "1"},
            "wait_for_selector": ".product-card",
            "wait_ms": 1200,
        },
        selectors={"title": "h1"},
    )

    kwargs = browser_fetch_kwargs(target, FetcherType.dynamic, proxy_url="http://proxy.local:8080")

    assert kwargs == {
        "timeout": 90000,
        "disable_resources": False,
        "network_idle": True,
        "stealth": True,
        "hide_canvas": True,
        "real_chrome": True,
        "cdp_url": "ws://browser.example/devtools/browser/abc",
        "nstbrowser_mode": True,
        "useragent": "ua-test",
        "extra_headers": {"X-Test": "1"},
        "wait_selector": ".product-card",
        "wait": 1200,
        "proxy": "http://proxy.local:8080",
    }


def test_stealthy_browser_fetch_kwargs_includes_optional_stealthy_overrides_and_proxy():
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.stealthy,
        browser={
            "timeout_ms": 90000,
            "disable_resources": False,
            "network_idle": True,
            "stealth": True,
            "hide_canvas": True,
            "real_chrome": True,
            "cdp_url": "ws://browser.example/devtools/browser/abc",
            "nstbrowser_mode": True,
            "humanize": 1.25,
            "os_randomize": True,
            "geoip": True,
            "disable_ads": True,
            "additional_arguments": {"window": [1920, 1080]},
            "useragent": "ua-test",
            "extra_headers": {"X-Test": "1"},
            "wait_for_selector": ".product-card",
            "wait_ms": 1200,
        },
        selectors={"title": "h1"},
    )

    kwargs = browser_fetch_kwargs(target, FetcherType.stealthy, proxy_url="http://proxy.local:8080")

    assert kwargs == {
        "timeout": 90000,
        "disable_resources": False,
        "network_idle": True,
        "hide_canvas": True,
        "humanize": 1.25,
        "os_randomize": True,
        "geoip": True,
        "disable_ads": True,
        "additional_arguments": {"window": [1920, 1080]},
        "useragent": "ua-test",
        "extra_headers": {"X-Test": "1"},
        "wait_selector": ".product-card",
        "wait": 1200,
        "proxy": "http://proxy.local:8080",
    }


def test_stealthy_browser_fetch_kwargs_drop_unsupported_playwright_only_options():
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.stealthy,
        browser={
            "timeout_ms": 90000,
            "disable_resources": False,
            "network_idle": True,
            "stealth": True,
            "hide_canvas": True,
            "real_chrome": True,
            "cdp_url": "ws://browser.example/devtools/browser/abc",
            "nstbrowser_mode": True,
            "humanize": True,
            "os_randomize": False,
            "geoip": False,
            "disable_ads": False,
            "additional_arguments": {"window": [1920, 1080]},
            "useragent": "ua-test",
            "extra_headers": {"X-Test": "1"},
            "wait_for_selector": ".product-card",
            "wait_ms": 1200,
        },
        selectors={"title": "h1"},
    )

    kwargs = browser_fetch_kwargs(target, FetcherType.stealthy, proxy_url="http://proxy.local:8080")

    assert "stealth" not in kwargs
    assert kwargs["hide_canvas"] is True
    assert "real_chrome" not in kwargs
    assert "cdp_url" not in kwargs
    assert "nstbrowser_mode" not in kwargs
    assert kwargs["useragent"] == "ua-test"


def test_response_title_prefers_explicit_title_attribute():
    page = type("Page", (), {"title": "  Product title  "})()

    assert response_title(page) == "Product title"


def test_missing_adaptive_selectors_returns_all_selectors_when_no_rows_extracted():
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1", "price": ".price"},
    )

    missing = missing_adaptive_selectors(target, [])

    assert missing == ["title", "price"]


def test_missing_adaptive_selectors_ignores_present_values_and_flags_empty_ones():
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1", "price": ".price", "sku": ".sku"},
    )

    missing = missing_adaptive_selectors(
        target,
        [
            {"title": "Scope", "price": "", "sku": None},
            {"title": "Mount", "price": [], "sku": None},
        ],
    )

    assert missing == ["price", "sku"]


def test_log_adaptive_selector_gap_redacts_target_url_secrets(caplog):
    caplog.set_level(logging.INFO, logger="scrapeyard.engine.adaptive_diagnostics")
    target = TargetConfig(
        url="https://user:pass@example.com/products?api_key=secret&page=2",
        fetcher=FetcherType.basic,
        selectors={"title": "h1", "price": ".price"},
    )

    log_adaptive_selector_gap(target, [{"title": "Scope", "price": ""}])

    assert "user:pass" not in caplog.text
    assert "api_key=secret" not in caplog.text
    assert "https://example.com/products?api_key=<redacted>&page=<redacted>" in caplog.text
