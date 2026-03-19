"""Scrapling-based scraping engine — fetches URLs, applies selectors, handles pagination."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from scrapling import Fetcher, PlayWrightFetcher, StealthyFetcher

from scrapeyard.common.settings import get_settings
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.resilience import RetryHandler, RetryableError
from scrapeyard.engine.selectors import extract_item_selectors, extract_selectors
from scrapeyard.models.job import ErrorType

logger = logging.getLogger(__name__)

_BROWSER_TIMEOUT_MS = 60000


def _extract_page_data(page: Any, target: TargetConfig) -> list[dict[str, Any]]:
    """Extract either a single page-wide record or multiple item-scoped records."""
    if target.item_selector is not None:
        return extract_item_selectors(page, target.item_selector, target.selectors)
    return [extract_selectors(page, target.selectors)]


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
    """Return parser kwargs for Scrapling adaptive matching.

    Scrapling 0.2.x exposes adaptive parser configuration via ``custom_config``
    across both basic and browser-backed fetchers. Browser fetchers do not
    accept legacy kwargs such as ``auto_save``.
    """
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


@dataclass
class TargetResult:
    """Result of scraping a single target URL."""

    url: str
    status: str = "failed"  # "success" or "failed"
    data: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_scraped: int = 0
    error_type: ErrorType | None = None
    http_status: int | None = None
    error_detail: str | None = None


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


def _classify_fetch_exception(exc: Exception, fetcher_type: FetcherType) -> tuple[ErrorType, int | None]:
    """Map low-level fetch exceptions to structured Scrapeyard error types."""
    if isinstance(exc, RetryableError):
        return ErrorType.http_error, exc.status
    if isinstance(exc, FetchError):
        return ErrorType.http_error, exc.status
    if isinstance(exc, asyncio.TimeoutError):
        return ErrorType.timeout, None

    detail = f"{type(exc).__name__}: {exc}".lower()
    if fetcher_type != FetcherType.basic and any(
        token in detail
        for token in (
            "playwright",
            "browser",
            "page.goto",
            "target page",
            "executable",
            "chromium",
            "webkit",
            "firefox",
        )
    ):
        return ErrorType.browser_error, None

    if isinstance(exc, (ConnectionError, OSError)) or any(
        token in detail
        for token in (
            "connection",
            "dns",
            "tls",
            "ssl",
            "certificate",
            "network",
            "socket",
        )
    ):
        return ErrorType.network_error, None

    return ErrorType.http_error, None


async def _fetch_page(
    fetcher_cls: Any,
    url: str,
    target: TargetConfig,
    fetcher_type: FetcherType,
    adaptive: bool,
    retryable_status: set[int],
    adaptive_dir: str,
) -> Any:
    """Fetch a single page using the appropriate Scrapling method.

    Raises :class:`RetryableError` for retryable HTTP status codes,
    :class:`FetchError` for other error statuses.
    """
    call_kwargs = _adaptive_fetch_kwargs(
        target,
        adaptive=adaptive,
        adaptive_dir=adaptive_dir,
    )

    if fetcher_type == FetcherType.basic:
        # Fetcher.get is synchronous — run in thread pool.
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: fetcher_cls.get(url, **call_kwargs),
        )
    else:
        # Browser-backed fetchers need more headroom than simple HTTP fetches,
        # and dropping non-essential resources reduces load-event stalls on
        # heavy retail pages.
        browser = target.browser
        call_kwargs.setdefault(
            "timeout",
            browser.timeout_ms if browser is not None else _BROWSER_TIMEOUT_MS,
        )
        call_kwargs.setdefault(
            "disable_resources",
            True if browser is None else browser.disable_resources,
        )
        call_kwargs.setdefault(
            "network_idle",
            False if browser is None else browser.network_idle,
        )
        # StealthyFetcher / PlayWrightFetcher have async_fetch.
        response = await fetcher_cls.async_fetch(url, **call_kwargs)

    if response.status and response.status >= 400:
        if response.status in retryable_status:
            raise RetryableError(response.status)
        raise FetchError(response.status)

    return response


async def scrape_target(
    target: TargetConfig,
    adaptive: bool,
    retry: RetryConfig,
    adaptive_dir: str | None = None,
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

    if adaptive_dir is None:
        adaptive_dir = get_settings().adaptive_dir

    # Ensure adaptive directory exists.
    Path(adaptive_dir).mkdir(parents=True, exist_ok=True)

    fetcher_cls = _get_fetcher(target.fetcher)
    retry_handler = RetryHandler(retry)
    retryable_status = set(retry.retryable_status)

    try:
        page = await retry_handler.execute(
            _fetch_page, fetcher_cls, target.url, target, target.fetcher, adaptive, retryable_status, adaptive_dir
        )
        data = _extract_page_data(page, target)
        if adaptive:
            missing = []
            if not data:
                missing = list(target.selectors.keys())
            else:
                for key in target.selectors:
                    values = [record.get(key) for record in data]
                    if all(value is None or value == "" or value == [] for value in values):
                        missing.append(key)
            if missing:
                logger.info(
                    "Adaptive relocation check: url=%s missing_selectors=%s",
                    target.url,
                    ",".join(missing),
                )
        result.data.extend(data)
        result.pages_scraped = 1

        # Pagination — resolve each "next" href against the *current* page URL,
        # not the original target URL, so relative links work across pages.
        if target.pagination:
            max_pages = target.pagination.max_pages
            next_selector = target.pagination.next
            current_url = target.url
            for _ in range(max_pages - 1):
                next_links = page.css(next_selector)
                if not next_links:
                    break
                next_el = next_links[0]
                next_url = _resolve_href(next_el, current_url)
                if not next_url:
                    break

                page = await retry_handler.execute(
                    _fetch_page, fetcher_cls, next_url, target, target.fetcher, adaptive, retryable_status, adaptive_dir
                )
                current_url = next_url
                data = _extract_page_data(page, target)
                result.data.extend(data)
                result.pages_scraped += 1

        result.status = "success"

    except Exception as exc:
        error_type, http_status = _classify_fetch_exception(exc, target.fetcher)
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        logger.exception(
            "Scrape target failed: url=%s fetcher=%s classified_as=%s detail=%s",
            target.url,
            target.fetcher.value,
            error_type.value,
            detail,
        )
        result.status = "failed"
        result.error_type = error_type
        result.http_status = http_status
        result.error_detail = detail
        result.errors.append(detail)

    return result


def _resolve_href(element: Any, base_url: str) -> str | None:
    """Return an absolute URL for a next-page element or None."""
    href = element.attrib.get("href") if hasattr(element, "attrib") else None
    if href is None:
        try:
            href = element.attributes.get("href")
        except AttributeError:
            return None
    if not href:
        return None
    return urljoin(base_url, href)
