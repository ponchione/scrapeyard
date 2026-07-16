"""Browser fetch instrumentation and debug blob helpers."""

from __future__ import annotations

import hashlib
import inspect
import logging
import re
from collections.abc import Callable
from contextvars import ContextVar
from functools import cache
from pathlib import Path
from typing import Any, cast

from patchright.async_api import TimeoutError as PatchrightTimeoutError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapling import Fetcher
from scrapling.engines.constants import EXTRA_RESOURCES

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.common.run_threads import run_thread_work
from scrapeyard.config.schema import (
    BROWSER_FETCH_KWARGS,
    BrowserActionConfig,
    BrowserActionType,
    BrowserConfig,
    FetcherType,
    TargetConfig,
)
from scrapeyard.engine.url_guard import (
    URLResolutionError,
    UnsafeURLError,
    assert_public_url,
    canonical_url_origin,
    redact_sensitive_mapping,
)
from scrapeyard.engine.url_guard import redact_userinfo_in_text, redact_userinfo_in_url
from scrapeyard.engine.basic_fetch import fetch_streaming_response
from scrapeyard.storage.filesystem import (
    cleanup_safe_to_thread,
    ensure_directory,
    write_bytes_file,
)

logger = logging.getLogger(__name__)

_HTML_EXCERPT_CHARS = 2000
_EVENT_TEXT_CHARS = 300
_MAX_CONSOLE_MESSAGES = 20
_MAX_REQUEST_FAILURES = 20
_MAX_BLOCKED_REQUESTS = 20
_PAGE_ACTION_EXCEPTION_KEY = "_page_action_exception"


_BROWSER_BLOCK_RESOURCES: ContextVar[bool | None] = ContextVar(
    "scrapeyard_browser_block_resources",
    default=None,
)
_BROWSER_REQUIRE_RESOLVED_DNS: ContextVar[bool] = ContextVar(
    "scrapeyard_browser_require_resolved_dns",
    default=False,
)
_BROWSER_RUN_BUDGET: ContextVar[RunBudget | None] = ContextVar(
    "scrapeyard_browser_run_budget",
    default=None,
)
_BROWSER_BLOCKED_REQUESTS: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "scrapeyard_browser_blocked_requests",
    default=None,
)
_BROWSER_TARGET_ORIGIN: ContextVar[tuple[str, str, int] | None] = ContextVar(
    "scrapeyard_browser_target_origin",
    default=None,
)
_BROWSER_ORIGIN_SCOPED_HEADERS: ContextVar[frozenset[str]] = ContextVar(
    "scrapeyard_browser_origin_scoped_headers",
    default=frozenset(),
)


class BrowserPageActionError(RuntimeError):
    """Raised when Scrapling swallowed a configured browser action failure."""

    def __init__(self, message: str, *, debug: dict[str, Any]) -> None:
        self.debug = debug
        super().__init__(message)


class BrowserTransportTimeout(TimeoutError):
    """Local retryable boundary for browser navigation and wait timeouts."""

    def __init__(self, message: str, *, debug: dict[str, Any]) -> None:
        self.debug = debug
        super().__init__(message)


_BROWSER_TIMEOUT_ERRORS = (PlaywrightTimeoutError, PatchrightTimeoutError)


