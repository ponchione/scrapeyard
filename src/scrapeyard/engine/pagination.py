"""Pagination helpers for scraper target processing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import quote_plus, unquote_plus, urljoin, urlsplit, urlunsplit

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.run_threads import run_thread_work
from scrapeyard.config.schema import PaginationMode, TargetConfig
from scrapeyard.engine.scrape_models import (
    CLICK_PAGINATION_ATTRIBUTE,
    ClickPaginationResult,
    FetchOutcome,
    TargetResult,
)
from scrapeyard.engine.rate_limiter import DomainRateLimiter
from scrapeyard.engine.selectors import select_elements_strict
from scrapeyard.engine.url_guard import (
    URLResolutionError,
    UnsafeURLError,
    assert_public_url,
    url_host_label,
)
from scrapeyard.queue.cancellation import (
    CancellationCheckpoint,
    cancellation_checkpoint,
)

FetchTargetPageCallable = Callable[..., Awaitable[FetchOutcome]]
ExtractPageDataCallable = Callable[[object, TargetConfig], list[dict[str, Any]]]


async def _run_cpu_work(
    function: Callable[..., Any],
    *args: Any,
    budget: RunBudget | None,
    **kwargs: Any,
) -> Any:
    return await run_thread_work(function, *args, run_budget=budget, **kwargs)


def resolve_href(element: object, base_url: str) -> str | None:
    """Return an absolute URL for a next-page element or None."""
    attrib = getattr(element, "attrib", None)
    attrib_get = getattr(attrib, "get", None)
    href = attrib_get("href") if callable(attrib_get) else None
    if href is None:
        attributes = getattr(element, "attributes", None)
        attributes_get = getattr(attributes, "get", None)
        if not callable(attributes_get):
            return None
        href = attributes_get("href")
    if not href:
        return None
    if not isinstance(href, str):
        href = str(href)
    return urljoin(base_url, href)


def pagination_url_key(url: str) -> str:
    """Return a normalized key for pagination loop detection."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    path = parsed.path or "/"
    return urlunsplit((scheme, url_host_label(url), path, parsed.query, ""))


async def _pagination_url_is_safe(
    url: str,
    *,
    budget: RunBudget | None,
    cancellation_guard: CancellationCheckpoint | None,
) -> bool:
    await cancellation_checkpoint(
        cancellation_guard,
        "before_pagination_dns_validation",
    )
    if budget is not None:
        budget.check_deadline()
    try:
        await run_thread_work(
            assert_public_url,
            url,
            run_budget=budget,
            allow_unresolved=False,
        )
    except UnsafeURLError:
        return False
    except URLResolutionError:
        # The actual fetch repeats DNS validation inside RetryHandler. Continue
        # to that path so resolver availability failures consume retry policy,
        # while concrete unsafe destinations remain permanently blocked above.
        return True
    await cancellation_checkpoint(
        cancellation_guard,
        "after_pagination_dns_validation",
    )
    if budget is not None:
        budget.check_deadline()
    return True


def with_query_param(url: str, name: str, value: int) -> str:
    """Return ``url`` with query parameter ``name`` set to ``value``.

    Other query segments are preserved byte for byte, in order. The first
    existing occurrence of ``name`` is replaced and later duplicates dropped.
    """
    parts = urlsplit(url)
    segments = [segment for segment in parts.query.split("&") if segment] if parts.query else []
    replacement = f"{quote_plus(name, safe='[]')}={value}"
    rebuilt: list[str] = []
    replaced = False
    for segment in segments:
        key = unquote_plus(segment.split("=", 1)[0])
        if key == name:
            if not replaced:
                rebuilt.append(replacement)
                replaced = True
            continue
        rebuilt.append(segment)
    if not replaced:
        rebuilt.append(replacement)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(rebuilt), ""))


