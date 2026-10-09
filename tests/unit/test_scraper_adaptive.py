"""Test that Scrapling adaptive DB path is configured correctly."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scrapeyard.config.schema import FetcherType, RetryConfig, TargetConfig
from scrapeyard.engine.browser_session import BrowserSession
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

    with patch(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(return_value=mock_response),
    ) as fetch:
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

        custom_config = fetch.await_args.args[2]["custom_config"]
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

    with patch(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(return_value=mock_response),
    ):
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

    with patch(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(return_value=mock_response),
    ) as fetch:
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        assert "custom_config" not in fetch.await_args.args[2]


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

    with patch.object(BrowserSession, "fetch", AsyncMock(return_value=mock_response)) as fetch:
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = fetch.await_args.args[1]
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

    with patch.object(BrowserSession, "fetch", AsyncMock(return_value=mock_response)) as fetch:
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = fetch.await_args.args[1]
        assert call_kwargs["timeout"] == 60000
        assert call_kwargs["disable_resources"] is True
        assert call_kwargs["network_idle"] is False


@pytest.mark.asyncio
async def test_stealthy_fetcher_uses_browser_friendly_defaults(tmp_path):
    """Stealthy fetcher should get a longer timeout and only supported browser kwargs."""
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

    with patch.object(BrowserSession, "fetch", AsyncMock(return_value=mock_response)) as fetch:
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = fetch.await_args.args[1]
        assert call_kwargs["timeout"] == 60000
        assert call_kwargs["disable_resources"] is True
        assert call_kwargs["network_idle"] is False
        assert "stealth" not in call_kwargs
        assert "hide_canvas" not in call_kwargs
        assert "useragent" not in call_kwargs


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

    with patch(
        "scrapeyard.engine.scraper.fetch_basic_response",
        AsyncMock(return_value=mock_response),
    ) as fetch:
        await scrape_target(target, adaptive=True, retry=retry, adaptive_dir=str(adaptive_dir))

        custom_config = fetch.await_args.args[2]["custom_config"]
        assert custom_config["storage_args"]["url"] == "https://example.com/"


@pytest.mark.asyncio
async def test_browser_override_changes_fetcher_kwargs(tmp_path):
    """Explicit browser config should override fetch tuning except guarded routing."""
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
            "wait_for_selector": ".product-card a",
            "wait_ms": 1250,
        },
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch.object(BrowserSession, "fetch", AsyncMock(return_value=mock_response)) as fetch:
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = fetch.await_args.args[1]
        assert call_kwargs["timeout"] == 90000
        assert call_kwargs["disable_resources"] is True
        assert call_kwargs["network_idle"] is True
        assert call_kwargs["wait_selector"] == ".product-card a"
        assert call_kwargs["wait"] == 1250


@pytest.mark.asyncio
async def test_browser_click_selector_runs_in_page_action(tmp_path):
    """Browser config may perform a pre-extraction consent click before capture/extraction."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.dynamic,
        browser={
            "click_selector": "button.accept-age-gate",
            "click_timeout_ms": 3000,
            "click_wait_ms": 750,
            "wait_for_selector": ".product-card a",
        },
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch.object(BrowserSession, "fetch", AsyncMock(return_value=mock_response)) as fetch:
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        call_kwargs = fetch.await_args.args[1]
        assert call_kwargs["wait_selector"] == ".product-card a"

        page_action = call_kwargs["page_action"]

        page = MagicMock()
        page.url = "http://example.com"
        page.title = AsyncMock(return_value="Example")
        page.evaluate = AsyncMock(return_value="<html><body>Products</body></html>")
        page.locator.return_value.click = AsyncMock(return_value=None)
        page.wait_for_timeout = AsyncMock(return_value=None)

        await page_action(page)

        page.locator.assert_called_once_with("button.accept-age-gate")
        page.locator.return_value.click.assert_awaited_once_with(timeout=3000)
        page.wait_for_timeout.assert_awaited_once_with(750)


@pytest.mark.asyncio
async def test_browser_click_selector_fails_open_when_absent(tmp_path):
    """Missing consent selectors should not abort browser fetches before extraction."""
    adaptive_dir = tmp_path / "adaptive"

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.css.return_value = []

    target = TargetConfig(
        url="http://example.com",
        fetcher=FetcherType.dynamic,
        browser={
            "click_selector": "button.accept-age-gate",
            "click_timeout_ms": 3000,
            "wait_for_selector": ".product-card a",
        },
        selectors={"title": "h1"},
    )
    retry = RetryConfig()

    with patch.object(BrowserSession, "fetch", AsyncMock(return_value=mock_response)) as fetch:
        await scrape_target(target, adaptive=False, retry=retry, adaptive_dir=str(adaptive_dir))

        page_action = fetch.await_args.args[1]["page_action"]

        page = MagicMock()
        page.url = "http://example.com"
        page.title = AsyncMock(return_value="Example")
        page.evaluate = AsyncMock(return_value="<html><body>Products</body></html>")
        page.locator.return_value.click = AsyncMock(side_effect=TimeoutError())
        page.wait_for_timeout = AsyncMock(return_value=None)

        result = await page_action(page)

        assert result is page
        page.locator.return_value.click.assert_awaited_once_with(timeout=3000)
        page.wait_for_timeout.assert_not_awaited()
