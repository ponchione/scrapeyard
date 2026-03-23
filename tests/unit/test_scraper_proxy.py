"""Unit tests for proxy URL passthrough in fetch and scrape functions."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from scrapeyard.config.schema import RetryConfig, TargetConfig
from scrapeyard.engine.scraper import scrape_target


def _target(**overrides) -> TargetConfig:
    base = {"url": "https://example.com", "selectors": {"title": "h1"}}
    base.update(overrides)
    return TargetConfig(**base)


@pytest.mark.asyncio
async def test_scrape_target_passes_proxy_to_fetch_page(tmp_path):
    """When proxy_url is set, _fetch_page receives it and injects proxy into call_kwargs."""
    target = _target(fetcher="basic")
    retry = RetryConfig()
    captured_kwargs = {}

    def fake_get(url, **kwargs):
        captured_kwargs.update(kwargs)
        resp = MagicMock()
        resp.status = 200
        resp.css = MagicMock(return_value=[MagicMock(text="Title")])
        return resp

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get = fake_get
        result = await scrape_target(
            target, adaptive=False, retry=retry,
            adaptive_dir=str(tmp_path), proxy_url="http://proxy:8080",
        )

    assert result.status == "success"
    assert captured_kwargs.get("proxy") == "http://proxy:8080"


@pytest.mark.asyncio
async def test_scrape_target_no_proxy_by_default(tmp_path):
    """When proxy_url is None (default), no proxy kwarg is injected."""
    target = _target(fetcher="basic")
    retry = RetryConfig()
    captured_kwargs = {}

    def fake_get(url, **kwargs):
        captured_kwargs.update(kwargs)
        resp = MagicMock()
        resp.status = 200
        resp.css = MagicMock(return_value=[MagicMock(text="Title")])
        return resp

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get = fake_get
        result = await scrape_target(
            target, adaptive=False, retry=retry,
            adaptive_dir=str(tmp_path),
        )

    assert result.status == "success"
    assert "proxy" not in captured_kwargs
