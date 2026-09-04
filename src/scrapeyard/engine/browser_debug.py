"""Browser fetch instrumentation and debug blob helpers."""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from typing import Any, cast

from patchright.async_api import Error as PatchrightError
from patchright.async_api import TimeoutError as PatchrightTimeoutError
from patchright._impl._errors import TargetClosedError as PatchrightTargetClosedError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright._impl._errors import TargetClosedError as PlaywrightTargetClosedError
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
_MAX_REQUEST_LEDGER_ENTRIES = 1000
_REQUEST_URL_CHARS = 2048
_PAGE_ACTION_EXCEPTION_KEY = "_page_action_exception"
_DEBUG_RESPONSE_TYPES = frozenset({"fetch", "xhr"})


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
_BROWSER_TARGET_CLOSED_ERRORS = (PlaywrightTargetClosedError, PatchrightTargetClosedError)
_BROWSER_SCREENSHOT_ERRORS = (PlaywrightError, PatchrightError)


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


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_failure_text(request: Any) -> str:
    try:
        failure = getattr(request, "failure", None)
        if callable(failure):
            failure = failure()
    except Exception:
        failure = None
    if isinstance(failure, dict):
        failure = failure.get("errorText") or failure.get("error_text")
    elif failure is not None and not isinstance(failure, str):
        failure = getattr(failure, "error_text", failure)
    return truncate_text(redact_userinfo_in_text(coerce_to_text(failure)), _EVENT_TEXT_CHARS)


def _response_headers(response: Any) -> dict[str, Any]:
    try:
        headers = getattr(response, "headers", None)
        if callable(headers):
            headers = headers()
    except Exception:
        headers = None
    if not hasattr(headers, "items"):
        return {}
    bounded = {
        str(name): truncate_text(coerce_to_text(value), _EVENT_TEXT_CHARS)
        for name, value in headers.items()
    }
    return cast(dict[str, Any], redact_sensitive_mapping(bounded))


