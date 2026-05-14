from __future__ import annotations

from types import SimpleNamespace

import pytest

from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.engine.adaptive_diagnostics import missing_adaptive_selectors
from scrapeyard.engine.browser_debug import browser_fetch_kwargs, default_debug_blob, response_title
from scrapeyard.engine.scraper import _fetch_page
from scrapeyard.engine.url_guard import UnsafeURLError


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
            "additional_arguments": {"screen": {"max_width": 1920}},
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
        "humanize": 1.25,
        "os_randomize": True,
        "geoip": True,
        "disable_ads": True,
        "additional_arguments": {"screen": {"max_width": 1920}},
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
            "additional_arguments": {"screen": {"max_width": 1920}},
            "useragent": "ua-test",
            "extra_headers": {"X-Test": "1"},
            "wait_for_selector": ".product-card",
            "wait_ms": 1200,
        },
        selectors={"title": "h1"},
    )

    kwargs = browser_fetch_kwargs(target, FetcherType.stealthy, proxy_url="http://proxy.local:8080")

    assert "stealth" not in kwargs
    assert "hide_canvas" not in kwargs
    assert "real_chrome" not in kwargs
    assert "cdp_url" not in kwargs
    assert "nstbrowser_mode" not in kwargs
    assert "useragent" not in kwargs


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
