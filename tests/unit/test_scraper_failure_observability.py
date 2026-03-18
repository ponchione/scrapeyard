"""Tests for scraper failure classification and observability metadata."""

from __future__ import annotations

import asyncio

import pytest

from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.scraper import FetchError, scrape_target
from scrapeyard.models.job import ErrorType


def _target(fetcher: FetcherType = FetcherType.basic) -> TargetConfig:
    return TargetConfig(
        url="https://example.com",
        fetcher=fetcher,
        selectors={"title": "h1"},
    )


@pytest.mark.asyncio
async def test_scrape_target_classifies_http_status(monkeypatch, tmp_path):
    async def _raise_fetch_error(*_args, **_kwargs):
        raise FetchError(403)

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _raise_fetch_error)

    result = await scrape_target(
        _target(FetcherType.basic),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "failed"
    assert result.error_type == ErrorType.http_error
    assert result.http_status == 403
    assert result.error_detail == "FetchError: HTTP 403"


@pytest.mark.asyncio
async def test_scrape_target_classifies_timeout(monkeypatch, tmp_path):
    async def _raise_timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _raise_timeout)

    result = await scrape_target(
        _target(FetcherType.basic),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "failed"
    assert result.error_type == ErrorType.timeout
    assert result.http_status is None
    assert result.error_detail == "TimeoutError"


@pytest.mark.asyncio
async def test_scrape_target_classifies_browser_failures(monkeypatch, tmp_path):
    async def _raise_browser_error(*_args, **_kwargs):
        raise RuntimeError("Executable doesn't exist at /ms-playwright/chromium")

    monkeypatch.setattr("scrapeyard.engine.scraper._fetch_page", _raise_browser_error)

    result = await scrape_target(
        _target(FetcherType.dynamic),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(tmp_path),
    )

    assert result.status == "failed"
    assert result.error_type == ErrorType.browser_error
    assert result.http_status is None
    assert "Executable doesn't exist" in result.error_detail
