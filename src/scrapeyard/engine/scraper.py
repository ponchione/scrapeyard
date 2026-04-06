"""Scrapling-based scraping engine — fetches URLs, applies selectors, handles pagination."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from scrapling import Fetcher, PlayWrightFetcher, StealthyFetcher

from scrapeyard.common.settings import get_settings
from scrapeyard.config.schema import BrowserConfig, FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.detection import enrich_item_detection
from scrapeyard.engine.resilience import RetryHandler, RetryableError
from scrapeyard.engine.selectors import count_selector_matches, extract_selectors, select_items
from scrapeyard.models.job import ErrorType

logger = logging.getLogger(__name__)

_BROWSER_TIMEOUT_MS = 60000
_HTML_EXCERPT_CHARS = 2000

_CHALLENGE_MARKERS = (
    "captcha",
    "cf-challenge",
    "challenge page",
    "verify you are human",
    "press and hold",
    "security check",
    "bot verification",
)
_CONSENT_MARKERS = (
    "cookie consent",
    "consent",
    "privacy settings",
    "accept cookies",
    "your privacy choices",
)
_LOGIN_MARKERS = (
    "sign in",
    "log in",
    "my account",
    "login required",
)
_BLOCK_MARKERS = (
    "access denied",
    "access blocked",
    "temporarily blocked",
    "request blocked",
    "forbidden",
)
_PROXY_MARKERS = (
    "proxy",
    "407",
    "tunnel",
    "authentication required",
    "econnrefused",
    "connection refused",
)
_BROWSER_ERROR_MARKERS = (
    "playwright",
    "browser",
    "page.goto",
    "target page",
    "executable",
    "chromium",
    "webkit",
    "firefox",
)
_NETWORK_ERROR_MARKERS = (
    "connection",
    "dns",
    "tls",
    "ssl",
    "certificate",
    "network",
    "socket",
)


def _extract_page_data(page: Any, target: TargetConfig) -> list[dict[str, Any]]:
    """Extract records and enrich with pricing visibility and stock status detection."""
    if target.item_selector is not None:
        items = select_items(page, target.item_selector)
        data = [extract_selectors(item, target.selectors) for item in items]
    else:
        items = [page]
        data = [extract_selectors(page, target.selectors)]

    for item_data, element in zip(data, items, strict=False):
        enrich_item_detection(
            item_data, element, target.map_detection, target.stock_detection,
        )

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
    debug: dict[str, Any] | None = None


@dataclass
class FetchOutcome:
    page: Any
    debug: dict[str, Any]


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

    def __init__(self, status: int, debug: dict[str, Any] | None = None) -> None:
        self.status = status
        self.debug = debug
        super().__init__(f"HTTP {status}")


def _token_match(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _classify_fetch_exception(
    exc: Exception,
    fetcher_type: FetcherType,
) -> tuple[ErrorType, int | None, dict[str, Any] | None]:
    """Map low-level fetch exceptions to structured Scrapeyard error types."""
    debug = getattr(exc, "debug", None)
    if isinstance(exc, RetryableError):
        if exc.status == 404:
            return ErrorType.http_not_found, exc.status, debug
        if exc.status in {401, 403, 429}:
            return ErrorType.blocked_response, exc.status, debug
        return ErrorType.http_error, exc.status, debug
    if isinstance(exc, FetchError):
        if exc.status == 404:
            return ErrorType.http_not_found, exc.status, exc.debug
        if exc.status in {401, 403, 429}:
            if exc.debug:
                signal = _classify_page_signals(exc.debug)
                if signal in {
                    ErrorType.challenge_page,
                    ErrorType.consent_gate,
                    ErrorType.login_gate,
                }:
                    return signal, exc.status, exc.debug
            return ErrorType.blocked_response, exc.status, exc.debug
        return ErrorType.http_error, exc.status, exc.debug
    if isinstance(exc, asyncio.TimeoutError):
        return (
            ErrorType.navigation_timeout if fetcher_type != FetcherType.basic else ErrorType.timeout,
            None,
            debug,
        )

    detail = f"{type(exc).__name__}: {exc}".lower()
    if _token_match(detail, _PROXY_MARKERS):
        return ErrorType.proxy_rejected, None, debug
    if fetcher_type != FetcherType.basic and _token_match(detail, _BROWSER_ERROR_MARKERS):
        return ErrorType.browser_error, None, debug
    if isinstance(exc, (ConnectionError, OSError)) or _token_match(detail, _NETWORK_ERROR_MARKERS):
        return ErrorType.network_error, None, debug

    return ErrorType.http_error, None, debug


def _default_debug_blob(
    fetcher_type: FetcherType,
    target: TargetConfig,
    url: str,
) -> dict[str, Any]:
    browser = _target_browser_config(target)
    return {
        "fetcher": fetcher_type.value,
        "final_url": url,
        "page_title": None,
        "main_document_status": None,
        "item_selector_count": None,
        "selector_counts": {},
        "html_excerpt": None,
        "screenshot_path": None,
        "browser_settings": {
            "timeout_ms": browser.timeout_ms,
            "disable_resources": browser.disable_resources,
            "network_idle": browser.network_idle,
            "stealth": browser.stealth,
            "hide_canvas": browser.hide_canvas,
            "useragent": browser.useragent,
            "extra_headers": browser.extra_headers,
            "click_selector": browser.click_selector,
            "click_timeout_ms": browser.click_timeout_ms,
            "click_wait_ms": browser.click_wait_ms,
            "wait_for_selector": browser.wait_for_selector,
            "wait_ms": browser.wait_ms,
        },
    }


def _target_browser_config(target: TargetConfig) -> BrowserConfig:
    return target.browser or BrowserConfig()


def _browser_fetch_kwargs(
    target: TargetConfig,
    *,
    proxy_url: str | None,
) -> dict[str, Any]:
    """Build browser-specific fetch kwargs from target config."""
    browser = _target_browser_config(target)
    kwargs: dict[str, Any] = {
        "timeout": browser.timeout_ms,
        "disable_resources": browser.disable_resources,
        "network_idle": browser.network_idle,
        "stealth": browser.stealth,
        "hide_canvas": browser.hide_canvas,
    }
    if proxy_url is not None:
        kwargs["proxy"] = proxy_url
    if browser.useragent:
        kwargs["useragent"] = browser.useragent
    if browser.extra_headers:
        kwargs["extra_headers"] = browser.extra_headers
    if browser.wait_for_selector:
        kwargs["wait_selector"] = browser.wait_for_selector
    if browser.wait_ms is not None:
        kwargs["wait"] = browser.wait_ms
    return kwargs


def _coerce_to_text(value: Any) -> str:
    """Coerce None, bytes, or arbitrary values into a plain string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _truncate_text(value: str, limit: int = _HTML_EXCERPT_CHARS) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _response_text(page: Any) -> str:
    text = _coerce_to_text(getattr(page, "text", None) or getattr(page, "body", None))
    if text:
        return text
    return _coerce_to_text(page)


