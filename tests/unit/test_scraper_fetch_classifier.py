from __future__ import annotations

import asyncio

from scrapeyard.config.schema import FetcherType
from scrapeyard.engine.fetch_classifier import classify_fetch_exception
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
