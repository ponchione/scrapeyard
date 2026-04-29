"""Browser fetch instrumentation and debug blob helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from functools import cache
from pathlib import Path
from typing import Any

from scrapling import PlayWrightFetcher, StealthyFetcher

from scrapeyard.config.schema import BrowserConfig, FetcherType, TargetConfig

logger = logging.getLogger(__name__)

_HTML_EXCERPT_CHARS = 2000
_EVENT_TEXT_CHARS = 300
_MAX_CONSOLE_MESSAGES = 20
_MAX_REQUEST_FAILURES = 20
_BROWSER_FETCH_KWARGS = {
    "timeout_ms": "timeout",
    "disable_resources": "disable_resources",
    "network_idle": "network_idle",
    "stealth": "stealth",
    "hide_canvas": "hide_canvas",
    "real_chrome": "real_chrome",
    "nstbrowser_mode": "nstbrowser_mode",
    "useragent": "useragent",
    "extra_headers": "extra_headers",
    "cdp_url": "cdp_url",
    "humanize": "humanize",
    "os_randomize": "os_randomize",
    "geoip": "geoip",
    "disable_ads": "disable_ads",
    "additional_arguments": "additional_arguments",
    "wait_for_selector": "wait_selector",
    "wait_ms": "wait",
}
_ALWAYS_SEND_BROWSER_FIELDS = {
    "timeout_ms",
    "disable_resources",
    "network_idle",
    "stealth",
    "hide_canvas",
    "real_chrome",
    "nstbrowser_mode",
}
_SEND_WHEN_NOT_NONE_BROWSER_FIELDS = {"humanize", "wait_ms"}


def _bounded_append(items: list[dict[str, Any]], entry: dict[str, Any], *, limit: int) -> None:
    items.append(entry)
    if len(items) > limit:
        del items[: len(items) - limit]


def _safe_text_attr(value: Any, attr: str) -> str | None:
    try:
        raw = getattr(value, attr, None)
    except Exception:
        return None
    if raw is None:
        return None
    if callable(raw):
        try:
            raw = raw()
        except Exception:
            return None
    text = truncate_text(coerce_to_text(raw), _EVENT_TEXT_CHARS)
    return text or None


def _register_console_capture(page: Any, capture: dict[str, Any]) -> None:
    if not hasattr(page, "on"):
        return

    def _on_console(message: Any) -> None:
        entry = {
            "type": _safe_text_attr(message, "type") or "unknown",
            "text": _safe_text_attr(message, "text") or "",
        }
        _bounded_append(capture["console_messages"], entry, limit=_MAX_CONSOLE_MESSAGES)

    try:
        page.on("console", _on_console)
    except Exception as exc:
        logger.debug("Failed to register browser console capture: %s: %s", type(exc).__name__, exc)


def _register_request_failure_capture(page: Any, capture: dict[str, Any]) -> None:
    if not hasattr(page, "on"):
        return

    def _on_request_failed(request: Any) -> None:
        failure = None
        try:
            failure = request.failure() if hasattr(request, "failure") else None
        except Exception:
            failure = None
        entry = {
            "url": _safe_text_attr(request, "url") or "",
            "method": _safe_text_attr(request, "method") or "unknown",
            "resource_type": _safe_text_attr(request, "resource_type") or "unknown",
            "error_text": _safe_text_attr(failure, "error_text") or "",
        }
        _bounded_append(capture["request_failures"], entry, limit=_MAX_REQUEST_FAILURES)

    try:
        page.on("requestfailed", _on_request_failed)
    except Exception as exc:
        logger.debug("Failed to register browser requestfailed capture: %s: %s", type(exc).__name__, exc)


def default_debug_blob(fetcher_type: FetcherType, target: TargetConfig, url: str) -> dict[str, Any]:
    browser = target_browser_config(target)
    return {
        "fetcher": fetcher_type.value,
        "final_url": url,
        "page_title": None,
        "main_document_status": None,
        "item_selector_count": None,
        "selector_counts": {},
        "html_excerpt": None,
        "screenshot_path": None,
        "console_messages": [],
        "request_failures": [],
        "browser_settings": browser.model_dump(),
    }


def target_browser_config(target: TargetConfig) -> BrowserConfig:
    return target.browser or BrowserConfig()


@cache
def _supported_fetcher_kwargs(fetcher_type: FetcherType) -> set[str]:
    fetcher_cls = {
        FetcherType.dynamic: PlayWrightFetcher,
        FetcherType.stealthy: StealthyFetcher,
    }.get(fetcher_type)
    if fetcher_cls is None:
        return set()
    return set(inspect.signature(fetcher_cls.async_fetch).parameters)


def browser_fetch_kwargs(target: TargetConfig, fetcher_type: FetcherType, *, proxy_url: str | None) -> dict[str, Any]:
    """Build browser-specific fetch kwargs from target config, filtered to the fetcher signature."""
    browser = target_browser_config(target)
    browser_values = browser.model_dump()
    kwargs: dict[str, Any] = {}
    for field_name, kwarg_name in _BROWSER_FETCH_KWARGS.items():
        value = browser_values[field_name]
        if (
            field_name in _ALWAYS_SEND_BROWSER_FIELDS
            or (field_name in _SEND_WHEN_NOT_NONE_BROWSER_FIELDS and value is not None)
            or value
        ):
            kwargs[kwarg_name] = value
    if proxy_url is not None:
        kwargs["proxy"] = proxy_url

    supported_kwargs = _supported_fetcher_kwargs(fetcher_type)
    return {key: value for key, value in kwargs.items() if key in supported_kwargs}


def coerce_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def truncate_text(value: str, limit: int = _HTML_EXCERPT_CHARS) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def response_text(page: Any) -> str:
    text = coerce_to_text(getattr(page, "text", None) or getattr(page, "body", None))
    if text:
        return text
    return coerce_to_text(page)


def response_title(page: Any) -> str | None:
    value = getattr(page, "title", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    html = response_text(page)
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return truncate_text(match.group(1), 300)
    return None


async def capture_browser_state(
    page: Any,
    *,
    browser: Any,
    fetcher_type: FetcherType,
    artifacts_dir: str | None,
    capture: dict[str, Any],
) -> Any:
    capture.setdefault("console_messages", [])
    capture.setdefault("request_failures", [])
    _register_console_capture(page, capture)
    _register_request_failure_capture(page, capture)
    if browser is not None and browser.click_selector:
        try:
            await page.locator(browser.click_selector).click(timeout=browser.click_timeout_ms)
            if browser.click_wait_ms is not None:
                await page.wait_for_timeout(browser.click_wait_ms)
        except Exception as exc:
            logger.info(
                "Optional browser click_selector did not resolve or click: %s (%s: %s)",
                browser.click_selector,
                type(exc).__name__,
                exc,
            )
    capture["final_url"] = getattr(page, "url", None)
    try:
        capture["page_title"] = await page.title()
    except Exception as exc:
        logger.debug("Failed to capture browser page title: %s: %s", type(exc).__name__, exc, exc_info=exc)
        capture["page_title"] = None
    try:
        capture["html_excerpt"] = truncate_text(await page.content())
    except Exception as exc:
        logger.debug("Failed to capture browser HTML excerpt: %s: %s", type(exc).__name__, exc, exc_info=exc)
        capture["html_excerpt"] = None
    if artifacts_dir is not None:
        artifacts_path = Path(artifacts_dir)
        artifacts_path.mkdir(parents=True, exist_ok=True)
        screenshot_path = artifacts_path / f"{fetcher_type.value}-main.png"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
            capture["screenshot_path"] = str(screenshot_path)
        except Exception as exc:
            logger.debug(
                "Failed to capture browser screenshot at %s: %s: %s",
                screenshot_path,
                type(exc).__name__,
                exc,
                exc_info=exc,
            )
            capture["screenshot_path"] = None
    return page


async def fetch_basic_response(fetcher_cls: Any, url: str, call_kwargs: dict[str, Any]) -> Any:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fetcher_cls.get(url, **call_kwargs))


async def fetch_browser_response(
    fetcher_cls: Any,
    url: str,
    target: TargetConfig,
    fetcher_type: FetcherType,
    call_kwargs: dict[str, Any],
    artifacts_dir: str | None,
) -> tuple[Any, dict[str, Any]]:
    capture: dict[str, Any] = {}

    async def _page_action(page: Any) -> Any:
        return await capture_browser_state(
            page,
            browser=target.browser,
            fetcher_type=fetcher_type,
            artifacts_dir=artifacts_dir,
            capture=capture,
        )

    call_kwargs["page_action"] = _page_action
    response = await fetcher_cls.async_fetch(url, **call_kwargs)
    return response, capture


def populate_fetch_debug(debug: dict[str, Any], response: Any, url: str) -> None:
    debug["main_document_status"] = getattr(response, "status", None)
    debug["final_url"] = debug.get("final_url") or getattr(response, "url", None) or url
    debug["page_title"] = debug.get("page_title") or response_title(response)
    debug["html_excerpt"] = debug.get("html_excerpt") or (truncate_text(response_text(response)) or None)