@dataclass(slots=True)
class _BrowserRequestCapture:
    """Context-level request capture installed before the first navigation."""

    capture: dict[str, Any]
    request_indexes: dict[int, int]
    responses: list[tuple[dict[str, Any], Any]]

    def __init__(self, capture: dict[str, Any]) -> None:
        self.capture = capture
        self.request_indexes = {}
        self.responses = []

    def _entry(self, request: Any) -> dict[str, Any] | None:
        request_key = id(request)
        existing = self.request_indexes.get(request_key)
        ledger = self.capture.setdefault("request_ledger", [])
        self.capture.setdefault("request_ledger_omitted", 0)
        if existing is not None:
            return cast(dict[str, Any], ledger[existing])
        if len(ledger) >= _MAX_REQUEST_LEDGER_ENTRIES:
            self.capture["request_ledger_omitted"] += 1
            return None

        redirected_from = getattr(request, "redirected_from", None)
        if callable(redirected_from):
            try:
                redirected_from = redirected_from()
            except Exception:
                redirected_from = None
        redirected_from_url = _safe_text_attr(redirected_from, "url")
        if redirected_from_url:
            redirected_from_url = truncate_text(
                redact_userinfo_in_url(redirected_from_url),
                _REQUEST_URL_CHARS,
            )

        is_navigation = getattr(request, "is_navigation_request", False)
        if callable(is_navigation):
            try:
                is_navigation = is_navigation()
            except Exception:
                is_navigation = False

        entry: dict[str, Any] = {
            "started_at": _utc_timestamp(),
            "method": _safe_text_attr(request, "method") or "unknown",
            "url": truncate_text(
                redact_userinfo_in_url(_safe_text_attr(request, "url") or ""),
                _REQUEST_URL_CHARS,
            ),
            "resource_type": _safe_text_attr(request, "resource_type") or "unknown",
            "is_navigation_request": bool(is_navigation),
            "redirected_from": redirected_from_url,
            "response_status": None,
            "response_at": None,
            "finished_at": None,
            "retry_after": None,
            "failure": None,
        }
        self.request_indexes[request_key] = len(ledger)
        ledger.append(entry)
        return entry

    def on_request(self, request: Any) -> None:
        self._entry(request)

    def on_response(self, response: Any) -> None:
        request = getattr(response, "request", None)
        entry = self._entry(request) if request is not None else None
        if entry is None:
            return
        status = getattr(response, "status", None)
        entry["response_status"] = status if isinstance(status, int) else None
        entry["response_at"] = _utc_timestamp()
        headers = _response_headers(response)
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        entry["retry_after"] = retry_after
        if entry["resource_type"] == "document":
            entry["response_headers"] = headers
        if entry["resource_type"] in _DEBUG_RESPONSE_TYPES:
            entry["content_type"] = headers.get("content-type") or headers.get("Content-Type")
            self.responses.append((entry, response))

    def on_request_finished(self, request: Any) -> None:
        entry = self._entry(request)
        if entry is not None:
            entry["finished_at"] = _utc_timestamp()

    def on_request_failed(self, request: Any) -> None:
        entry = self._entry(request)
        error_text = _request_failure_text(request)
        if entry is not None:
            entry["finished_at"] = _utc_timestamp()
            entry["failure"] = error_text
        failure_entry = {
            "url": truncate_text(
                redact_userinfo_in_url(_safe_text_attr(request, "url") or ""),
                _REQUEST_URL_CHARS,
            ),
            "method": _safe_text_attr(request, "method") or "unknown",
            "resource_type": _safe_text_attr(request, "resource_type") or "unknown",
            "error_text": error_text,
        }
        _bounded_append(
            self.capture.setdefault("request_failures", []),
            failure_entry,
            limit=_MAX_REQUEST_FAILURES,
        )

    def install(self, context: Any) -> None:
        if not hasattr(context, "on"):
            raise RuntimeError("Browser context does not support request events")
        self.capture.setdefault("request_ledger", [])
        self.capture.setdefault("request_ledger_omitted", 0)
        self.capture.setdefault("request_failures", [])

        # Playwright caches wrappers by assigning attributes to bound-method
        # owners. This capture uses slots, so register plain closures instead.
        def on_request(request: Any) -> None:
            self.on_request(request)

        def on_response(response: Any) -> None:
            self.on_response(response)

        def on_request_finished(request: Any) -> None:
            self.on_request_finished(request)

        def on_request_failed(request: Any) -> None:
            self.on_request_failed(request)

        context.on("request", on_request)
        context.on("response", on_response)
        context.on("requestfinished", on_request_finished)
        context.on("requestfailed", on_request_failed)

    async def capture_response_bodies(
        self,
        artifacts_dir: str | None,
        budget: RunBudget | None,
    ) -> None:
        if artifacts_dir is None:
            return
        for index, (entry, response) in enumerate(self.responses, 1):
            body = getattr(response, "body", None)
            if not callable(body):
                entry["body_capture_error"] = "response body API unavailable"
                continue
            try:
                awaitable = body()
                payload = await awaitable if budget is None else await budget.wait_for(awaitable)
                if not isinstance(payload, (bytes, bytearray, memoryview)):
                    raise TypeError("response body API did not return bytes")
                content_type = str(entry.get("content_type") or "").lower()
                if not any(marker in content_type for marker in ("json", "text", "javascript")):
                    entry["body_capture_skipped"] = "non-text content type"
                    continue
                suffix = ".json" if "json" in content_type else ".txt"
                safe_payload = _redact_text_response_body(bytes(payload), content_type)
                granted = len(safe_payload)
                if budget is not None:
                    granted = await budget.reserve_browser_debug_bytes(len(safe_payload))
                if granted == 0 and safe_payload:
                    self.capture.setdefault("debug_artifact_limits", []).append(
                        _debug_limit_diagnostic(
                            budget,
                            artifact="response_body",
                            requested=len(safe_payload),
                            stored=0,
                            action="omitted",
                        )
                    )
                    entry["body_capture_skipped"] = "browser debug byte budget"
                    continue
                name = f"response-{index:04d}{suffix}"
                path = Path(artifacts_dir) / "responses" / name
                try:
                    await cleanup_safe_to_thread(ensure_directory, path.parent)
                    await cleanup_safe_to_thread(write_bytes_file, path, safe_payload)
                except BaseException:
                    if budget is not None and granted:
                        await budget.release_browser_debug_bytes(granted)
                    raise
                entry["body_artifact"] = f"responses/{name}"
                entry["body_size"] = len(safe_payload)
                entry["body_sha256"] = hashlib.sha256(safe_payload).hexdigest()
            except BudgetExceeded:
                raise
            except Exception as exc:
                entry["body_capture_error"] = (
                    f"{type(exc).__name__}: "
                    f"{truncate_text(_exception_text(exc), _EVENT_TEXT_CHARS)}"
                )


