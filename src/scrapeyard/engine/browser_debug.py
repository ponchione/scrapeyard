"""Browser fetch instrumentation and debug blob helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from contextvars import ContextVar
from functools import cache
from pathlib import Path
from typing import Any

from scrapling import PlayWrightFetcher, StealthyFetcher
from scrapling.engines import camo as scrapling_camo_engine
from scrapling.engines import pw as scrapling_pw_engine
from scrapling.engines.constants import DEFAULT_DISABLED_RESOURCES
from scrapling.engines.toolbelt.navigation import (
    async_intercept_route as scrapling_async_intercept_route,
)

from scrapeyard.config.schema import (
    BROWSER_FETCH_KWARGS,
    BrowserActionConfig,
    BrowserActionType,
    BrowserConfig,
    FetcherType,
    TargetConfig,
)
from scrapeyard.engine.url_guard import UnsafeURLError, assert_public_url, redact_sensitive_mapping
from scrapeyard.engine.url_guard import redact_userinfo_in_text, redact_userinfo_in_url

logger = logging.getLogger(__name__)

_HTML_EXCERPT_CHARS = 2000
_EVENT_TEXT_CHARS = 300
_MAX_CONSOLE_MESSAGES = 20
_MAX_REQUEST_FAILURES = 20
_PAGE_ACTION_EXCEPTION_KEY = "_page_action_exception"


_BROWSER_BLOCK_RESOURCES: ContextVar[bool | None] = ContextVar(
    "scrapeyard_browser_block_resources", default=None,
)
_BROWSER_REQUIRE_RESOLVED_DNS: ContextVar[bool] = ContextVar(
    "scrapeyard_browser_require_resolved_dns", default=False,
)


class BrowserPageActionError(RuntimeError):
    """Raised when Scrapling swallowed a configured browser action failure."""

    def __init__(self, message: str, *, debug: dict[str, Any]) -> None:
        self.debug = debug
        super().__init__(message)


def _exception_text(exc: Exception) -> str:
    return redact_userinfo_in_text(str(exc)) or type(exc).__name__


def _bounded_append(items: list[dict[str, Any]], entry: dict[str, Any], *, limit: int) -> None:
    items.append(entry)
    if len(items) > limit:
        del items[: len(items) - limit]


async def _guarded_async_intercept_route(route: Any) -> None:
    """Scrapling route handler wrapper that blocks unsafe browser requests."""
    block_resources = _BROWSER_BLOCK_RESOURCES.get()
    request = route.request
    request_url = getattr(request, "url", "")
    resource_type = getattr(request, "resource_type", None)

    if block_resources and resource_type in DEFAULT_DISABLED_RESOURCES:
        logger.debug(
            'Blocking background resource "%s" of type "%s"',
            redact_userinfo_in_url(str(request_url)),
            resource_type,
        )
        await route.abort()
        return

    if isinstance(request_url, str) and request_url:
        try:
            await asyncio.to_thread(
                assert_public_url,
                request_url,
                allow_unresolved=not _BROWSER_REQUIRE_RESOLVED_DNS.get(),
            )
        except UnsafeURLError:
            logger.warning(
                "Blocked browser request to non-public URL: %s",
                redact_userinfo_in_url(request_url),
            )
            await route.abort()
            raise

    if block_resources is None:
        await scrapling_async_intercept_route(route)
        return
    await route.continue_()


def _install_browser_route_guard() -> None:
    scrapling_pw_engine.async_intercept_route = _guarded_async_intercept_route
    scrapling_camo_engine.async_intercept_route = _guarded_async_intercept_route


_install_browser_route_guard()


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
    text = truncate_text(redact_userinfo_in_text(coerce_to_text(raw)), _EVENT_TEXT_CHARS)
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
        logger.debug(
            "Failed to register browser console capture: %s: %s",
            type(exc).__name__,
            _exception_text(exc),
        )


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
        logger.debug(
            "Failed to register browser requestfailed capture: %s: %s",
            type(exc).__name__,
            _exception_text(exc),
        )


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
        "browser_settings": redact_sensitive_mapping(browser.model_dump(mode="json")),
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
    browser_values = browser.model_dump(mode="json")
    kwargs: dict[str, Any] = {}
    for mapping in BROWSER_FETCH_KWARGS:
        value = browser_values[mapping.field_name]
        if mapping.should_send(value):
            kwargs[mapping.kwarg_name] = value
    if proxy_url is not None:
        kwargs["proxy"] = proxy_url

    supported_kwargs = _supported_fetcher_kwargs(fetcher_type)
    return {key: value for key, value in kwargs.items() if key in supported_kwargs}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close_page_safely(page: Any) -> None:
    close = getattr(page, "close", None)
    if not callable(close):
        return
    try:
        await _maybe_await(close())
    except Exception as exc:
        logger.debug(
            "Failed to close unsafe browser page: %s: %s",
            type(exc).__name__,
            _exception_text(exc),
        )


async def _click_selector(page: Any, selector: str, timeout_ms: int | None) -> None:
    locator = page.locator(selector)
    kwargs = {} if timeout_ms is None else {"timeout": timeout_ms}
    await _maybe_await(locator.click(**kwargs))


async def _wait_for_selector(page: Any, selector: str, timeout_ms: int | None) -> None:
    kwargs = {} if timeout_ms is None else {"timeout": timeout_ms}
    wait_for_selector = getattr(page, "wait_for_selector", None)
    if callable(wait_for_selector):
        await _maybe_await(wait_for_selector(selector, **kwargs))
        return
    locator = page.locator(selector)
    await _maybe_await(locator.wait_for(**kwargs))


async def _wait_for_timeout(page: Any, wait_ms: int) -> None:
    await _maybe_await(page.wait_for_timeout(wait_ms))


async def _scroll_once(page: Any, pixels: int) -> None:
    mouse = getattr(page, "mouse", None)
    wheel = getattr(mouse, "wheel", None)
    if callable(wheel):
        await _maybe_await(wheel(0, pixels))
        return
    evaluate = getattr(page, "evaluate", None)
    if callable(evaluate):
        await _maybe_await(evaluate("(distance) => window.scrollBy(0, distance)", pixels))
        return
    raise AttributeError("Page does not support mouse wheel or evaluate scrolling")


async def _run_post_action_waits(page: Any, action: BrowserActionConfig) -> None:
    if action.wait_for_selector:
        await _wait_for_selector(page, action.wait_for_selector, action.timeout_ms)
    if action.wait_ms is not None:
        await _wait_for_timeout(page, action.wait_ms)


async def _run_browser_action(page: Any, action: BrowserActionConfig) -> None:
    if action.type == BrowserActionType.click:
        await _click_selector(page, action.selector or "", action.timeout_ms)
        await _run_post_action_waits(page, action)
        return
    if action.type == BrowserActionType.wait_for_selector:
        await _wait_for_selector(page, action.selector or "", action.timeout_ms)
        await _run_post_action_waits(page, action)
        return
    if action.type == BrowserActionType.wait_ms:
        await _wait_for_timeout(page, action.wait_ms or 0)
        return
    if action.type == BrowserActionType.scroll:
        for _ in range(action.times):
            await _scroll_once(page, action.pixels)
            if action.wait_ms is not None:
                await _wait_for_timeout(page, action.wait_ms)
        return
    if action.type == BrowserActionType.repeat_click:
        for _ in range(action.max_times):
            await _click_selector(page, action.selector or "", action.timeout_ms)
            await _run_post_action_waits(page, action)


async def run_browser_actions(page: Any, actions: list[BrowserActionConfig]) -> None:
    """Execute configured browser actions in order."""
    for action in actions:
        try:
            await _run_browser_action(page, action)
        except Exception as exc:
            if not action.optional:
                raise
            logger.info(
                "Optional browser action did not complete: %s (%s: %s)",
                action.type.value,
                type(exc).__name__,
                _exception_text(exc),
            )


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
            await _click_selector(page, browser.click_selector, browser.click_timeout_ms)
            if browser.click_wait_ms is not None:
                await _wait_for_timeout(page, browser.click_wait_ms)
        except Exception as exc:
            logger.info(
                "Optional browser click_selector did not resolve or click: %s (%s: %s)",
                browser.click_selector,
                type(exc).__name__,
                _exception_text(exc),
            )
    if browser is not None and browser.actions:
        await run_browser_actions(page, browser.actions)
    capture["final_url"] = getattr(page, "url", None)
    if isinstance(capture["final_url"], str) and capture["final_url"]:
        await asyncio.to_thread(
            assert_public_url,
            capture["final_url"],
            allow_unresolved=not _BROWSER_REQUIRE_RESOLVED_DNS.get(),
        )
    try:
        capture["page_title"] = await page.title()
    except Exception as exc:
        logger.debug(
            "Failed to capture browser page title: %s: %s",
            type(exc).__name__,
            _exception_text(exc),
            exc_info=exc,
        )
        capture["page_title"] = None
    try:
        capture["html_excerpt"] = truncate_text(await page.content())
    except Exception as exc:
        logger.debug(
            "Failed to capture browser HTML excerpt: %s: %s",
            type(exc).__name__,
            _exception_text(exc),
            exc_info=exc,
        )
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
                _exception_text(exc),
                exc_info=exc,
            )
            capture["screenshot_path"] = None
    return page


async def fetch_basic_response(fetcher_cls: Any, url: str, call_kwargs: dict[str, Any]) -> Any:
    return await asyncio.to_thread(fetcher_cls.get, url, **call_kwargs)


async def fetch_browser_response(
    fetcher_cls: Any,
    url: str,
    target: TargetConfig,
    fetcher_type: FetcherType,
    call_kwargs: dict[str, Any],
    artifacts_dir: str | None,
    *,
    require_resolved_dns: bool = False,
) -> tuple[Any, dict[str, Any]]:
    capture: dict[str, Any] = {}
    browser = target_browser_config(target)

    async def _page_action(page: Any) -> Any:
        try:
            return await capture_browser_state(
                page,
                browser=browser,
                fetcher_type=fetcher_type,
                artifacts_dir=artifacts_dir,
                capture=capture,
            )
        except Exception as exc:
            message = truncate_text(_exception_text(exc), _EVENT_TEXT_CHARS)
            capture["page_action_error"] = {
                "exception_type": type(exc).__name__,
                "message": message,
            }
            if isinstance(exc, UnsafeURLError):
                capture[_PAGE_ACTION_EXCEPTION_KEY] = exc
                await _close_page_safely(page)
                raise
            action_exc = BrowserPageActionError(
                f"Browser page action failed: {message}",
                debug=capture,
            )
            capture[_PAGE_ACTION_EXCEPTION_KEY] = action_exc
            raise action_exc from exc

    call_kwargs["page_action"] = _page_action
    call_kwargs["disable_resources"] = True
    guard_token = _BROWSER_BLOCK_RESOURCES.set(browser.disable_resources)
    dns_token = _BROWSER_REQUIRE_RESOLVED_DNS.set(require_resolved_dns)
    try:
        response = await fetcher_cls.async_fetch(url, **call_kwargs)
    finally:
        _BROWSER_REQUIRE_RESOLVED_DNS.reset(dns_token)
        _BROWSER_BLOCK_RESOURCES.reset(guard_token)
    action_exc = capture.pop(_PAGE_ACTION_EXCEPTION_KEY, None)
    if action_exc is not None:
        if isinstance(action_exc, UnsafeURLError):
            action_exc.debug = capture
            raise action_exc
        if isinstance(action_exc, BrowserPageActionError):
            raise action_exc
        message = capture.get("page_action_error", {}).get("message") or type(action_exc).__name__
        raise BrowserPageActionError(
            f"Browser page action failed: {message}",
            debug=capture,
        ) from action_exc
    return response, capture


def populate_fetch_debug(debug: dict[str, Any], response: Any, url: str) -> None:
    debug["main_document_status"] = getattr(response, "status", None)
    response_url = getattr(response, "url", None)
    if not isinstance(response_url, str) or not response_url:
        response_url = None
    debug["final_url"] = response_url or debug.get("final_url") or url
    debug["page_title"] = debug.get("page_title") or response_title(response)
    debug["html_excerpt"] = debug.get("html_excerpt") or (truncate_text(response_text(response)) or None)