def _response_title(page: Any) -> str | None:
    value = getattr(page, "title", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    html = _response_text(page)
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return _truncate_text(match.group(1), 300)
    return None


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


def _missing_adaptive_selectors(
    target: TargetConfig,
    data: list[dict[str, Any]],
) -> list[str]:
    """Return selector names whose extracted values are entirely empty."""
    if not data:
        return list(target.selectors.keys())
    missing: list[str] = []
    for key in target.selectors:
        values = [record.get(key) for record in data]
        if all(value is None or value == "" or value == [] for value in values):
            missing.append(key)
    return missing


def _log_adaptive_selector_gap(target: TargetConfig, data: list[dict[str, Any]]) -> None:
    missing = _missing_adaptive_selectors(target, data)
    if missing:
        logger.info(
            "Adaptive relocation check: url=%s missing_selectors=%s",
            target.url,
            ",".join(missing),
        )


def _has_extracted_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_extracted_value(item) for item in value)
    return True


def _classify_page_signals(debug: dict[str, Any]) -> ErrorType | None:
    blob = "\n".join(
        part.lower() for part in (
            _coerce_to_text(debug.get("page_title")),
            _coerce_to_text(debug.get("html_excerpt")),
        ) if part
    )
    if not blob:
        return None
    if _token_match(blob, _CHALLENGE_MARKERS):
        return ErrorType.challenge_page
    if _token_match(blob, _CONSENT_MARKERS):
        return ErrorType.consent_gate
    if _token_match(blob, _LOGIN_MARKERS):
        return ErrorType.login_gate
    if _token_match(blob, _BLOCK_MARKERS):
        return ErrorType.blocked_response
    return None


def _classify_rendered_outcome(
    debug: dict[str, Any],
    data: list[dict[str, Any]],
    *,
    has_item_selector: bool,
) -> ErrorType | None:
    signal_classification = _classify_page_signals(debug)
    if signal_classification is not None:
        return signal_classification

    selector_counts = debug.get("selector_counts") or {}
    item_selector_count = debug.get("item_selector_count")

    if has_item_selector:
        if item_selector_count is None:
            return None
        if item_selector_count == 0:
            return ErrorType.rendered_empty
        if not any(count > 0 for count in selector_counts.values()):
            return ErrorType.selector_miss
        return None

    if selector_counts and not any(count > 0 for count in selector_counts.values()):
        return ErrorType.rendered_empty
    if any(_has_extracted_value(value) for row in data for value in row.values()):
        return None
    if selector_counts and any(count > 0 for count in selector_counts.values()):
        return ErrorType.selector_miss
    return ErrorType.rendered_empty


