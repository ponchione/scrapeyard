"""Scrapling-based scraping engine — fetches URLs, applies selectors, handles pagination."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from scrapling import Fetcher

from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.common.json_encoding import compact_json_size
from scrapeyard.common.run_threads import run_thread_work
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
from scrapeyard.engine.browser_fetchers import CamoufoxFetcher, DynamicFetcher
from scrapeyard.engine.detection import enrich_item_detection
from scrapeyard.engine.fetch_classifier import (
    classify_fetch_exception,
    classify_rendered_outcome,
)
from scrapeyard.engine.pagination import paginate_target
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.resilience import RetryHandler, RetryableError
from scrapeyard.engine.scrape_models import (
    FetchError,
    FetchOutcome,
    TargetResult as TargetResult,
    TargetStatus as TargetStatus,
)
from scrapeyard.engine.selectors import (
    SelectorExecutionError,
    count_selector_matches_strict,
    extract_selectors_strict,
    select_items_strict,
)
from scrapeyard.models.job import ErrorType
from scrapeyard.queue.cancellation import (
    CancellationCheckpoint,
    cancellation_checkpoint,
)
from scrapeyard.engine.url_guard import (
    assert_public_url,
    redact_userinfo_in_text,
    resolve_public_url,
    url_host_label,
)

_BASIC_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_BASIC_REDIRECTS = 10

@dataclass(frozen=True)
class ScrapeContext:
    fetcher_cls: Any
    retry_handler: RetryHandler
    retryable_status: set[int]
    adaptive_dir: str
    budget: RunBudget | None
    cancellation_guard: CancellationCheckpoint | None
    response_observer: Callable[[], None] | None
    rate_limiter: DomainRateLimiter | None
    domain_rate_limit: float


@dataclass(frozen=True)
class ScrapePageResult:
    page: Any
    debug: dict[str, Any]
    page_data: list[dict[str, Any]]


async def _run_page_cpu_work(
    context: ScrapeContext,
    function: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Keep selector/detection CPU work off the event loop and under deadline."""

    return await run_thread_work(
        function,
        *args,
        run_budget=context.budget,
        **kwargs,
    )


def _extract_page_data(
    page: Any,
    target: TargetConfig,
    *,
    budget: RunBudget | None = None,
) -> list[dict[str, Any]]:
    """Extract records and enrich with pricing visibility and stock status detection."""
    if target.item_selector is not None:
        items = select_items_strict(page, target.item_selector)
    else:
        items = [page]

    if budget is not None:
        budget.reserve_extracted_records(len(items))

    data: list[dict[str, Any]] = []
    for element in items:
        reserved_bytes = 0

        def reserve_output_bytes(amount: int) -> None:
            nonlocal reserved_bytes
            if budget is not None:
                budget.reserve_estimated_result_bytes(amount)
                reserved_bytes += amount

        item_data = extract_selectors_strict(
            element,
            target.selectors,
            reserve_output_bytes=reserve_output_bytes if budget is not None else None,
        )
        enrich_item_detection(
            item_data,
            element,
            target.map_detection,
            target.stock_detection,
            budget=budget,
        )
        if budget is not None:
            finalized_size = compact_json_size(item_data)
            # One conservative byte covers the record's array separator.
            # Selector extraction already reserved the base dictionary, so
            # only reserve enrichment growth beyond that representation.
            budget.reserve_estimated_result_bytes(
                max(0, finalized_size - reserved_bytes) + 1
            )
        data.append(item_data)
    return data


def _normalized_adaptive_domain(target: TargetConfig) -> str:
    if target.adaptive_domain:
        return target.adaptive_domain.strip().lower()
    return url_host_label(target.url)


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
        FetcherType.stealthy: CamoufoxFetcher,
        FetcherType.dynamic: DynamicFetcher,
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


def _measured_response_body_bytes(response: Any) -> int | None:
    """Return the byte size of the body representation exposed by a fetcher."""

    body = getattr(response, "body", None)
    if isinstance(body, (bytes, bytearray, memoryview)):
        return len(body)
    if not isinstance(body, str):
        return None
    encoding = getattr(response, "encoding", None)
    if not isinstance(encoding, str) or not encoding:
        encoding = "utf-8"
    try:
        return len(body.encode(encoding))
    except (LookupError, UnicodeEncodeError):
        return len(body.encode("utf-8"))