async def _request_header_mapping(request: Any) -> dict[str, str] | None:
    all_headers = getattr(request, "all_headers", None)
    if callable(all_headers):
        headers = all_headers()
        if inspect.isawaitable(headers):
            headers = await headers
    else:
        headers = getattr(request, "headers", None)
    if not hasattr(headers, "items"):
        return None
    return {str(name): str(value) for name, value in headers.items()}


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

    if block_resources and resource_type in EXTRA_RESOURCES:
        logger.debug(
            'Blocking background resource "%s" of type "%s"',
            redact_userinfo_in_url(str(request_url)),
            resource_type,
        )
        await route.abort()
        return

    if isinstance(request_url, str) and request_url:
        try:
            await run_thread_work(
                assert_public_url,
                request_url,
                run_budget=_BROWSER_RUN_BUDGET.get(),
                allow_unresolved=not _BROWSER_REQUIRE_RESOLVED_DNS.get(),
            )
        except URLResolutionError:
            # Do not release an unvalidated subrequest. Aborting this browser
            # attempt still lets the outer RetryHandler retry transient DNS.
            await route.abort()
            raise
        except UnsafeURLError:
            safe_request_url = truncate_text(
                redact_userinfo_in_url(request_url),
                _EVENT_TEXT_CHARS,
            )
            blocked_requests = _BROWSER_BLOCKED_REQUESTS.get()
            if blocked_requests is not None:
                _bounded_append(
                    blocked_requests,
                    {"kind": "request", "url": safe_request_url},
                    limit=_MAX_BLOCKED_REQUESTS,
                )
            logger.warning(
                "Blocked browser request to non-public URL: %s",
                safe_request_url,
            )
            await route.abort()
            raise

    scoped_headers = _BROWSER_ORIGIN_SCOPED_HEADERS.get()
    target_origin = _BROWSER_TARGET_ORIGIN.get()
    request_origin = canonical_url_origin(request_url) if isinstance(request_url, str) else None
    if scoped_headers and target_origin is not None and request_origin != target_origin:
        headers = await _request_header_mapping(request)
        if headers is None:
            await route.abort()
            raise UnsafeURLError(
                "Browser could not safely remove target-origin headers from a "
                "cross-origin request"
            )
        filtered = {
            name: value
            for name, value in headers.items()
            if name.lower() not in scoped_headers
        }
        await route.continue_(headers=filtered)
        return

    await route.continue_()


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
    common = {
        "timeout",
        "disable_resources",
        "network_idle",
        "useragent",
        "extra_headers",
        "proxy",
        "wait_selector",
        "wait",
    }
    if fetcher_type is FetcherType.dynamic:
        return common | {
            "stealth",
            "hide_canvas",
            "real_chrome",
            "cdp_url",
            "nstbrowser_mode",
        }
    if fetcher_type is FetcherType.stealthy:
        return common | {
            "hide_canvas",
            "humanize",
            "os_randomize",
            "geoip",
            "disable_ads",
            "additional_arguments",
        }
    return set()


def browser_fetch_kwargs(
    target: TargetConfig, fetcher_type: FetcherType, *, proxy_url: str | None
) -> dict[str, Any]:
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


async def run_browser_actions(
    page: Any,
    actions: list[BrowserActionConfig],
    budget: RunBudget | None = None,
) -> None:
    """Execute configured browser actions in order."""
    for action in actions:
        try:
            if budget is None:
                await _run_browser_action(page, action)
            else:
                await budget.wait_for(_run_browser_action(page, action))
        except BudgetExceeded:
            raise
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


def _debug_limit_diagnostic(
    budget: RunBudget,
    *,
    artifact: str,
    requested: int,
    stored: int,
    action: str,
) -> dict[str, Any]:
    return {
        "limit_name": BudgetLimitName.browser_debug_bytes.value,
        "configured_limit": budget.max_browser_debug_bytes,
        "requested_amount": requested,
        "stored_amount": stored,
        "artifact": artifact,
        "action": action,
    }


async def _capture_html_excerpt(
    page: Any,
    capture: dict[str, Any],
    budget: RunBudget | None,
) -> None:
    content_awaitable = page.content()
    content = (
        await content_awaitable if budget is None else await budget.wait_for(content_awaitable)
    )
    excerpt = truncate_text(content)
    payload = excerpt.encode("utf-8")
    capture["_html_excerpt_captured"] = True
    if budget is None:
        capture["html_excerpt"] = excerpt
        return

    granted = await budget.reserve_browser_debug_bytes(len(payload), allow_partial=True)
    if granted == len(payload):
        capture["html_excerpt"] = excerpt
        return

    capture.setdefault("debug_artifact_limits", []).append(
        _debug_limit_diagnostic(
            budget,
            artifact="html_excerpt",
            requested=len(payload),
            stored=granted,
            action="omitted" if granted == 0 else "truncated",
        )
    )
    capture["html_excerpt_omitted"] = granted == 0
    capture["html_excerpt"] = (
        payload[:granted].decode("utf-8", errors="ignore") or None if granted > 0 else None
    )