async def _capture_browser_state(
    page: Any,
    *,
    browser: Any,
    fetcher_type: FetcherType,
    artifacts_dir: str | None,
    capture: dict[str, Any],
) -> Any:
    if browser is not None and browser.click_selector:
        try:
            await page.locator(browser.click_selector).click(timeout=browser.click_timeout_ms)
            if browser.click_wait_ms is not None:
                await page.wait_for_timeout(browser.click_wait_ms)
        except Exception:
            logger.info(
                "Optional browser click_selector did not resolve or click: %s",
                browser.click_selector,
            )
    capture["final_url"] = getattr(page, "url", None)
    try:
        capture["page_title"] = await page.title()
    except Exception:
        capture["page_title"] = None
    try:
        capture["html_excerpt"] = _truncate_text(await page.content())
    except Exception:
        capture["html_excerpt"] = None
    if artifacts_dir is not None:
        artifacts_path = Path(artifacts_dir)
        artifacts_path.mkdir(parents=True, exist_ok=True)
        screenshot_path = artifacts_path / f"{fetcher_type.value}-main.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            capture["screenshot_path"] = str(screenshot_path)
        except Exception:
            capture["screenshot_path"] = None
    return page


async def _fetch_basic_response(fetcher_cls: Any, url: str, call_kwargs: dict[str, Any]) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: fetcher_cls.get(url, **call_kwargs),
    )


async def _fetch_browser_response(
    fetcher_cls: Any,
    url: str,
    target: TargetConfig,
    fetcher_type: FetcherType,
    call_kwargs: dict[str, Any],
    artifacts_dir: str | None,
) -> tuple[Any, dict[str, Any]]:
    capture: dict[str, Any] = {}

    async def _page_action(page: Any) -> Any:
        return await _capture_browser_state(
            page,
            browser=target.browser,
            fetcher_type=fetcher_type,
            artifacts_dir=artifacts_dir,
            capture=capture,
        )

    call_kwargs["page_action"] = _page_action
    response = await fetcher_cls.async_fetch(url, **call_kwargs)
    return response, capture


def _populate_fetch_debug(debug: dict[str, Any], response: Any, url: str) -> None:
    debug["main_document_status"] = getattr(response, "status", None)
    debug["final_url"] = debug.get("final_url") or getattr(response, "url", None) or url
    debug["page_title"] = debug.get("page_title") or _response_title(response)
    debug["html_excerpt"] = debug.get("html_excerpt") or (_truncate_text(_response_text(response)) or None)


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
    """Fetch a single page using the appropriate Scrapling method.

    Raises :class:`RetryableError` for retryable HTTP status codes,
    :class:`FetchError` for other error statuses.
    """
    call_kwargs = _adaptive_fetch_kwargs(
        target,
        adaptive=adaptive,
        adaptive_dir=adaptive_dir,
    )
    debug = _default_debug_blob(fetcher_type, target, url)

    if fetcher_type == FetcherType.basic:
        if proxy_url is not None:
            call_kwargs["proxy"] = proxy_url
        response = await _fetch_basic_response(fetcher_cls, url, call_kwargs)
    else:
        call_kwargs.update(_browser_fetch_kwargs(target, proxy_url=proxy_url))
        response, capture = await _fetch_browser_response(
            fetcher_cls,
            url,
            target,
            fetcher_type,
            call_kwargs,
            artifacts_dir,
        )
        debug.update(capture)

    _populate_fetch_debug(debug, response, url)

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


async def _paginate(
    *,
    page: Any,
    target: TargetConfig,
    result: TargetResult,
    retry_handler: RetryHandler,
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
        next_url = _resolve_href(next_links[0], current_url)
        if not next_url:
            break

        next_outcome = await _fetch_target_page(
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
        result.data.extend(_extract_page_data(page, target))
        result.pages_scraped += 1


def _finalize_target_success(result: TargetResult, target: TargetConfig) -> None:
    rendered_classification = _classify_rendered_outcome(
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
            _log_adaptive_selector_gap(target, page_data)
        result.data.extend(page_data)
        result.pages_scraped = 1
        await _paginate(
            page=page,
            target=target,
            result=result,
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
        error_type, http_status, debug = _classify_fetch_exception(exc, target.fetcher)
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
        result.debug = debug or _default_debug_blob(target.fetcher, target, target.url)
        result.debug.setdefault("classification", error_type.value)
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
    if not isinstance(href, str):
        href = str(href)
    return urljoin(base_url, href)
