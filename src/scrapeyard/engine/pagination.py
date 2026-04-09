"""Pagination helpers for scraper target processing."""

from __future__ import annotations

from typing import Any, Awaitable, Callable
from urllib.parse import urljoin

from scrapeyard.config.schema import TargetConfig
from scrapeyard.engine.scrape_models import TargetResult

FetchTargetPageCallable = Callable[..., Awaitable[Any]]
ExtractPageDataCallable = Callable[[Any, TargetConfig], list[dict[str, Any]]]


def resolve_href(element: Any, base_url: str) -> str | None:
    """Return an absolute URL for a next-page element or None."""
    href = element.attrib.get("href") if hasattr(element, "attrib") else None
    if href is None:
        try:
            href = element.attributes.get("href")
        except AttributeError:
            return None
    if not href:
        return None
    if not isinstance(href, str):
        href = str(href)
    return urljoin(base_url, href)


async def paginate_target(
    *,
    page: Any,
    target: TargetConfig,
    result: TargetResult,
    fetch_target_page: FetchTargetPageCallable,
    extract_page_data: ExtractPageDataCallable,
    retry_handler: Any,
    fetcher_cls: Any,
    adaptive: bool,
    retryable_status: set[int],
    adaptive_dir: str,
    proxy_url: str | None,
    artifacts_dir: str | None,
) -> None:
    if target.pagination is None:
        return

    current_url = result.debug.get("final_url") or target.url if result.debug else target.url
    next_selector = target.pagination.next
    for _ in range(target.pagination.max_pages - 1):
        next_links = page.css(next_selector)
        if not next_links:
            break
        next_url = resolve_href(next_links[0], current_url)
        if not next_url:
            break

        next_outcome = await fetch_target_page(
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
        page = next_outcome.page
        current_url = next_outcome.debug.get("final_url") or next_url
        result.data.extend(extract_page_data(page, target))
        result.pages_scraped += 1
