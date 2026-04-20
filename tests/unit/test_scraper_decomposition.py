from __future__ import annotations

from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.engine.adaptive_diagnostics import missing_adaptive_selectors
from scrapeyard.engine.browser_debug import browser_fetch_kwargs, default_debug_blob, response_title


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
        "useragent": None,
        "extra_headers": {},
        "click_selector": None,
        "click_timeout_ms": 3000,
        "click_wait_ms": None,
        "wait_for_selector": None,
        "wait_ms": None,
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
        "extra_headers": {"X-Test": "1"},
        "wait_selector": ".product-card",
        "wait": 1200,
        "proxy": "http://proxy.local:8080",
    }


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