async def _fetch_basic_with_safe_redirects(
    fetcher_cls: Any,
    url: str,
    call_kwargs: dict[str, Any],
    debug: dict[str, Any],
    *,
    require_resolved_dns: bool = False,
    budget: RunBudget | None = None,
    cancellation_guard: CancellationCheckpoint | None = None,
    response_observer: Callable[[], None] | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    domain_rate_limit: float = 0,
) -> Any:
    """Follow basic-fetch redirects only after validating each destination."""
    current_url = url
    redirects: list[str] = []
    cookie_jar = httpx.Cookies()
    call_kwargs["follow_redirects"] = False
    for _ in range(_MAX_BASIC_REDIRECTS + 1):
        request_url = current_url
        request_kwargs = dict(call_kwargs)
        production_stream = fetcher_cls is Fetcher
        if production_stream and request_kwargs.get("proxy") is None:
            resolved = await run_thread_work(
                resolve_public_url,
                current_url,
                run_budget=budget,
            )
            request_url = resolved.connect_url
            headers = dict(request_kwargs.get("headers") or {})
            headers["Host"] = resolved.host_header
            request_kwargs["headers"] = headers
            extensions = dict(request_kwargs.get("extensions") or {})
            extensions["sni_hostname"] = resolved.sni_hostname
            request_kwargs["extensions"] = extensions
        else:
            await _assert_fetch_url(
                current_url,
                require_resolved_dns=require_resolved_dns,
                budget=budget,
            )
        await _acquire_request_rate_limit(
            current_url,
            rate_limiter=rate_limiter,
            min_interval=domain_rate_limit,
            budget=budget,
            cancellation_guard=cancellation_guard,
        )
        if production_stream:
            request_kwargs["cookie_jar"] = cookie_jar
            request_kwargs["cookie_url"] = current_url
            request_kwargs["header_url"] = current_url
            response = await fetch_basic_response(
                fetcher_cls,
                request_url,
                request_kwargs,
                budget=budget,
            )
        else:
            response = await fetch_basic_response(fetcher_cls, request_url, request_kwargs)
        if response_observer is not None:
            response_observer()
        if request_url != current_url:
            # Parsing, redirect joining, and public diagnostics use the logical
            # URL, never the implementation-only pinned IP URL.
            response.url = current_url
        await cancellation_checkpoint(
            cancellation_guard,
            "after_fetch",
        )
        if budget is not None and not production_stream:
            measured_bytes = _measured_response_body_bytes(response)
            if measured_bytes is not None:
                await budget.consume_fetched_bytes(measured_bytes)
            else:
                budget.check_deadline()
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
        redirects.append(current_url)
        debug["redirects"] = list(redirects)
    raise FetchError(310, debug={**debug, "redirects": redirects})


async def _assert_fetch_url(
    url: str,
    *,
    require_resolved_dns: bool,
    budget: RunBudget | None,
) -> None:
    await run_thread_work(
        assert_public_url,
        url,
        run_budget=budget,
        allow_unresolved=not require_resolved_dns,
    )


async def _assert_connection_endpoint(
    url: str,
    *,
    allowed_schemes: tuple[str, ...],
    budget: RunBudget | None,
) -> None:
    """Resolve a remote transport endpoint immediately before it is used."""

    await run_thread_work(
        assert_public_url,
        url,
        run_budget=budget,
        allowed_schemes=allowed_schemes,
        allow_unresolved=False,
    )


async def _acquire_request_rate_limit(
    url: str,
    *,
    rate_limiter: DomainRateLimiter | None,
    min_interval: float,
    budget: RunBudget | None,
    cancellation_guard: CancellationCheckpoint | None,
) -> None:
    """Throttle one top-level network attempt at its actual request boundary."""

    if rate_limiter is None:
        return
    await cancellation_checkpoint(cancellation_guard, "before_rate_limit_wait")
    acquire = rate_limiter.acquire(url_host_label(url), min_interval)
    if budget is None:
        await acquire
    else:
        await budget.wait_for(acquire)
    await cancellation_checkpoint(cancellation_guard, "after_rate_limit_wait")


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
    budget: RunBudget | None = None,
    cancellation_guard: CancellationCheckpoint | None = None,
    response_observer: Callable[[], None] | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    domain_rate_limit: float = 0,
) -> FetchOutcome:
    """Fetch a single page using the appropriate Scrapling method."""
    response_observed = False

    def observe_response_once() -> None:
        nonlocal response_observed
        if response_observed:
            return
        response_observed = True
        if response_observer is not None:
            response_observer()

    call_kwargs = _adaptive_fetch_kwargs(target, adaptive=adaptive, adaptive_dir=adaptive_dir)
    debug = default_debug_blob(fetcher_type, target, url)
    require_resolved_dns = _requires_verified_dns(target, fetcher_type, proxy_url)
    if proxy_url is not None:
        await _assert_connection_endpoint(
            proxy_url,
            allowed_schemes=("http", "https", "socks4", "socks4a", "socks5", "socks5h"),
            budget=budget,
        )
    if target.browser is not None and target.browser.cdp_url is not None:
        await _assert_connection_endpoint(
            target.browser.cdp_url,
            allowed_schemes=("http", "https", "ws", "wss"),
            budget=budget,
        )

    if fetcher_type == FetcherType.basic:
        fetch_timeout = get_settings().basic_fetch_timeout_seconds
        if budget is not None:
            budget.check_deadline()
            fetch_timeout = min(fetch_timeout, max(budget.remaining_seconds, 0.001))
        call_kwargs.setdefault("timeout", fetch_timeout)
        if proxy_url is not None:
            call_kwargs["proxy"] = proxy_url
        response = await _fetch_basic_with_safe_redirects(
            fetcher_cls,
            url,
            call_kwargs,
            debug,
            require_resolved_dns=require_resolved_dns,
            budget=budget,
            cancellation_guard=cancellation_guard,
            response_observer=observe_response_once,
            rate_limiter=rate_limiter,
            domain_rate_limit=domain_rate_limit,
        )
    else:
        await _assert_fetch_url(
            url,
            require_resolved_dns=require_resolved_dns,
            budget=budget,
        )
        call_kwargs.update(browser_fetch_kwargs(target, fetcher_type, proxy_url=proxy_url))
        await _acquire_request_rate_limit(
            url,
            rate_limiter=rate_limiter,
            min_interval=domain_rate_limit,
            budget=budget,
            cancellation_guard=cancellation_guard,
        )
        response, capture = await fetch_browser_response(
            fetcher_cls,
            url,
            target,
            fetcher_type,
            call_kwargs,
            artifacts_dir,
            require_resolved_dns=require_resolved_dns,
            budget=budget,
            response_observer=observe_response_once,
        )
        observe_response_once()
        await cancellation_checkpoint(
            cancellation_guard,
            "after_fetch",
        )
        debug.update(capture)

    populate_fetch_debug(debug, response, url)
    await _assert_fetch_url(
        debug["final_url"],
        require_resolved_dns=require_resolved_dns,
        budget=budget,
    )
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
    budget: RunBudget | None = None,
    cancellation_guard: CancellationCheckpoint | None = None,
    response_observer: Callable[[], None] | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    domain_rate_limit: float = 0,
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
        budget,
        cancellation_guard,
        response_observer,
        rate_limiter,
        domain_rate_limit,
    )


