"""Tests for scraper failure classification and observability metadata."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.scraper import FetchError, FetchOutcome, scrape_target
from scrapeyard.models.job import ErrorType


class _FakeNode:
    def __init__(
        self,
        text: str = "",
        css_map: dict[str, list] | None = None,
        css_errors: dict[str, Exception] | None = None,
    ):
        self.text = text
        self._css_map = css_map or {}
        self._css_errors = css_errors or {}
        self.attrib: dict[str, str] = {}

    def css(self, selector: str):
        if selector in self._css_errors:
            raise self._css_errors[selector]
        return self._css_map.get(selector, [])

    def xpath(self, selector: str):
        return self._css_map.get(selector, [])

    def get_all_text(self) -> str:
        return self.text


class _BlockingTextNode(_FakeNode):
    def get_all_text(self) -> str:
        time.sleep(0.2)
        return self.text


def _target(fetcher: FetcherType = FetcherType.basic, **overrides: Any) -> TargetConfig:
    base: dict[str, Any] = {
        "url": "https://example.com",
        "fetcher": fetcher,
        "selectors": {"title": "h1"},
    }
    base.update(overrides)
    return TargetConfig.model_validate(base)


@pytest.mark.asyncio
async def test_near_limit_detection_work_obeys_deadline_without_blocking_event_loop(
    monkeypatch,
    tmp_path,
):
    page = _BlockingTextNode(
        text="ordinary listing content" * 100_000,
        css_map={"h1": []},
    )

    async def _return_page(*_args, **_kwargs):
        return FetchOutcome(
            page=page,
            debug={"fetcher": "basic", "final_url": "https://example.com"},
        )

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _return_page)
    target = _target(
        map_detection={
            "text_patterns": [f"missing-pattern-{index}" for index in range(100)]
        }
    )
    budget = RunBudget(
        max_duration_seconds=0.02,
        max_fetched_bytes=10_000_000,
        max_extracted_records=100,
        max_serialized_result_bytes=10_000_000,
        max_browser_debug_bytes=1_000_000,
    )

    async def _event_loop_probe() -> float:
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(0.005)
        return asyncio.get_running_loop().time() - started

    probe = asyncio.create_task(_event_loop_probe())
    with pytest.raises(BudgetExceeded):
        await scrape_target(
            target,
            adaptive=False,
            retry=RetryConfig(max_attempts=1),
            adaptive_dir=str(tmp_path),
            budget=budget,
        )

    assert await probe < 0.08


@pytest.mark.asyncio
async def test_scrape_target_classifies_http_not_found(monkeypatch, tmp_path):
    async def _raise_fetch_error(*_args, **_kwargs):
        raise FetchError(404, debug={"page_title": "Not Found", "final_url": "https://example.com/missing"})

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _raise_fetch_error)

    result = await scrape_target(
        _target(FetcherType.basic),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "failed"
    assert result.error_type == ErrorType.http_not_found
    assert result.http_status == 404
    assert result.debug["page_title"] == "Not Found"


@pytest.mark.asyncio
async def test_scrape_target_classifies_navigation_timeout_for_browser_fetchers(monkeypatch, tmp_path):
    async def _raise_timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _raise_timeout)

    result = await scrape_target(
        _target(FetcherType.dynamic),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "failed"
    assert result.error_type == ErrorType.navigation_timeout
    assert result.http_status is None
    assert result.error_detail == "TimeoutError"


@pytest.mark.asyncio
async def test_scrape_target_classifies_browser_failures(monkeypatch, tmp_path):
    async def _raise_browser_error(*_args, **_kwargs):
        raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium")

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _raise_browser_error)

    result = await scrape_target(
        _target(FetcherType.dynamic),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "failed"
    assert result.error_type == ErrorType.browser_error
    assert result.http_status is None
    assert "Executable doesn't exist" in result.error_detail


@pytest.mark.asyncio
async def test_scrape_target_collects_debug_for_rendered_empty(monkeypatch, tmp_path):
    page = _FakeNode(
        text="<html><title>Category</title><main>No products yet</main></html>",
        css_map={".product-card": [], "h2": [], ".price": []},
    )
    page.status = 200
    page.url = "https://example.com/category"

    async def _return_page(*_args, **_kwargs):
        return FetchOutcome(
            page=page,
            debug={
                "fetcher": "dynamic",
                "final_url": "https://example.com/category",
                "page_title": "Category",
                "main_document_status": 200,
                "html_excerpt": "<main>No products yet</main>",
                "screenshot_path": None,
                "console_messages": [],
                "request_failures": [],
                "browser_settings": {"timeout_ms": 60000, "disable_resources": True, "network_idle": False},
            },
        )

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _return_page)

    result = await scrape_target(
        _target(
            FetcherType.dynamic,
            item_selector=".product-card",
            selectors={"title": "h2", "price": ".price"},
        ),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "success"
    assert result.data == []
    assert result.debug is not None
    assert result.debug["final_url"] == "https://example.com/category"
    assert result.debug["item_selector_count"] == 0
    assert result.debug["selector_counts"] == {"title": 0, "price": 0}
    assert result.debug["classification"] == ErrorType.rendered_empty.value


@pytest.mark.asyncio
async def test_scrape_target_classifies_akamai_interstitial_as_challenge_page(monkeypatch, tmp_path):
    page = _FakeNode(
        text="<html><title>Bass Pro Shops</title><main>Powered and protected by Akamai</main></html>",
        css_map={".product-card": [], "h2": [], ".price": []},
    )
    page.status = 200
    page.url = "https://example.com/category"

    async def _return_page(*_args, **_kwargs):
        return FetchOutcome(
            page=page,
            debug={
                "fetcher": "dynamic",
                "final_url": "https://example.com/category",
                "page_title": "Bass Pro Shops",
                "main_document_status": 200,
                "html_excerpt": "Powered and protected by Akamai akam-sw.js install script sec-if-cpt-container",
                "screenshot_path": None,
                "console_messages": [{"type": "info", "text": "akam-sw.js install script booting"}],
                "request_failures": [],
                "browser_settings": {"timeout_ms": 60000, "disable_resources": True, "network_idle": False},
            },
        )

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _return_page)

    result = await scrape_target(
        _target(
            FetcherType.dynamic,
            item_selector=".product-card",
            selectors={"title": "h2", "price": ".price"},
        ),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "success"
    assert result.data == []
    assert result.debug is not None
    assert result.debug["classification"] == ErrorType.challenge_page.value


@pytest.mark.asyncio
async def test_scrape_target_classifies_selector_miss_when_items_exist(monkeypatch, tmp_path):
    item = _FakeNode(text="product card", css_map={"h2": [], ".price": []})
    page = _FakeNode(
        text="<html><title>Category</title><main>Products</main></html>",
        css_map={".product-card": [item], "h2": [], ".price": []},
    )
    page.status = 200
    page.url = "https://example.com/category"

    async def _return_page(*_args, **_kwargs):
        return FetchOutcome(
            page=page,
            debug={
                "fetcher": "dynamic",
                "final_url": "https://example.com/category",
                "page_title": "Category",
                "main_document_status": 200,
                "html_excerpt": "<main>Products</main>",
                "screenshot_path": None,
                "console_messages": [],
                "request_failures": [],
                "browser_settings": {"timeout_ms": 60000, "disable_resources": True, "network_idle": False},
            },
        )

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _return_page)

    result = await scrape_target(
        _target(
            FetcherType.dynamic,
            item_selector=".product-card",
            selectors={"title": "h2", "price": ".price"},
        ),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "success"
    assert len(result.data) == 1
    assert result.data[0]["title"] is None
    assert result.debug is not None
    assert result.debug["item_selector_count"] == 1
    assert result.debug["classification"] == ErrorType.selector_miss.value


@pytest.mark.asyncio
async def test_scrape_target_distinguishes_selector_engine_failures_from_empty_results(monkeypatch, tmp_path):
    item = _FakeNode(text="product card", css_map={"h2": []}, css_errors={".price": RuntimeError("selector engine failed")})
    page = _FakeNode(
        text="<html><title>Category</title><main>Products</main></html>",
        css_map={".product-card": [item]},
    )
    page.status = 200
    page.url = "https://example.com/category"

    async def _return_page(*_args, **_kwargs):
        return FetchOutcome(
            page=page,
            debug={
                "fetcher": "dynamic",
                "final_url": "https://example.com/category",
                "page_title": "Category",
                "main_document_status": 200,
                "html_excerpt": "<main>Products</main>",
                "screenshot_path": None,
                "console_messages": [],
                "request_failures": [],
                "browser_settings": {"timeout_ms": 60000, "disable_resources": True, "network_idle": False},
            },
        )

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _return_page)

    result = await scrape_target(
        _target(
            FetcherType.dynamic,
            item_selector=".product-card",
            selectors={"title": "h2", "price": ".price"},
        ),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "failed"
    assert result.error_type == ErrorType.selector_engine_error
    assert result.debug is not None
    assert result.debug["classification"] == ErrorType.selector_engine_error.value
    assert result.debug["selector_failure"] == {
        "operation": "count_selector_matches",
        "field_name": "price",
        "query_sha256": "d1335122501cdba534fa846d3cc4ab0fb5282e4628f6da024248961203fc3b2a",
        "selector_type": "css",
        "exception_type": "RuntimeError",
    }