@dataclass(slots=True)
class _BrowserNetworkGuard:
    """Context-wide HTTP/WebSocket policy installed before the first page."""

    block_resources: bool
    require_resolved_dns: bool
    budget: RunBudget | None
    blocked_requests: list[dict[str, str]]
    target_origin: tuple[str, str, int] | None
    extra_headers: dict[str, str]

    def _record_blocked(self, kind: str, request_url: str) -> str:
        safe_request_url = truncate_text(
            redact_userinfo_in_url(request_url),
            _EVENT_TEXT_CHARS,
        )
        _bounded_append(
            self.blocked_requests,
            {"kind": kind, "url": safe_request_url},
            limit=_MAX_BLOCKED_REQUESTS,
        )
        return safe_request_url

    async def _validate_url(self, request_url: str, *, kind: str) -> None:
        try:
            await run_thread_work(
                assert_public_url,
                request_url,
                run_budget=self.budget,
                allow_unresolved=not self.require_resolved_dns,
            )
        except UnsafeURLError:
            safe_request_url = self._record_blocked(kind, request_url)
            logger.warning(
                "Blocked browser %s to non-public URL: %s",
                kind,
                safe_request_url,
            )
            raise

    async def route_http(self, route: Any) -> None:
        """Apply the request policy to every page in the browser context."""

        request = route.request
        request_url = getattr(request, "url", "")
        resource_type = getattr(request, "resource_type", None)

        if self.block_resources and resource_type in EXTRA_RESOURCES:
            logger.debug(
                'Blocking background resource "%s" of type "%s"',
                redact_userinfo_in_url(str(request_url)),
                resource_type,
            )
            await route.abort()
            return

        if isinstance(request_url, str) and request_url:
            try:
                await self._validate_url(request_url, kind="request")
            except (URLResolutionError, UnsafeURLError):
                await route.abort()
                return

        if not self.extra_headers:
            await route.continue_()
            return

        headers = await _request_header_mapping(request)
        if headers is None:
            await route.abort()
            logger.warning("Blocked browser request whose headers could not be scoped")
            return

        scoped_names = {name.lower() for name in self.extra_headers}
        filtered = {
            name: value for name, value in headers.items() if name.lower() not in scoped_names
        }
        request_origin = canonical_url_origin(request_url) if isinstance(request_url, str) else None
        if self.target_origin is None or request_origin != self.target_origin:
            await route.continue_(headers=filtered)
            return

        # Playwright carries continue_() header overrides through redirects.
        # Fetch exactly one hop and fulfill it so a redirect becomes a fresh,
        # independently classified browser request before credentials are added.
        response = await route.fetch(
            headers={**filtered, **self.extra_headers},
            max_redirects=0,
        )
        await route.fulfill(response=response)

    async def route_websocket(self, route: Any) -> None:
        """Block disabled WebSockets and classify every enabled destination."""

        request_url = getattr(route, "url", "")
        if self.block_resources:
            if isinstance(request_url, str) and request_url:
                self._record_blocked("websocket", request_url)
            await route.close(code=1008, reason="WebSocket resources are disabled")
            return

        if not isinstance(request_url, str) or not request_url:
            await route.close(code=1008, reason="WebSocket URL unavailable")
            return
        try:
            await self._validate_url(request_url, kind="websocket")
        except (URLResolutionError, UnsafeURLError):
            await route.close(code=1008, reason="WebSocket destination blocked")
            return
        route.connect_to_server()

    async def install(self, context: Any) -> None:
        """Install all guards at context scope; any failure is navigation-fatal."""

        async def route_http(route: Any) -> None:
            try:
                await self.route_http(route)
            except _BROWSER_TARGET_CLOSED_ERRORS:
                return

        async def route_websocket(route: Any) -> None:
            await self.route_websocket(route)

        await context.route("**/*", route_http)
        route_web_socket = getattr(context, "route_web_socket", None)
        if not callable(route_web_socket):
            raise RuntimeError("Browser context does not support WebSocket routing")
        await route_web_socket("**/*", route_websocket)


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
        "html_artifact": None,
        "screenshot_path": None,
        "console_messages": [],
        "request_failures": [],
        "request_ledger": [],
        "request_ledger_omitted": 0,
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


