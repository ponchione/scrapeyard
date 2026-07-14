"""Rate-limit coverage at physical top-level request boundaries."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import pytest

from scrapeyard.config.schema import RetryConfig, TargetConfig
from scrapeyard.engine.scraper import scrape_target


class _Node:
    def __init__(self, text: str = "", *, href: str | None = None) -> None:
        self.text = text
        self.attrib = {} if href is None else {"href": href}

    def get_all_text(self) -> str:
        return self.text


class _Response:
    def __init__(
        self,
        *,
        url: str,
        status: int = 200,
        title: str = "result",
        next_url: str | None = None,
    ) -> None:
        self.url = url
        self.status = status
        self.headers: dict[str, str] = {}
        self.body = b"response"
        self.encoding = "utf-8"
        self.text = "response"
        self._title = title
        self._next_url = next_url

    def css(self, selector: str):
        if selector == "h1":
            return [_Node(self._title)]
        if selector == "a.next" and self._next_url is not None:
            return [_Node(href=self._next_url)]
        return []

    def xpath(self, _selector: str):
        return []


@pytest.mark.asyncio
async def test_retry_acquires_once_for_each_network_attempt(monkeypatch, tmp_path):
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/products",
            "selectors": {"title": "h1"},
        }
    )
    responses = iter(
        [
            _Response(url=target.url, status=503),
            _Response(url=target.url, title="ok"),
        ]
    )
    limiter = AsyncMock()
    monkeypatch.setattr(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(side_effect=lambda *_args, **_kwargs: next(responses)),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper._get_fetcher", lambda _fetcher: object())
    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())
    monkeypatch.setattr("scrapeyard.engine.resilience.asyncio.sleep", AsyncMock())

    result = await scrape_target(
        target,
        adaptive=False,
        retry=RetryConfig(max_attempts=2, backoff="fixed", backoff_max=1),
        adaptive_dir=str(tmp_path),
        rate_limiter=limiter,
        domain_rate_limit=9,
    )

    assert result.is_success, result.errors
    assert limiter.acquire.await_args_list == [
        call("example.com", 9),
        call("example.com", 9),
    ]


@pytest.mark.asyncio
async def test_pagination_acquires_for_initial_and_cross_origin_pages(monkeypatch, tmp_path):
    target = TargetConfig.model_validate(
        {
            "url": "https://example.com/page-1",
            "selectors": {"title": "h1"},
            "pagination": {"next": "a.next", "max_pages": 2},
        }
    )
    responses = iter(
        [
            _Response(
                url=target.url,
                title="first",
                next_url="https://other.example/page-2",
            ),
            _Response(url="https://other.example/page-2", title="second"),
        ]
    )
    limiter = AsyncMock()
    monkeypatch.setattr(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(side_effect=lambda *_args, **_kwargs: next(responses)),
    )
    monkeypatch.setattr("scrapeyard.engine.scraper._get_fetcher", lambda _fetcher: object())
    monkeypatch.setattr("scrapeyard.engine.scraper._assert_fetch_url", AsyncMock())
    monkeypatch.setattr(
        "scrapeyard.engine.pagination._pagination_url_is_safe",
        AsyncMock(return_value=True),
    )

    result = await scrape_target(
        target,
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
        rate_limiter=limiter,
        domain_rate_limit=6,
    )

    assert result.is_success, result.errors
    assert result.pages_scraped == 2
    assert [record["title"].splitlines()[0] for record in result.data] == [
        "first",
        "second",
    ]
    assert limiter.acquire.await_args_list == [
        call("example.com", 6),
        call("other.example", 6),
    ]
