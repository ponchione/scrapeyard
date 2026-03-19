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
        assert custom_config["auto_match"] is True
        assert custom_config["storage_args"]["storage_file"] == str(adaptive_dir / "scrapling.db")
        assert custom_config["storage_args"]["url"] == "http://example.com/"

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
        assert "custom_config" not in call_kwargs.kwargs


@pytest.mark.asyncio
async def test_dynamic_fetcher_adaptive_uses_custom_config_only(tmp_path):
    """Browser-backed adaptive fetches should only pass supported kwargs."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="https://www.example.com/products",
        fetcher=FetcherType.dynamic,
        adaptive_domain="example.com",
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.PlayWrightFetcher") as mock_fetcher:
        mock_fetcher.async_fetch.return_value = mock_response
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = mock_fetcher.async_fetch.call_args.kwargs
        assert "auto_save" not in call_kwargs
        assert "adaptor" not in call_kwargs
        assert call_kwargs["custom_config"]["auto_match"] is True
        assert call_kwargs["custom_config"]["storage_args"]["storage_file"] == str(adaptive_dir / "scrapling.db")
        assert call_kwargs["custom_config"]["storage_args"]["url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_dynamic_fetcher_uses_browser_friendly_defaults(tmp_path):
    """Dynamic fetcher should get a longer timeout and resource suppression."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.PlayWrightFetcher") as mock_fetcher:
        mock_fetcher.async_fetch.return_value = mock_response
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = mock_fetcher.async_fetch.call_args.kwargs
        assert call_kwargs["timeout"] == 60000
        assert call_kwargs["disable_resources"] is True
        assert call_kwargs["network_idle"] is False


@pytest.mark.asyncio
async def test_stealthy_fetcher_uses_browser_friendly_defaults(tmp_path):
    """Stealthy fetcher should get a longer timeout and resource suppression."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.stealthy,
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.StealthyFetcher") as mock_fetcher:
        mock_fetcher.async_fetch.return_value = mock_response
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = mock_fetcher.async_fetch.call_args.kwargs
        assert call_kwargs["timeout"] == 60000
        assert call_kwargs["disable_resources"] is True
        assert call_kwargs["network_idle"] is False


@pytest.mark.asyncio
async def test_adaptive_domain_override_changes_storage_namespace(tmp_path):
    """Adaptive state should use the explicit target namespace when provided."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="https://www.example.com/products",
        fetcher=FetcherType.basic,
        adaptive_domain="example.com",
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.Fetcher") as mock_fetcher:
        mock_fetcher.get.return_value = mock_response
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

        custom_config = mock_fetcher.get.call_args.kwargs["custom_config"]
        assert custom_config["storage_args"]["url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_browser_override_changes_fetcher_kwargs(tmp_path):
    """Explicit browser config should override the built-in defaults."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.dynamic,
        browser={
            "timeout_ms": 90000,
            "disable_resources": False,
            "network_idle": True,
        },
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch("scrapeyard.engine.scraper.PlayWrightFetcher") as mock_fetcher:
        mock_fetcher.async_fetch.return_value = mock_response
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = mock_fetcher.async_fetch.call_args.kwargs
        assert call_kwargs["timeout"] == 90000
        assert call_kwargs["disable_resources"] is False
        assert call_kwargs["network_idle"] is True