async def _capture_screenshot(
    page: Any,
    *,
    fetcher_type: FetcherType,
    artifacts_dir: str,
    capture: dict[str, Any],
    budget: RunBudget | None,
) -> None:
    screenshot_awaitable = page.screenshot(full_page=True)
    screenshot = (
        await screenshot_awaitable
        if budget is None
        else await budget.wait_for(screenshot_awaitable)
    )
    if not isinstance(screenshot, (bytes, bytearray, memoryview)):
        capture["screenshot_path"] = None
        capture["screenshot_capture_error"] = {
            "reason": "browser API did not return screenshot bytes"
        }
        return

    payload = bytes(screenshot)
    granted = len(payload)
    if budget is not None:
        granted = await budget.reserve_browser_debug_bytes(len(payload))
        if granted == 0 and payload:
            capture["screenshot_path"] = None
            capture.setdefault("debug_artifact_limits", []).append(
                _debug_limit_diagnostic(
                    budget,
                    artifact="screenshot",
                    requested=len(payload),
                    stored=0,
                    action="omitted",
                )
            )
            return

    artifacts_path = Path(artifacts_dir)
    screenshot_path = artifacts_path / f"{fetcher_type.value}-main.png"
    try:
        await cleanup_safe_to_thread(ensure_directory, artifacts_path)
        await cleanup_safe_to_thread(write_bytes_file, screenshot_path, payload)
    except BaseException:
        if budget is not None and granted:
            await budget.release_browser_debug_bytes(granted)
        raise
    capture["screenshot_path"] = str(screenshot_path)


async def capture_browser_state(
    page: Any,
    *,
    browser: Any,
    fetcher_type: FetcherType,
    artifacts_dir: str | None,
    capture: dict[str, Any],
    budget: RunBudget | None = None,
) -> Any:
    capture.setdefault("console_messages", [])
    capture.setdefault("request_failures", [])
    _register_console_capture(page, capture)
    _register_request_failure_capture(page, capture)
    if browser is not None and browser.click_selector:
        try:
            click = _click_selector(page, browser.click_selector, browser.click_timeout_ms)
            if budget is None:
                await click
            else:
                await budget.wait_for(click)
            if browser.click_wait_ms is not None:
                wait = _wait_for_timeout(page, browser.click_wait_ms)
                if budget is None:
                    await wait
                else:
                    await budget.wait_for(wait)
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.info(
                "Optional browser click_selector did not resolve or click: "
                "query_sha256=%s (%s: %s)",
                hashlib.sha256(browser.click_selector.encode("utf-8")).hexdigest(),
                type(exc).__name__,
                _exception_text(exc),
            )
    if browser is not None and browser.actions:
        await run_browser_actions(page, browser.actions, budget=budget)
    capture["final_url"] = getattr(page, "url", None)
    if isinstance(capture["final_url"], str) and capture["final_url"]:
        await run_thread_work(
            assert_public_url,
            capture["final_url"],
            run_budget=budget,
            allow_unresolved=not _BROWSER_REQUIRE_RESOLVED_DNS.get(),
        )
    try:
        title = page.title()
        capture["page_title"] = await title if budget is None else await budget.wait_for(title)
    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.debug(
            "Failed to capture browser page title: %s: %s",
            type(exc).__name__,
            _exception_text(exc),
        )
        capture["page_title"] = None
    try:
        await _capture_html_excerpt(page, capture, budget)
    except BudgetExceeded:
        raise
    except Exception as exc:
        logger.debug(
            "Failed to capture browser HTML excerpt: %s: %s",
            type(exc).__name__,
            _exception_text(exc),
        )
        capture["html_excerpt"] = None
        capture["_html_excerpt_captured"] = True
    if artifacts_dir is not None:
        try:
            await _capture_screenshot(
                page,
                fetcher_type=fetcher_type,
                artifacts_dir=artifacts_dir,
                capture=capture,
                budget=budget,
            )
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.debug(
                "Failed to capture browser screenshot in %s: %s: %s",
                artifacts_dir,
                type(exc).__name__,
                _exception_text(exc),
            )
            capture["screenshot_path"] = None
    return page


async def fetch_basic_response(
    fetcher_cls: Any,
    url: str,
    call_kwargs: dict[str, Any],
    *,
    budget: RunBudget | None = None,
) -> Any:
    if fetcher_cls is Fetcher:
        return await fetch_streaming_response(
            fetcher_cls,
            url,
            call_kwargs,
            budget=budget,
        )
    return await run_thread_work(
        fetcher_cls.get,
        url,
        run_budget=budget,
        **call_kwargs,
    )