def page_data_fingerprint(page_data: list[dict[str, Any]]) -> str:
    """Stable identity of one page's extracted records, for repeat detection."""
    encoded = json.dumps(page_data, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _fetch_follow_on_page(
    next_url: str,
    *,
    target: TargetConfig,
    fetch_target_page: FetchTargetPageCallable,
    retry_handler: object,
    fetcher_cls: object,
    adaptive: bool,
    retryable_status: set[int],
    adaptive_dir: str,
    proxy_url: str | None,
    artifacts_dir: str | None,
    budget: RunBudget | None,
    cancellation_guard: CancellationCheckpoint | None,
    rate_limiter: DomainRateLimiter | None,
    domain_rate_limit: float,
) -> FetchOutcome:
    fetch_args = (
        retry_handler,
        fetcher_cls,
        next_url,
        target,
        adaptive,
        retryable_status,
        adaptive_dir,
        proxy_url,
        artifacts_dir,
    )
    rate_limit_kwargs = (
        {}
        if rate_limiter is None
        else {
            "rate_limiter": rate_limiter,
            "domain_rate_limit": domain_rate_limit,
        }
    )
    if budget is None and cancellation_guard is None:
        outcome = await fetch_target_page(*fetch_args, **rate_limit_kwargs)
    elif cancellation_guard is None:
        outcome = await fetch_target_page(
            *fetch_args,
            budget,
            **rate_limit_kwargs,
        )
    else:
        outcome = await fetch_target_page(
            *fetch_args,
            budget,
            cancellation_guard,
            **rate_limit_kwargs,
        )
    await cancellation_checkpoint(
        cancellation_guard,
        "after_pagination_fetch",
    )
    return outcome


async def paginate_target(
    *,
    page: object,
    target: TargetConfig,
    result: TargetResult,
    fetch_target_page: FetchTargetPageCallable,
    extract_page_data: ExtractPageDataCallable,
    retry_handler: object,
    fetcher_cls: object,
    adaptive: bool,
    retryable_status: set[int],
    adaptive_dir: str,
    proxy_url: str | None,
    artifacts_dir: str | None,
    budget: RunBudget | None = None,
    cancellation_guard: CancellationCheckpoint | None = None,
    rate_limiter: DomainRateLimiter | None = None,
    domain_rate_limit: float = 0,
) -> None:
    if target.pagination is None:
        result.pagination_stop_reason = "not_configured"
        return

    if target.pagination.mode is PaginationMode.click:
        await _extract_click_pages(
            page=page,
            target=target,
            result=result,
            extract_page_data=extract_page_data,
            budget=budget,
            cancellation_guard=cancellation_guard,
        )
        return

    fetch_kwargs: dict[str, Any] = {
        "target": target,
        "fetch_target_page": fetch_target_page,
        "retry_handler": retry_handler,
        "fetcher_cls": fetcher_cls,
        "adaptive": adaptive,
        "retryable_status": retryable_status,
        "adaptive_dir": adaptive_dir,
        "proxy_url": proxy_url,
        "artifacts_dir": artifacts_dir,
        "budget": budget,
        "cancellation_guard": cancellation_guard,
        "rate_limiter": rate_limiter,
        "domain_rate_limit": domain_rate_limit,
    }
    if target.pagination.page_param is not None:
        await _paginate_by_page_param(
            page=page,
            target=target,
            result=result,
            extract_page_data=extract_page_data,
            fetch_kwargs=fetch_kwargs,
        )
        return

    current_url = (result.debug.get("final_url") if result.debug else None) or target.url
    seen_urls = {pagination_url_key(current_url)}
    next_selector = target.pagination.next
    assert next_selector is not None  # Enforced by PaginationConfig validation.
    # Inspect the last permitted page too: execution success alone does not
    # establish that pagination exhausted the configured listing scope.
    for page_number in range(1, target.pagination.max_pages + 1):
        await cancellation_checkpoint(
            cancellation_guard,
            "before_pagination_page",
        )
        if budget is not None:
            budget.check_deadline()
        next_links = await _run_cpu_work(
            select_elements_strict,
            page,
            next_selector,
            budget=budget,
            operation="select_pagination_next",
        )
        if not next_links:
            result.pagination_stop_reason = "exhausted"
            break
        next_url = resolve_href(next_links[0], current_url)
        if not next_url:
            result.pagination_stop_reason = "invalid_next_link"
            break
        if page_number == target.pagination.max_pages:
            result.pagination_stop_reason = "max_pages"
            break
        if not await _pagination_url_is_safe(
            next_url,
            budget=budget,
            cancellation_guard=cancellation_guard,
        ):
            result.pagination_stop_reason = "unsafe_next_url"
            break
        if pagination_url_key(next_url) in seen_urls:
            result.pagination_stop_reason = "repeated_url"
            break

        next_outcome = await _fetch_follow_on_page(next_url, **fetch_kwargs)
        final_url = next_outcome.debug.get("final_url") or next_url
        final_key = pagination_url_key(final_url)
        if final_key in seen_urls:
            result.pagination_stop_reason = "repeated_url"
            break
        seen_urls.add(final_key)
        page = next_outcome.page
        current_url = final_url
        page_data = await _run_cpu_work(
            extract_page_data,
            page,
            target,
            budget=budget,
        )
        result.data.extend(page_data)
        result.pages_scraped += 1
        await cancellation_checkpoint(
            cancellation_guard,
            "after_pagination_page",
        )


async def _paginate_by_page_param(
    *,
    page: object,
    target: TargetConfig,
    result: TargetResult,
    extract_page_data: ExtractPageDataCallable,
    fetch_kwargs: dict[str, Any],
) -> None:
    pagination = target.pagination
    assert pagination is not None and pagination.page_param is not None
    budget: RunBudget | None = fetch_kwargs["budget"]
    cancellation_guard: CancellationCheckpoint | None = fetch_kwargs["cancellation_guard"]
    # Every page derives from the first page's final URL, so a redirect on a
    # later page cannot change the numbering base.
    base_url = (result.debug.get("final_url") if result.debug else None) or target.url
    seen_urls = {pagination_url_key(base_url)}
    seen_pages = {page_data_fingerprint(result.data)}
    for page_number in range(1, pagination.max_pages + 1):
        await cancellation_checkpoint(
            cancellation_guard,
            "before_pagination_page",
        )
        if budget is not None:
            budget.check_deadline()
        if pagination.next is not None:
            # A configured next element only signals that another page exists.
            next_links = await _run_cpu_work(
                select_elements_strict,
                page,
                pagination.next,
                budget=budget,
                operation="select_pagination_next",
            )
            if not next_links:
                result.pagination_stop_reason = "exhausted"
                break
        if page_number == pagination.max_pages:
            result.pagination_stop_reason = "max_pages"
            break
        next_url = with_query_param(
            base_url,
            pagination.page_param,
            pagination.page_first + page_number * pagination.page_step,
        )
        if not await _pagination_url_is_safe(
            next_url,
            budget=budget,
            cancellation_guard=cancellation_guard,
        ):
            result.pagination_stop_reason = "unsafe_next_url"
            break
        if pagination_url_key(next_url) in seen_urls:
            result.pagination_stop_reason = "repeated_url"
            break

        next_outcome = await _fetch_follow_on_page(next_url, **fetch_kwargs)
        final_url = next_outcome.debug.get("final_url") or next_url
        final_key = pagination_url_key(final_url)
        if final_key in seen_urls:
            result.pagination_stop_reason = "repeated_url"
            break
        seen_urls.add(final_key)
        page = next_outcome.page
        page_data = await _run_cpu_work(
            extract_page_data,
            page,
            target,
            budget=budget,
        )
        if not page_data:
            result.pages_scraped += 1
            result.pagination_stop_reason = "exhausted"
            break
        fingerprint = page_data_fingerprint(page_data)
        if fingerprint in seen_pages:
            # Past the end, some listings serve an earlier page again.
            result.pagination_stop_reason = "repeated_page"
            break
        seen_pages.add(fingerprint)
        result.data.extend(page_data)
        result.pages_scraped += 1
        await cancellation_checkpoint(
            cancellation_guard,
            "after_pagination_page",
        )


async def _extract_click_pages(
    *,
    page: object,
    target: TargetConfig,
    result: TargetResult,
    extract_page_data: ExtractPageDataCallable,
    budget: RunBudget | None,
    cancellation_guard: CancellationCheckpoint | None,
) -> None:
    click_result = getattr(page, CLICK_PAGINATION_ATTRIBUTE, None)
    if not isinstance(click_result, ClickPaginationResult):
        result.pagination_stop_reason = "unknown"
        return
    for snapshot in click_result.pages:
        await cancellation_checkpoint(
            cancellation_guard,
            "before_pagination_page",
        )
        if budget is not None:
            budget.check_deadline()
        page_data = await _run_cpu_work(
            extract_page_data,
            snapshot,
            target,
            budget=budget,
        )
        result.data.extend(page_data)
        result.pages_scraped += 1
        await cancellation_checkpoint(
            cancellation_guard,
            "after_pagination_page",
        )
    result.pagination_stop_reason = cast(Any, click_result.stop_reason)
