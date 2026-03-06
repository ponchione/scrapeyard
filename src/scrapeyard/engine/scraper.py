"""Scrapling-based scraping engine — fetches URLs, applies selectors, handles pagination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from scrapling import Fetcher, PlayWrightFetcher, StealthyFetcher

from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.resilience import RetryHandler, RetryableError
from scrapeyard.engine.selectors import extract_selectors


@dataclass
class TargetResult:
    """Result of scraping a single target URL."""

    url: str
    status: str  # "success" or "failed"
    data: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_scraped: int = 0


def _get_fetcher(fetcher_type: FetcherType) -> Any:
    """Return the Scrapling fetcher class for the given type."""
    mapping = {
        FetcherType.basic: Fetcher,
        FetcherType.stealthy: StealthyFetcher,
        FetcherType.dynamic: PlayWrightFetcher,
    }
    return mapping[fetcher_type]


class FetchError(Exception):
    """Non-retryable HTTP error."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"HTTP {status}")


async def _fetch_page(
    fetcher_cls: Any,
    url: str,
    fetcher_type: FetcherType,
    adaptive: bool,
    retryable_status: set[int],
) -> Any:
    """Fetch a single page using the appropriate Scrapling method.

    Raises :class:`RetryableError` for retryable HTTP status codes,
    :class:`FetchError` for other error statuses.
    """
    custom_config = {"auto_match": adaptive}

    if fetcher_type == FetcherType.basic:
        # Fetcher.get is synchronous — run in thread pool.
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: fetcher_cls.get(url, custom_config=custom_config),
        )
    else:
        # StealthyFetcher / PlayWrightFetcher have async_fetch.
        response = await fetcher_cls.async_fetch(url, custom_config=custom_config)

    if response.status and response.status >= 400:
        if response.status in retryable_status:
            raise RetryableError(response.status)
        raise FetchError(response.status)

    return response


async def scrape_target(
    target: TargetConfig,
    adaptive: bool,
    retry: RetryConfig,
) -> TargetResult:
    """Fetch a URL, apply selectors, and handle pagination.

    Parameters
    ----------
    target:
        Target configuration with URL, fetcher type, selectors, and optional pagination.
    adaptive:
        Whether to enable Scrapling adaptive matching.
    retry:
        Retry configuration passed to :class:`RetryHandler`.
    """
    result = TargetResult(url=target.url)
    fetcher_cls = _get_fetcher(target.fetcher)
    retry_handler = RetryHandler(retry)
    retryable_status = set(retry.retryable_status)

    try:
        page = await retry_handler.execute(
            _fetch_page, fetcher_cls, target.url, target.fetcher, adaptive, retryable_status
        )
        data = extract_selectors(page, target.selectors)
        result.data.append(data)
        result.pages_scraped = 1

        # Pagination
        if target.pagination:
            max_pages = target.pagination.max_pages
            next_selector = target.pagination.next
            for _ in range(max_pages - 1):
                next_links = page.css(next_selector)
                if not next_links:
                    break
                next_el = next_links[0]
                next_url = _resolve_href(next_el, target.url)
                if not next_url:
                    break

                page = await retry_handler.execute(
                    _fetch_page, fetcher_cls, next_url, target.fetcher, adaptive, retryable_status
                )
                data = extract_selectors(page, target.selectors)
                result.data.append(data)
                result.pages_scraped += 1

        result.status = "success"

    except Exception as exc:
        result.status = "failed"
        result.errors.append(str(exc))

    return result


def _resolve_href(element: Any, base_url: str) -> str | None:
    """Extract an href from an element, returning an absolute URL or None."""
    href = element.attrib.get("href") if hasattr(element, "attrib") else None
    if href is None:
        # Try common attribute access patterns.
        try:
            href = element.attributes.get("href")
        except AttributeError:
            return None
    if not href:
        return None
    if href.startswith(("http://", "https://")):
        return href
    # Relative URL — resolve against base.
    parsed = urlparse(base_url)
    if href.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rsplit('/', 1)[0]}/{href}"
