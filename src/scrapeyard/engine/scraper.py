"""Scrapling-based scraping engine — fetches URLs, applies selectors, handles pagination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from scrapling import Fetcher, PlayWrightFetcher, StealthyFetcher

from scrapeyard.common.settings import get_settings
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.adaptive_diagnostics import log_adaptive_selector_gap
from scrapeyard.engine.browser_debug import (
    browser_fetch_kwargs,
    default_debug_blob,
    fetch_basic_response,
    fetch_browser_response,
    populate_fetch_debug,
)
from scrapeyard.engine.detection import enrich_item_detection
from scrapeyard.engine.fetch_classifier import (
    classify_fetch_exception,
    classify_rendered_outcome,
)
from scrapeyard.engine.pagination import paginate_target
from scrapeyard.engine.resilience import RetryHandler, RetryableError
from scrapeyard.engine.scrape_models import FetchError, FetchOutcome, TargetResult, TargetStatus
from scrapeyard.engine.selectors import (
    SelectorExecutionError,
    count_selector_matches_strict,
    extract_selectors_strict,
    select_items_strict,
)
from scrapeyard.models.job import ErrorType
from scrapeyard.engine.url_guard import assert_public_url, redact_userinfo_in_text

_BASIC_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_BASIC_REDIRECTS = 10

@dataclass(frozen=True)
class ScrapeContext:
    fetcher_cls: Any
    retry_handler: RetryHandler
    retryable_status: set[int]
    adaptive_dir: str


@dataclass(frozen=True)
class ScrapePageResult:
    page: Any
    debug: dict[str, Any]
    page_data: list[dict[str, Any]]


def _extract_page_data(page: Any, target: TargetConfig) -> list[dict[str, Any]]:
    """Extract records and enrich with pricing visibility and stock status detection."""
    if target.item_selector is not None:
        items = select_items_strict(page, target.item_selector)
        data = [extract_selectors_strict(item, target.selectors) for item in items]
    else:
        items = [page]
        data = [extract_selectors_strict(page, target.selectors)]

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


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None) or {}
    if not hasattr(headers, "items"):
        return None
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return str(value)
    return None


async def _fetch_basic_with_safe_redirects(
    fetcher_cls: Any,
    url: str,
    call_kwargs: dict[str, Any],
    debug: dict[str, Any],
    *,
    require_resolved_dns: bool = False,
) -> Any:
    """Follow basic-fetch redirects only after validating each destination."""
    current_url = url
    redirects: list[str] = []
    call_kwargs["follow_redirects"] = False
    for _ in range(_MAX_BASIC_REDIRECTS + 1):
        await _assert_fetch_url(current_url, require_resolved_dns=require_resolved_dns)
        response = await fetch_basic_response(fetcher_cls, current_url, call_kwargs)
        if getattr(response, "status", None) not in _BASIC_REDIRECT_STATUSES:
            if redirects:
                debug["redirects"] = redirects
            return response
        location = _response_header(response, "location")
        if not location:
            return response
        response_url = getattr(response, "url", None)
        base_url = response_url if isinstance(response_url, str) and response_url else current_url
        current_url = urljoin(base_url, location)
        await _assert_fetch_url(current_url, require_resolved_dns=require_resolved_dns)
        redirects.append(current_url)
    raise FetchError(310, debug={**debug, "redirects": redirects})


async def _assert_fetch_url(url: str, *, require_resolved_dns: bool) -> None:
    await asyncio.to_thread(
        assert_public_url,
        url,
        allow_unresolved=not require_resolved_dns,
    )


def _requires_verified_dns(target: TargetConfig, fetcher_type: FetcherType, proxy_url: str | None) -> bool:
    if proxy_url is not None:
        return True
    return bool(
        fetcher_type == FetcherType.dynamic
        and target.browser is not None
        and target.browser.cdp_url is not None
    )


def _selector_debug(page: Any, target: TargetConfig) -> dict[str, Any]:
    item_selector_count = None
    selector_scope = page
    if target.item_selector is not None:
        items = select_items_strict(page, target.item_selector)
        item_selector_count = len(items)
        if items:
            selector_scope = items[0]
    selector_counts = {
        name: count_selector_matches_strict(selector_scope, selector, field_name=name)
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
    require_resolved_dns = _requires_verified_dns(target, fetcher_type, proxy_url)

    if fetcher_type == FetcherType.basic:
        call_kwargs.setdefault("timeout", get_settings().basic_fetch_timeout_seconds)
        if proxy_url is not None:
            call_kwargs["proxy"] = proxy_url
        response = await _fetch_basic_with_safe_redirects(
            fetcher_cls,
            url,
            call_kwargs,
            debug,
            require_resolved_dns=require_resolved_dns,
        )
    else:
        await _assert_fetch_url(url, require_resolved_dns=require_resolved_dns)
        call_kwargs.update(browser_fetch_kwargs(target, fetcher_type, proxy_url=proxy_url))
        response, capture = await fetch_browser_response(
            fetcher_cls,
            url,
            target,
            fetcher_type,
            call_kwargs,
            artifacts_dir,
            require_resolved_dns=require_resolved_dns,
        )
        debug.update(capture)

    populate_fetch_debug(debug, response, url)
    await _assert_fetch_url(debug["final_url"], require_resolved_dns=require_resolved_dns)
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


def _prepare_scrape_context(
    target: TargetConfig,
    retry: RetryConfig,
    adaptive_dir: str | None,
) -> ScrapeContext:
    resolved_adaptive_dir = adaptive_dir or get_settings().adaptive_dir
    Path(resolved_adaptive_dir).mkdir(parents=True, exist_ok=True)
    return ScrapeContext(
        fetcher_cls=_get_fetcher(target.fetcher),
        retry_handler=RetryHandler(retry),
        retryable_status=set(retry.retryable_status),
        adaptive_dir=resolved_adaptive_dir,
    )


async def _scrape_first_page(
    *,
    context: ScrapeContext,
    target: TargetConfig,
    result: TargetResult,
    adaptive: bool,
    proxy_url: str | None,
    artifacts_dir: str | None,
) -> ScrapePageResult:
    outcome = await _fetch_target_page(
        context.retry_handler,
        context.fetcher_cls,
        target.url,
        target,
        adaptive,
        context.retryable_status,
        context.adaptive_dir,
        proxy_url,
        artifacts_dir,
    )
    result.debug = outcome.debug
    result.debug.update(_selector_debug(outcome.page, target))
    page_data = _extract_page_data(outcome.page, target)
    if adaptive:
        log_adaptive_selector_gap(target, page_data)
    result.data.extend(page_data)
    result.pages_scraped = 1
    return ScrapePageResult(page=outcome.page, debug=outcome.debug, page_data=page_data)


async def _scrape_paginated_pages(
    *,
    page: Any,
    target: TargetConfig,
    result: TargetResult,
    context: ScrapeContext,
    adaptive: bool,
    proxy_url: str | None,
    artifacts_dir: str | None,
) -> None:
    await paginate_target(
        page=page,
        target=target,
        result=result,
        fetch_target_page=_fetch_target_page,
        extract_page_data=_extract_page_data,
        retry_handler=context.retry_handler,
        fetcher_cls=context.fetcher_cls,
        adaptive=adaptive,
        retryable_status=context.retryable_status,
        adaptive_dir=context.adaptive_dir,
        proxy_url=proxy_url,
        artifacts_dir=artifacts_dir,
    )


def _finalize_target_success(result: TargetResult, target: TargetConfig) -> None:
    rendered_classification = classify_rendered_outcome(
        result.debug or {},
        result.data,
        has_item_selector=target.item_selector is not None,
    )
    if rendered_classification is not None and result.debug is not None:
        result.debug["classification"] = rendered_classification.value
    result.status = TargetStatus.success


def _handle_selector_execution_failure(
    result: TargetResult,
    target: TargetConfig,
    exc: SelectorExecutionError,
) -> None:
    detail = redact_userinfo_in_text(str(exc))
    result.status = TargetStatus.failed
    result.error_type = ErrorType.selector_engine_error
    result.error_detail = detail
    result.debug = result.debug or default_debug_blob(target.fetcher, target, target.url)
    result.debug["classification"] = ErrorType.selector_engine_error.value
    result.debug["selector_failure"] = exc.debug
    result.errors.append(detail)


def _handle_scrape_exception(
    result: TargetResult,
    target: TargetConfig,
    exc: Exception,
) -> None:
    error_type, http_status, debug = classify_fetch_exception(exc, target.fetcher)
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    detail = redact_userinfo_in_text(detail)
    result.status = TargetStatus.failed
    result.error_type = error_type
    result.http_status = http_status
    result.error_detail = detail
    result.debug = debug or default_debug_blob(target.fetcher, target, target.url)
    result.debug.setdefault("classification", error_type.value)
    result.errors.append(detail)


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
    context = _prepare_scrape_context(target, retry, adaptive_dir)

    try:
        first_page = await _scrape_first_page(
            context=context,
            target=target,
            result=result,
            adaptive=adaptive,
            proxy_url=proxy_url,
            artifacts_dir=artifacts_dir,
        )
        await _scrape_paginated_pages(
            page=first_page.page,
            target=target,
            result=result,
            context=context,
            adaptive=adaptive,
            proxy_url=proxy_url,
            artifacts_dir=artifacts_dir,
        )
        _finalize_target_success(result, target)
    except SelectorExecutionError as exc:
        _handle_selector_execution_failure(result, target, exc)
    except Exception as exc:
        _handle_scrape_exception(result, target, exc)

    return result
