"""Test that Scrapling adaptive DB path is configured correctly."""

from unittest.mock import MagicMock, patch

import pytest

from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.scraper import scrape_target


@pytest.mark.asyncio
async def test_adaptive_db_path_passed_to_fetcher(tmp_path):
    """Verify that the adaptive DB path is passed via storage_args."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = mock_response
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = mock_fetcher.get.call_args
        custom_config = call_kwargs.kwargs.get("custom_config") or call_kwargs[1].get("custom_config")
        assert call_kwargs.kwargs["auto_save"] is True
        assert call_kwargs.kwargs["adaptor"] is True
        assert custom_config["auto_match"] is True
        assert custom_config["storage_args"]["storage_file"] == str(adaptive_dir / "scrapling.db")

    assert adaptive_dir.exists()


@pytest.mark.asyncio
async def test_adaptive_dir_created_if_missing(tmp_path):
    """Verify that the adaptive_dir is created if it does not exist."""
    adaptive_dir = tmp_path / "does_not_exist" / "adaptive"
    assert not adaptive_dir.exists()

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = mock_response
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

    assert adaptive_dir.exists()


@pytest.mark.asyncio
async def test_adaptive_false_passes_no_adaptive_kwargs(tmp_path):
    """When adaptive=False, no adaptive kwargs are sent to the fetcher."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.basic,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = mock_response
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = mock_fetcher.get.call_args
        assert "auto_save" not in call_kwargs.kwargs
        assert "adaptor" not in call_kwargs.kwargs
        assert "custom_config" not in call_kwargs.kwargs