async def fetch_browser_response(
    fetcher_cls: Any,
    url: str,
    target: TargetConfig,
    fetcher_type: FetcherType,
    call_kwargs: dict[str, Any],
    artifacts_dir: str | None,
    *,
    require_resolved_dns: bool = False,
    budget: RunBudget | None = None,
    response_observer: Callable[[], None] | None = None,
) -> tuple[Any, dict[str, Any]]:
    capture: dict[str, Any] = {}
    browser = target_browser_config(target)

    async def _page_action(page: Any) -> Any:
        # Scrapling invokes this only after navigation has produced a page, so
        # availability is known even if local debug capture exhausts the run.
        if response_observer is not None:
            response_observer()
        try:
            return await capture_browser_state(
                page,
                browser=browser,
                fetcher_type=fetcher_type,
                artifacts_dir=artifacts_dir,
                capture=capture,
                budget=budget,
            )
        except BudgetExceeded as exc:
            capture[_PAGE_ACTION_EXCEPTION_KEY] = exc
            await _close_page_safely(page)
            raise
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

    async def _page_setup(page: Any) -> None:
        await page.route("**/*", _guarded_async_intercept_route)

    call_kwargs["page_setup"] = _page_setup
    # The local route owns both resource blocking and SSRF validation. Keep the
    # upstream handler disabled so it cannot continue a request first.
    call_kwargs["disable_resources"] = False
    blocked_requests: list[dict[str, str]] = []
    guard_token = _BROWSER_BLOCK_RESOURCES.set(browser.disable_resources)
    dns_token = _BROWSER_REQUIRE_RESOLVED_DNS.set(require_resolved_dns)
    budget_token = _BROWSER_RUN_BUDGET.set(budget)
    blocked_token = _BROWSER_BLOCKED_REQUESTS.set(blocked_requests)
    origin_token = _BROWSER_TARGET_ORIGIN.set(canonical_url_origin(url))
    headers_token = _BROWSER_ORIGIN_SCOPED_HEADERS.set(
        frozenset(name.lower() for name in browser.extra_headers)
    )
    try:
        fetch = fetcher_cls.async_fetch(url, **call_kwargs)
        try:
            response = await fetch if budget is None else await budget.wait_for_owned(fetch)
        except _BROWSER_TIMEOUT_ERRORS as exc:
            original_debug = getattr(exc, "debug", None)
            timeout_debug = dict(original_debug) if isinstance(original_debug, dict) else {}
            timeout_debug.update(capture)
            if blocked_requests:
                timeout_debug["blocked_requests"] = blocked_requests
            raise BrowserTransportTimeout(str(exc), debug=timeout_debug) from exc
        except Exception as exc:
            if blocked_requests:
                capture["blocked_requests"] = blocked_requests
                try:
                    cast(Any, exc).debug = capture
                except Exception:
                    pass
            raise
    finally:
        _BROWSER_RUN_BUDGET.reset(budget_token)
        _BROWSER_ORIGIN_SCOPED_HEADERS.reset(headers_token)
        _BROWSER_TARGET_ORIGIN.reset(origin_token)
        _BROWSER_BLOCKED_REQUESTS.reset(blocked_token)
        _BROWSER_REQUIRE_RESOLVED_DNS.reset(dns_token)
        _BROWSER_BLOCK_RESOURCES.reset(guard_token)
    if blocked_requests:
        capture["blocked_requests"] = blocked_requests
    action_exc = capture.pop(_PAGE_ACTION_EXCEPTION_KEY, None)
    if action_exc is not None:
        if isinstance(action_exc, BudgetExceeded):
            raise action_exc
        if isinstance(action_exc, UnsafeURLError):
            cast(Any, action_exc).debug = capture
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
    excerpt_captured = bool(debug.pop("_html_excerpt_captured", False))
    if not excerpt_captured and not debug.get("html_excerpt_omitted"):
        debug["html_excerpt"] = debug.get("html_excerpt") or (
            truncate_text(response_text(response)) or None
        )
