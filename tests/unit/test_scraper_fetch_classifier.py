from __future__ import annotations

import asyncio

from scrapeyard.config.schema import FetcherType
from scrapeyard.engine.fetch_classifier import (
    classify_fetch_exception,
    classify_page_signals,
    classify_rendered_outcome,
)
from scrapeyard.engine.scraper import FetchError
from scrapeyard.engine.resilience import RetryableError
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


def test_classify_fetch_exception_preserves_retryable_http_statuses():
    exc = RetryableError(500)

    error_type, http_status, debug = classify_fetch_exception(exc, FetcherType.basic)

    assert error_type == ErrorType.http_error
    assert http_status == 500
    assert debug is None


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
