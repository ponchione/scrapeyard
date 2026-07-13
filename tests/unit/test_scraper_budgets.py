from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from scrapling.engines.toolbelt.custom import Response

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.resilience import RetryHandler
from scrapeyard.engine.scraper import _fetch_basic_with_safe_redirects, _fetch_page


def _budget(max_fetched_bytes: int) -> RunBudget:
    return RunBudget(
        max_duration_seconds=60,
        max_fetched_bytes=max_fetched_bytes,
        max_extracted_records=100,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1000,
    )


def _scrapling_response(*, body: bytes, url: str = "https://example.com/") -> Response:
    return Response(
        url=url,
        text=body.decode(),
        body=body,
        status=200,
        reason="OK",
        cookies={},
        headers={},
        request_headers={},
        encoding="utf-8",
        method="GET",
        history=[],
    )


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:The 'strip_cdata' option:DeprecationWarning")
async def test_real_scrapling_response_body_is_counted(monkeypatch):
    response = _scrapling_response(body=b"<html><body>hello</body></html>")

    class Fetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            return response

    monkeypatch.setattr(
        "scrapeyard.engine.scraper._assert_fetch_url",
        AsyncMock(),
    )
    budget = _budget(10_000)

    await _fetch_basic_with_safe_redirects(
        Fetcher,
        "https://example.com/",
        {},
        {},
        budget=budget,
    )

    assert isinstance(response.body, str)
    assert budget.fetched_bytes == len(response.body.encode(response.encoding))


@pytest.mark.asyncio
async def test_basic_redirect_bodies_are_counted_aggregate(monkeypatch):
    responses = iter(
        [
            SimpleNamespace(
                status=302,
                headers={"location": "/final"},
                body=b"abc",
                url="https://example.com/start",
            ),
            SimpleNamespace(
                status=200,
                headers={},
                body=b"def",
                url="https://example.com/final",
            ),
        ]
    )

    class Fetcher:
        @staticmethod
        def get(*_args, **_kwargs):
            return next(responses)

    monkeypatch.setattr(
        "scrapeyard.engine.scraper._assert_fetch_url",
        AsyncMock(),
    )
    budget = _budget(5)

    with pytest.raises(BudgetExceeded) as exc_info:
        await _fetch_basic_with_safe_redirects(
            Fetcher,
            "https://example.com/start",
            {},
            {},
            budget=budget,
        )

    assert exc_info.value.limit_name is BudgetLimitName.fetched_bytes
    assert exc_info.value.observed_amount == 6
    assert budget.fetched_bytes == 3


@pytest.mark.asyncio
async def test_retry_response_bodies_are_counted_aggregate(monkeypatch):
    calls = 0

    class Fetcher:
        @staticmethod
        def get(url, **_kwargs):
            nonlocal calls
            calls += 1
            return SimpleNamespace(
                status=503,
                headers={},
                body=b"abc",
                text="retry",
                url=url,
            )

    monkeypatch.setattr(
        "scrapeyard.engine.scraper._assert_fetch_url",
        AsyncMock(),
    )
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    budget = _budget(5)
    budget.sleep = AsyncMock()
    retry = RetryHandler(
        RetryConfig(max_attempts=2, backoff="fixed", backoff_max=1),
        budget=budget,
    )

    with pytest.raises(BudgetExceeded) as exc_info:
        await retry.execute(
            _fetch_page,
            Fetcher,
            target.url,
            target,
            target.fetcher,
            False,
            {503},
            "/tmp/adaptive",
            None,
            None,
            budget,
        )

    assert calls == 2
    assert exc_info.value.limit_name is BudgetLimitName.fetched_bytes
    assert exc_info.value.observed_amount == 6
