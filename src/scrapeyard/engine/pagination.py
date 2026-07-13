"""Pagination helpers for scraper target processing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from scrapeyard.common.budgets import RunBudget
from scrapeyard.config.schema import TargetConfig
from scrapeyard.engine.scrape_models import FetchOutcome, TargetResult
from scrapeyard.engine.selectors import select_elements_strict
from scrapeyard.engine.url_guard import UnsafeURLError, assert_public_url
from scrapeyard.queue.cancellation import (
    CancellationCheckpoint,
    cancellation_checkpoint,
)

FetchTargetPageCallable = Callable[..., Awaitable[FetchOutcome]]
ExtractPageDataCallable = Callable[[object, TargetConfig], list[dict[str, Any]]]


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
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"

    netloc = hostname
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"

    port = parsed.port
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{netloc}:{port}"

    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


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
        lookup = asyncio.to_thread(assert_public_url, url, allow_unresolved=False)
        if budget is None:
            await lookup
        else:
            await budget.wait_for(lookup)
    except UnsafeURLError:
        return False
    await cancellation_checkpoint(
        cancellation_guard,
        "after_pagination_dns_validation",
    )
    if budget is not None:
        budget.check_deadline()
    return True


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
) -> None:
    if target.pagination is None:
        return

    current_url = (result.debug.get("final_url") if result.debug else None) or target.url
    seen_urls = {pagination_url_key(current_url)}
    next_selector = target.pagination.next
    for _ in range(target.pagination.max_pages - 1):
        await cancellation_checkpoint(
            cancellation_guard,
            "before_pagination_page",
        )
        if budget is not None:
            budget.check_deadline()
        next_links = select_elements_strict(
            page,
            next_selector,
            operation="select_pagination_next",
        )
        if not next_links:
            break
        next_url = resolve_href(next_links[0], current_url)
        if not next_url:
            break
        if not await _pagination_url_is_safe(
            next_url,
            budget=budget,
            cancellation_guard=cancellation_guard,
        ):
            break
        if pagination_url_key(next_url) in seen_urls:
            break

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
        if budget is None and cancellation_guard is None:
            next_outcome = await fetch_target_page(*fetch_args)
        elif cancellation_guard is None:
            next_outcome = await fetch_target_page(*fetch_args, budget)
        else:
            next_outcome = await fetch_target_page(
                *fetch_args,
                budget,
                cancellation_guard,
            )
        await cancellation_checkpoint(
            cancellation_guard,
            "after_pagination_fetch",
        )
        final_url = next_outcome.debug.get("final_url") or next_url
        final_key = pagination_url_key(final_url)
        if final_key in seen_urls:
            break
        seen_urls.add(final_key)
        page = next_outcome.page
        current_url = final_url
        page_data = extract_page_data(page, target)
        if budget is not None:
            await budget.consume_extracted_records(len(page_data))
        result.data.extend(page_data)
        result.pages_scraped += 1
        await cancellation_checkpoint(
            cancellation_guard,
            "after_pagination_page",
        )
