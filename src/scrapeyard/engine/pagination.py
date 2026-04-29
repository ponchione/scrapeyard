"""Pagination helpers for scraper target processing."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

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

    current_url = (result.debug.get("final_url") if result.debug else None) or target.url
    seen_urls = {pagination_url_key(current_url)}
    next_selector = target.pagination.next
    for _ in range(target.pagination.max_pages - 1):
        next_links = page.css(next_selector)
        if not next_links:
            break
        next_url = resolve_href(next_links[0], current_url)
        if not next_url:
            break
        if pagination_url_key(next_url) in seen_urls:
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
        final_url = next_outcome.debug.get("final_url") or next_url
        final_key = pagination_url_key(final_url)
        if final_key in seen_urls:
            break
        seen_urls.add(final_key)
        page = next_outcome.page
        current_url = final_url
        result.data.extend(extract_page_data(page, target))
        result.pages_scraped += 1
