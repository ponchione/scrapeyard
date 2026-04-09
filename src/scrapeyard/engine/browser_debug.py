"""Browser fetch instrumentation and debug blob helpers."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from scrapeyard.config.schema import BrowserConfig, FetcherType, TargetConfig

logger = logging.getLogger(__name__)

_HTML_EXCERPT_CHARS = 2000


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


def target_browser_config(target: TargetConfig) -> BrowserConfig:
    return target.browser or BrowserConfig()


def browser_fetch_kwargs(target: TargetConfig, *, proxy_url: str | None) -> dict[str, Any]:
    """Build browser-specific fetch kwargs from target config."""
    browser = target_browser_config(target)
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
        capture["html_excerpt"] = truncate_text(await page.content())
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
