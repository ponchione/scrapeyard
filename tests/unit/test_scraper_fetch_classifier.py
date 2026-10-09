from __future__ import annotations

import asyncio

import httpx
import pytest

from scrapeyard.config.schema import FetcherType
from scrapeyard.engine.fetch_classifier import (
    classify_fetch_exception,
    classify_page_signals,
    classify_rendered_outcome,
)
from scrapeyard.engine.resilience import RetryableError
from scrapeyard.engine.scraper import FetchError
from scrapeyard.models.job import ErrorType


def test_classify_fetch_exception_prefers_page_signal_for_blocked_fetch_errors():
    exc = FetchError(
        403,
        debug={
            "page_title": "Verify you are human",
            "html_excerpt": "security check before accessing the site",
        },
    )

    error_type, http_status, debug = classify_fetch_exception(exc, FetcherType.dynamic)

    assert error_type == ErrorType.challenge_page
    assert http_status == 403
    assert debug == exc.debug


def test_classify_fetch_exception_maps_browser_timeout_for_dynamic_fetcher():
    error_type, http_status, debug = classify_fetch_exception(asyncio.TimeoutError(), FetcherType.dynamic)

    assert error_type == ErrorType.navigation_timeout
    assert http_status is None
    assert debug is None


def test_classify_fetch_exception_maps_httpx_timeout_for_basic_fetcher():
    error_type, http_status, debug = classify_fetch_exception(
        httpx.ReadTimeout("read timed out"),
        FetcherType.basic,
    )

    assert error_type == ErrorType.timeout
    assert http_status is None
    assert debug is None


def test_classify_fetch_exception_preserves_retryable_http_statuses():
    exc = RetryableError(500)

    error_type, http_status, debug = classify_fetch_exception(exc, FetcherType.basic)

    assert error_type == ErrorType.http_error
    assert http_status == 500
    assert debug is None


_CHALLENGE_DEBUG = {
    "page_title": "Just a moment...",
    "html_excerpt": "Verify you are human by completing the security check below",
}


@pytest.mark.parametrize(
    ("status", "page", "expected"),
    [
        (403, _CHALLENGE_DEBUG, ErrorType.challenge_page),
        (429, _CHALLENGE_DEBUG, ErrorType.challenge_page),
        (403, {"page_title": "Access Denied", "html_excerpt": "Reference #18"}, ErrorType.blocked_response),
        # Unlike a non-retryable FetchError, consent and login markers keep
        # the blocked_response that retryable denials have always reported.
        (403, {"page_title": "Sign in", "html_excerpt": "Log in to continue"}, ErrorType.blocked_response),
        (401, {"page_title": "Cookie consent", "html_excerpt": ""}, ErrorType.blocked_response),
        (404, _CHALLENGE_DEBUG, ErrorType.http_not_found),
        (503, _CHALLENGE_DEBUG, ErrorType.http_error),
        (500, {"page_title": "Internal Server Error", "html_excerpt": ""}, ErrorType.http_error),
    ],
)
def test_classify_fetch_exception_checks_retryable_responses_for_challenges(status, page, expected):
    exc = RetryableError(status, debug={"main_document_status": status, **page})

    error_type, http_status, debug = classify_fetch_exception(exc, FetcherType.basic)

    assert error_type == expected
    assert http_status == status
    assert debug is exc.debug


def test_classify_page_signals_detects_akamai_interstitial_markers_in_html_excerpt():
    debug = {
        "page_title": "Bass Pro Shops",
        "html_excerpt": "Powered and protected by Akamai akam-sw.js install script sec-if-cpt-container service worker bootstrap",
    }

    assert classify_page_signals(debug) == ErrorType.challenge_page


def test_classify_page_signals_detects_akamai_interstitial_markers_in_console_and_request_failures():
    debug = {
        "page_title": None,
        "html_excerpt": "<html><body>loading...</body></html>",
        "console_messages": [
            {"type": "info", "text": "akam-sw.js install script booting"},
        ],
        "request_failures": [
            {
                "url": "https://example.com/akam/sw.js",
                "method": "GET",
                "resource_type": "script",
                "error_text": "service worker bootstrap failed after security check",
            }
        ],
    }

    assert classify_page_signals(debug) == ErrorType.challenge_page


def test_classify_rendered_outcome_prefers_challenge_for_akamai_interstitial_over_rendered_empty():
    debug = {
        "page_title": "Bass Pro Shops",
        "html_excerpt": "Powered and protected by Akamai akam-sw.js install script sec-if-cpt-container",
        "selector_counts": {"title": 0},
        "item_selector_count": 0,
        "console_messages": [],
        "request_failures": [],
    }

    result = classify_rendered_outcome(debug, data=[], has_item_selector=True)

    assert result == ErrorType.challenge_page


_AKAMAI_BOT_MANAGER = (
    "<script>/* Akamai bot manager */ var container = 'sec-if-cpt-container';</script>"
)


def test_classify_rendered_outcome_ignores_access_markers_on_a_page_with_rendered_items():
    # MidwayUSA 2026-10-09: a normal listing embeds Akamai's bot-manager script.
    debug = {
        "page_title": "Scopes | MidwayUSA",
        "main_document_status": 200,
        "html_excerpt": f"<html><head>{_AKAMAI_BOT_MANAGER}</head><body><ol>...",
        "item_selector_count": 60,
        "selector_counts": {"name": 1, "price": 1, "url": 1},
    }
    data = [{"name": "Scope", "price": "$10"}, {"name": "", "price": "$20"}]

    assert classify_page_signals(debug) == ErrorType.challenge_page
    assert classify_rendered_outcome(debug, data, has_item_selector=True) is None


def test_classify_rendered_outcome_ignores_access_markers_when_selectors_match_without_items():
    debug = {
        "page_title": "Product | Shop",
        "html_excerpt": f"<html><head>{_AKAMAI_BOT_MANAGER}</head><nav>My account</nav>",
        "item_selector_count": None,
        "selector_counts": {"name": 1, "price": 0},
    }

    assert classify_rendered_outcome(debug, [{"name": "Scope"}], has_item_selector=False) is None


@pytest.mark.parametrize(
    ("counts", "has_item_selector"),
    [
        ({"item_selector_count": 0, "selector_counts": {"name": 0}}, True),
        ({"item_selector_count": 3, "selector_counts": {"name": 0, "price": 0}}, True),
        ({"item_selector_count": None, "selector_counts": {"name": 0}}, False),
        ({}, True),
    ],
)
def test_classify_rendered_outcome_keeps_access_markers_when_expected_content_is_missing(
    counts,
    has_item_selector,
):
    debug = {
        "page_title": "Access Denied",
        "html_excerpt": f"{_AKAMAI_BOT_MANAGER}<p>Press &amp; Hold to confirm you are a human</p>",
        **counts,
    }

    assert classify_rendered_outcome(debug, [], has_item_selector=has_item_selector) == (
        ErrorType.challenge_page
    )
