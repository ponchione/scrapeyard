"""Exception and rendered-output classification helpers for scraping."""

from __future__ import annotations

import asyncio
from typing import Any

from scrapeyard.config.schema import FetcherType
from scrapeyard.engine.adaptive_diagnostics import has_extracted_value
from scrapeyard.engine.resilience import RetryableError
from scrapeyard.engine.scrape_models import FetchError
from scrapeyard.models.job import ErrorType

_CHALLENGE_MARKERS = (
    "captcha",
    "cf-challenge",
    "challenge page",
    "verify you are human",
    "press and hold",
    "security check",
    "bot verification",
    "powered and protected by akamai",
    "akam-sw.js",
    "service worker bootstrap",
    "sec-if-cpt-container",
    "akamai",
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


def token_match(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _debug_signal_blob(debug: dict[str, Any]) -> str:
    parts: list[str] = [
        str(debug.get("page_title") or ""),
        str(debug.get("html_excerpt") or ""),
    ]
    for message in debug.get("console_messages") or []:
        if isinstance(message, dict):
            parts.append(str(message.get("type") or ""))
            parts.append(str(message.get("text") or ""))
        else:
            parts.append(str(message))
    for failure in debug.get("request_failures") or []:
        if isinstance(failure, dict):
            parts.append(str(failure.get("url") or ""))
            parts.append(str(failure.get("method") or ""))
            parts.append(str(failure.get("resource_type") or ""))
            parts.append(str(failure.get("error_text") or ""))
        else:
            parts.append(str(failure))
    return "\n".join(part.lower() for part in parts if part)


def classify_page_signals(debug: dict[str, Any]) -> ErrorType | None:
    blob = _debug_signal_blob(debug)
    if not blob:
        return None
    if token_match(blob, _CHALLENGE_MARKERS):
        return ErrorType.challenge_page
    if token_match(blob, _CONSENT_MARKERS):
        return ErrorType.consent_gate
    if token_match(blob, _LOGIN_MARKERS):
        return ErrorType.login_gate
    if token_match(blob, _BLOCK_MARKERS):
        return ErrorType.blocked_response
    return None


def classify_rendered_outcome(
    debug: dict[str, Any],
    data: list[dict[str, Any]],
    *,
    has_item_selector: bool,
) -> ErrorType | None:
    signal_classification = classify_page_signals(debug)
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
    if any(has_extracted_value(value) for row in data for value in row.values()):
        return None
    if selector_counts and any(count > 0 for count in selector_counts.values()):
        return ErrorType.selector_miss
    return ErrorType.rendered_empty


def classify_fetch_exception(
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
                signal = classify_page_signals(exc.debug)
                if signal in {ErrorType.challenge_page, ErrorType.consent_gate, ErrorType.login_gate}:
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
    if token_match(detail, _PROXY_MARKERS):
        return ErrorType.proxy_rejected, None, debug
    if fetcher_type != FetcherType.basic and token_match(detail, _BROWSER_ERROR_MARKERS):
        return ErrorType.browser_error, None, debug
    if isinstance(exc, (ConnectionError, OSError)) or token_match(detail, _NETWORK_ERROR_MARKERS):
        return ErrorType.network_error, None, debug
    return ErrorType.http_error, None, debug