def _redact_text_response_body(payload: bytes, content_type: str) -> bytes:
    text = payload.decode("utf-8", errors="replace")
    if "json" in content_type:
        try:
            return json.dumps(
                redact_sensitive_mapping(json.loads(text)),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            pass
    return redact_userinfo_in_text(text).encode("utf-8")


async def _capture_html(
    page: Any,
    capture: dict[str, Any],
    budget: RunBudget | None,
    *,
    fetcher_type: FetcherType,
    artifacts_dir: str | None,
) -> None:
    content_awaitable = page.content()
    content = (
        await content_awaitable if budget is None else await budget.wait_for(content_awaitable)
    )
    safe_content = redact_userinfo_in_text(content)
    excerpt = truncate_text(safe_content)
    payload = excerpt.encode("utf-8")
    capture["_html_excerpt_captured"] = True
    if budget is None:
        capture["html_excerpt"] = excerpt
    else:
        granted = await budget.reserve_browser_debug_bytes(len(payload), allow_partial=True)
        if granted == len(payload):
            capture["html_excerpt"] = excerpt
        else:
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
                payload[:granted].decode("utf-8", errors="ignore") or None
                if granted > 0
                else None
            )

    capture["html_requested"] = artifacts_dir is not None
    if artifacts_dir is None:
        return
    full_payload = safe_content.encode("utf-8")
    granted = len(full_payload)
    if budget is not None:
        granted = await budget.reserve_browser_debug_bytes(len(full_payload))
    if granted == 0 and full_payload:
        capture.setdefault("debug_artifact_limits", []).append(
            _debug_limit_diagnostic(
                budget,
                artifact="html",
                requested=len(full_payload),
                stored=0,
                action="omitted",
            )
        )
        capture["html_artifact"] = None
        return
    path = Path(artifacts_dir) / f"{fetcher_type.value}-main.html"
    try:
        await cleanup_safe_to_thread(ensure_directory, path.parent)
        await cleanup_safe_to_thread(write_bytes_file, path, full_payload)
    except BaseException:
        if budget is not None and granted:
            await budget.release_browser_debug_bytes(granted)
        raise
    capture["html_artifact"] = path.name
    capture["html_size"] = len(full_payload)
    capture["html_sha256"] = hashlib.sha256(full_payload).hexdigest()


async def _capture_screenshot(
    page: Any,
    *,
    fetcher_type: FetcherType,
    artifacts_dir: str,
    capture: dict[str, Any],
    budget: RunBudget | None,
) -> None:
    async def take(*, full_page: bool) -> Any:
        screenshot_awaitable = page.screenshot(full_page=full_page)
        return (
            await screenshot_awaitable
            if budget is None
            else await budget.wait_for(screenshot_awaitable)
        )

    try:
        screenshot = await take(full_page=True)
        capture["screenshot_mode"] = "full_page"
    except _BROWSER_SCREENSHOT_ERRORS as exc:
        capture["screenshot_full_page_error"] = {
            "exception_type": type(exc).__name__,
            "message": truncate_text(_exception_text(exc), _EVENT_TEXT_CHARS),
        }
        screenshot = await take(full_page=False)
        capture["screenshot_mode"] = "viewport_fallback"
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
    require_resolved_dns: bool = False,
    register_request_failures: bool = True,
) -> Any:
    capture.setdefault("console_messages", [])
    capture.setdefault("request_failures", [])
    capture["screenshot_requested"] = artifacts_dir is not None
    _register_console_capture(page, capture)
    if register_request_failures:
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
            allow_unresolved=not require_resolved_dns,
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
        await _capture_html(
            page,
            capture,
            budget,
            fetcher_type=fetcher_type,
            artifacts_dir=artifacts_dir,
        )
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
            capture["screenshot_capture_error"] = {
                "exception_type": type(exc).__name__,
                "message": truncate_text(_exception_text(exc), _EVENT_TEXT_CHARS),
            }
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
    request_capture = _BrowserRequestCapture(capture)

    async def _page_action(page: Any) -> Any:
        # Scrapling invokes this only after navigation has produced a page, so
        # availability is known even if local debug capture exhausts the run.
        if response_observer is not None:
            response_observer()
        try:
            result = await capture_browser_state(
                page,
                browser=browser,
                fetcher_type=fetcher_type,
                artifacts_dir=artifacts_dir,
                capture=capture,
                budget=budget,
                require_resolved_dns=require_resolved_dns,
                register_request_failures=False,
            )
            await request_capture.capture_response_bodies(artifacts_dir, budget)
            return result
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

    # Never give target-only credentials to browser-wide header APIs. The
    # context guard adds them to one same-origin HTTP hop at a time.
    call_kwargs.pop("extra_headers", None)
    call_kwargs["disable_resources"] = False
    blocked_requests: list[dict[str, str]] = []
    network_guard = _BrowserNetworkGuard(
        block_resources=browser.disable_resources,
        require_resolved_dns=require_resolved_dns,
        budget=budget,
        blocked_requests=blocked_requests,
        target_origin=canonical_url_origin(url),
        extra_headers=dict(browser.extra_headers),
    )

    async def _context_setup(context: Any) -> None:
        request_capture.install(context)
        await network_guard.install(context)

    call_kwargs["context_setup"] = _context_setup
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