def _prepare_scrape_context(
    target: TargetConfig,
    retry: RetryConfig,
    adaptive_dir: str | None,
    budget: RunBudget | None,
    cancellation_guard: CancellationCheckpoint | None,
    response_observer: Callable[[], None] | None,
    rate_limiter: DomainRateLimiter | None,
    domain_rate_limit: float,
) -> ScrapeContext:
    resolved_adaptive_dir = adaptive_dir or get_settings().adaptive_dir
    Path(resolved_adaptive_dir).mkdir(parents=True, exist_ok=True)
    return ScrapeContext(
        fetcher_cls=_get_fetcher(target.fetcher),
        retry_handler=RetryHandler(
            retry,
            budget=budget,
            cancellation_guard=cancellation_guard,
        ),
        retryable_status=set(retry.retryable_status),
        adaptive_dir=resolved_adaptive_dir,
        budget=budget,
        cancellation_guard=cancellation_guard,
        response_observer=response_observer,
        rate_limiter=rate_limiter,
        domain_rate_limit=domain_rate_limit,
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
        context.budget,
        context.cancellation_guard,
        context.response_observer,
        context.rate_limiter,
        context.domain_rate_limit,
    )
    await cancellation_checkpoint(
        context.cancellation_guard,
        "after_first_page_fetch",
    )
    result.debug = outcome.debug
    selector_debug = await _run_page_cpu_work(
        context,
        _selector_debug,
        outcome.page,
        target,
    )
    result.debug.update(selector_debug)
    page_data = await _run_page_cpu_work(
        context,
        _extract_page_data,
        outcome.page,
        target,
        budget=context.budget,
    )
    if adaptive:
        await _run_page_cpu_work(
            context,
            log_adaptive_selector_gap,
            target,
            page_data,
        )
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
        extract_page_data=lambda page, target: _extract_page_data(
            page,
            target,
            budget=context.budget,
        ),
        retry_handler=context.retry_handler,
        fetcher_cls=context.fetcher_cls,
        adaptive=adaptive,
        retryable_status=context.retryable_status,
        adaptive_dir=context.adaptive_dir,
        proxy_url=proxy_url,
        artifacts_dir=artifacts_dir,
        budget=context.budget,
        cancellation_guard=context.cancellation_guard,
        rate_limiter=context.rate_limiter,
        domain_rate_limit=context.domain_rate_limit,
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
    budget: RunBudget | None = None,
    cancellation_guard: CancellationCheckpoint | None = None,
    response_observer: Callable[[], None] | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    domain_rate_limit: float = 0,
) -> TargetResult:
    """Fetch a URL, apply selectors, and handle pagination."""
    result = TargetResult(url=target.url)
    context = _prepare_scrape_context(
        target,
        retry,
        adaptive_dir,
        budget,
        cancellation_guard,
        response_observer,
        rate_limiter,
        domain_rate_limit,
    )

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
        await _run_page_cpu_work(
            context,
            _finalize_target_success,
            result,
            target,
        )
    except BudgetExceeded:
        raise
    except SelectorExecutionError as exc:
        _handle_selector_execution_failure(result, target, exc)
    except Exception as exc:
        _handle_scrape_exception(result, target, exc)

    return result
