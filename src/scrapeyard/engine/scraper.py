"""Scrapling-based scraping engine — fetches URLs, applies selectors, handles pagination."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scrapling import Fetcher, PlayWrightFetcher, StealthyFetcher

from scrapeyard.common.settings import get_settings
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.adaptive_diagnostics import (
    log_adaptive_selector_gap,
    missing_adaptive_selectors,
)
from scrapeyard.engine.browser_debug import (
    browser_fetch_kwargs,
    capture_browser_state,
    default_debug_blob,
    fetch_basic_response,
    fetch_browser_response,
    populate_fetch_debug,
    response_title,
)
from scrapeyard.engine.detection import enrich_item_detection
from scrapeyard.engine.fetch_classifier import (
    classify_fetch_exception,
    classify_page_signals,
    classify_rendered_outcome,
)
from scrapeyard.engine.pagination import paginate_target, resolve_href
from scrapeyard.engine.resilience import RetryHandler, RetryableError
from scrapeyard.engine.scrape_models import FetchError, FetchOutcome, TargetResult
from scrapeyard.engine.selectors import count_selector_matches, extract_selectors, select_items


def _extract_page_data(page: Any, target: TargetConfig) -> list[dict[str, Any]]:
    """Extract records and enrich with pricing visibility and stock status detection."""
    if target.item_selector is not None:
        items = select_items(page, target.item_selector)
        data = [extract_selectors(item, target.selectors) for item in items]
    else:
        items = [page]
        data = [extract_selectors(page, target.selectors)]

    for item_data, element in zip(data, items, strict=False):
        enrich_item_detection(item_data, element, target.map_detection, target.stock_detection)
    return data


def _normalized_adaptive_domain(target: TargetConfig) -> str:
    if target.adaptive_domain:
        return target.adaptive_domain.strip().lower()
    return urlparse(target.url).hostname or "unknown-host"


def _adaptive_storage_url(target: TargetConfig) -> str:
    parsed = urlparse(target.url)
    scheme = parsed.scheme or "https"
    adaptive_domain = _normalized_adaptive_domain(target)
    return f"{scheme}://{adaptive_domain}/"


def _adaptive_fetch_kwargs(
    target: TargetConfig,
    *,
    adaptive: bool,
    adaptive_dir: str,
) -> dict[str, Any]:
    """Return parser kwargs for Scrapling adaptive matching."""
    if not adaptive:
        return {}
    return {
        "custom_config": {
            "auto_match": True,
            "storage_args": {
                "storage_file": str(Path(adaptive_dir) / "scrapling.db"),
                "url": _adaptive_storage_url(target),
            },
        },
    }


def _get_fetcher(fetcher_type: FetcherType) -> Any:
    """Return the Scrapling fetcher class for the given type."""
    mapping = {
        FetcherType.basic: Fetcher,
        FetcherType.stealthy: StealthyFetcher,
        FetcherType.dynamic: PlayWrightFetcher,
    }
    return mapping[fetcher_type]


def _selector_debug(page: Any, target: TargetConfig) -> dict[str, Any]:
    item_selector_count = None
    selector_scope = page
    if target.item_selector is not None:
        items = select_items(page, target.item_selector)
        item_selector_count = len(items)
        if items:
            selector_scope = items[0]
    selector_counts = {
        name: count_selector_matches(selector_scope, selector)
        for name, selector in target.selectors.items()
    }
    return {
        "item_selector_count": item_selector_count,
        "selector_counts": selector_counts,
    }


async def _fetch_page(
    fetcher_cls: Any,
    url: str,
    target: TargetConfig,
    fetcher_type: FetcherType,
    adaptive: bool,
    retryable_status: set[int],
    adaptive_dir: str,
    proxy_url: str | None = None,
    artifacts_dir: str | None = None,
) -> FetchOutcome:
    """Fetch a single page using the appropriate Scrapling method."""
    call_kwargs = _adaptive_fetch_kwargs(target, adaptive=adaptive, adaptive_dir=adaptive_dir)
    debug = default_debug_blob(fetcher_type, target, url)

    if fetcher_type == FetcherType.basic:
        if proxy_url is not None:
            call_kwargs["proxy"] = proxy_url
        response = await fetch_basic_response(fetcher_cls, url, call_kwargs)
    else:
        call_kwargs.update(browser_fetch_kwargs(target, proxy_url=proxy_url))
        response, capture = await fetch_browser_response(
            fetcher_cls,
            url,
            target,
            fetcher_type,
            call_kwargs,
            artifacts_dir,
        )
        debug.update(capture)

    populate_fetch_debug(debug, response, url)
    if response.status and response.status >= 400:
        if response.status in retryable_status:
            raise RetryableError(response.status)
        raise FetchError(response.status, debug=debug)
    return FetchOutcome(page=response, debug=debug)


async def _fetch_target_page(
    retry_handler: RetryHandler,
    fetcher_cls: Any,
    url: str,
    target: TargetConfig,
    adaptive: bool,
    retryable_status: set[int],
    adaptive_dir: str,
    proxy_url: str | None,
    artifacts_dir: str | None,
) -> FetchOutcome:
    return await retry_handler.execute(
        _fetch_page,
        fetcher_cls,
        url,
        target,
        target.fetcher,
        adaptive,
        retryable_status,
        adaptive_dir,
        proxy_url,
        artifacts_dir,
    )


def _finalize_target_success(result: TargetResult, target: TargetConfig) -> None:
    rendered_classification = classify_rendered_outcome(
        result.debug or {},
        result.data,
        has_item_selector=target.item_selector is not None,
    )
    if rendered_classification is not None and result.debug is not None:
        result.debug["classification"] = rendered_classification.value
    result.status = "success"


async def scrape_target(
    target: TargetConfig,
    adaptive: bool,
    retry: RetryConfig,
    adaptive_dir: str | None = None,
    proxy_url: str | None = None,
    artifacts_dir: str | None = None,
) -> TargetResult:
    """Fetch a URL, apply selectors, and handle pagination."""
    result = TargetResult(url=target.url)

    if adaptive_dir is None:
        adaptive_dir = get_settings().adaptive_dir

    Path(adaptive_dir).mkdir(parents=True, exist_ok=True)
    fetcher_cls = _get_fetcher(target.fetcher)
    retry_handler = RetryHandler(retry)
    retryable_status = set(retry.retryable_status)

    try:
        outcome = await _fetch_target_page(
            retry_handler,
            fetcher_cls,
            target.url,
            target,
            adaptive,
            retryable_status,
            adaptive_dir,
            proxy_url,
            artifacts_dir,
        )
        page = outcome.page
        result.debug = outcome.debug
        result.debug.update(_selector_debug(page, target))
        page_data = _extract_page_data(page, target)
        if adaptive:
            log_adaptive_selector_gap(target, page_data)
        result.data.extend(page_data)
        result.pages_scraped = 1
        await paginate_target(
            page=page,
            target=target,
            result=result,
            fetch_target_page=_fetch_target_page,
            extract_page_data=_extract_page_data,
            retry_handler=retry_handler,
            fetcher_cls=fetcher_cls,
            adaptive=adaptive,
            retryable_status=retryable_status,
            adaptive_dir=adaptive_dir,
            proxy_url=proxy_url,
            artifacts_dir=artifacts_dir,
        )
        _finalize_target_success(result, target)
    except Exception as exc:
        error_type, http_status, debug = classify_fetch_exception(exc, target.fetcher)
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        result.status = "failed"
        result.error_type = error_type
        result.http_status = http_status
        result.error_detail = detail
        result.debug = debug or default_debug_blob(target.fetcher, target, target.url)
        result.debug.setdefault("classification", error_type.value)
        result.errors.append(detail)

    return result


# Backwards-compatible aliases for tests and patch surfaces.
_default_debug_blob = default_debug_blob
_browser_fetch_kwargs = browser_fetch_kwargs
_response_title = response_title
_missing_adaptive_selectors = missing_adaptive_selectors
_classify_page_signals = classify_page_signals
_classify_fetch_exception = classify_fetch_exception
_classify_rendered_outcome = classify_rendered_outcome
_capture_browser_state = capture_browser_state
_fetch_basic_response = fetch_basic_response
_fetch_browser_response = fetch_browser_response
_populate_fetch_debug = populate_fetch_debug
_paginate = paginate_target
_resolve_href = resolve_href
