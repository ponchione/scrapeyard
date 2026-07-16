from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from scrapling import DynamicFetcher as ScraplingDynamicFetcher
from scrapling import StealthyFetcher as ScraplingStealthyFetcher

from scrapeyard.engine.browser_fetchers import (
    CamoufoxFetcher,
    DynamicFetcher,
    _fetch_chromium,
    _proxy_settings,
)


@pytest.mark.asyncio
async def test_dynamic_fetcher_selects_current_scrapling_sessions() -> None:
    response = object()
    with patch(
        "scrapeyard.engine.browser_fetchers._fetch_chromium",
        AsyncMock(return_value=response),
    ) as fetch:
        assert (
            await DynamicFetcher.async_fetch(
                "https://example.com",
                stealth=False,
                hide_canvas=True,
                humanize=True,
                os_randomize=True,
                geoip=True,
                disable_ads=True,
                additional_arguments={"locale": "en-US"},
                nstbrowser_mode=True,
                timeout=123,
            )
            is response
        )
        kwargs = fetch.await_args.args[1]
        assert kwargs == {"timeout": 123}
        assert fetch.await_args.kwargs["fetcher"] is ScraplingDynamicFetcher

        await DynamicFetcher.async_fetch(
            "https://example.com",
            stealth=True,
            nstbrowser_mode=True,
            hide_canvas=True,
        )
        assert fetch.await_args.args[1] == {"hide_canvas": True}
        assert fetch.await_args.kwargs["fetcher"] is ScraplingStealthyFetcher


@pytest.mark.asyncio
async def test_chromium_adapter_merges_parser_config_and_enables_sandbox() -> None:
    response = object()
    active = SimpleNamespace(fetch=AsyncMock(return_value=response))
    created: list[object] = []

    class Session:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self._browser_options: dict[str, object] = {}
            created.append(self)

        async def __aenter__(self) -> object:
            return active

        async def __aexit__(self, *_args: object) -> None:
            return None

    fetcher = SimpleNamespace(
        _generate_parser_arguments=MagicMock(return_value={"base": True})
    )
    actual = await _fetch_chromium(
        "https://example.com",
        {"custom_config": {"auto_match": True}, "timeout": 500},
        fetcher=fetcher,
        session_type=Session,
    )
    session = created[0]
    assert actual is response
    assert session.kwargs["selector_config"] == {
        "base": True,
        "auto_match": True,
    }
    assert session._browser_options["chromium_sandbox"] is True
    active.fetch.assert_awaited_once_with("https://example.com")


@pytest.mark.asyncio
async def test_chromium_adapter_rejects_changed_private_session_contract() -> None:
    class Session:
        def __init__(self, **_kwargs: object) -> None:
            self._browser_options = None

    fetcher = SimpleNamespace(_generate_parser_arguments=lambda: {})
    with pytest.raises(RuntimeError, match="launch options"):
        await _fetch_chromium(
            "https://example.com",
            {},
            fetcher=fetcher,
            session_type=Session,
        )
    with pytest.raises(TypeError, match="selector_config"):
        await _fetch_chromium(
            "https://example.com",
            {"selector_config": "invalid"},
            fetcher=fetcher,
            session_type=Session,
        )


@pytest.mark.asyncio
async def test_chromium_cdp_session_does_not_mutate_launch_sandbox() -> None:
    active = SimpleNamespace(fetch=AsyncMock(return_value=object()))
    created: list[object] = []

    class Session:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self._browser_options: dict[str, object] = {}
            created.append(self)

        async def __aenter__(self) -> object:
            return active

        async def __aexit__(self, *_args: object) -> None:
            return None

    await _fetch_chromium(
        "https://example.com",
        {"cdp_url": "wss://browser.example"},
        fetcher=SimpleNamespace(_generate_parser_arguments=lambda: {}),
        session_type=Session,
    )
    assert created[0]._browser_options == {}


@pytest.mark.asyncio
async def test_camoufox_adapter_preserves_navigation_and_page_controls() -> None:
    page_setup = AsyncMock()
    page_action = AsyncMock()
    locator = SimpleNamespace(first=SimpleNamespace(wait_for=AsyncMock()))
    navigation = SimpleNamespace(
        status=201,
        status_text="Created",
        all_headers=AsyncMock(return_value={"content-type": "text/html"}),
        request=SimpleNamespace(
            all_headers=AsyncMock(return_value={"accept": "text/html"})
        ),
    )
    page = SimpleNamespace(
        url="https://example.com/final",
        set_default_navigation_timeout=MagicMock(),
        set_default_timeout=MagicMock(),
        set_extra_http_headers=AsyncMock(),
        goto=AsyncMock(return_value=navigation),
        wait_for_load_state=AsyncMock(),
        locator=MagicMock(return_value=locator),
        wait_for_timeout=AsyncMock(),
        content=AsyncMock(return_value="<html>ok</html>"),
    )
    context = SimpleNamespace(
        new_page=AsyncMock(return_value=page),
        cookies=AsyncMock(return_value=[{"name": "session", "value": "safe"}]),
        close=AsyncMock(),
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context))
    launch_options: dict[str, object] = {}

    class CamoufoxContext:
        async def __aenter__(self) -> object:
            return browser

        async def __aexit__(self, *_args: object) -> None:
            return None

    def camoufox(**kwargs: object) -> CamoufoxContext:
        launch_options.update(kwargs)
        return CamoufoxContext()

    with patch("scrapeyard.engine.browser_fetchers.AsyncCamoufox", camoufox):
        response = await CamoufoxFetcher.async_fetch(
            "https://example.com/start",
            timeout=1234,
            disable_resources=True,
            network_idle=True,
            page_setup=page_setup,
            page_action=page_action,
            wait_selector="h1",
            wait_selector_state="visible",
            wait=25,
            extra_headers={"X-Test": "safe"},
            useragent="Scrapeyard-Test",
            proxy="socks5://proxy.example:1080",
            headless=False,
            humanize=True,
            geoip=False,
            block_webrtc=True,
            hide_canvas=True,
            additional_arguments={"locale": "fr-FR", "fonts": ["Arial"]},
        )

    assert response.status == 201
    assert response.url == "https://example.com/final"
    assert response.cookies == {"session": "safe"}
    browser.new_context.assert_awaited_once_with(no_viewport=True)
    assert launch_options == {
        "headless": False,
        "proxy": {"server": "socks5://proxy.example:1080"},
        "humanize": True,
        "geoip": False,
        "block_webrtc": True,
        "allow_webgl": False,
        "locale": "fr-FR",
        "fonts": ["Arial"],
        "config": {"navigator.userAgent": "Scrapeyard-Test"},
        "i_know_what_im_doing": True,
    }
    page.set_default_navigation_timeout.assert_called_once_with(1234.0)
    page.set_default_timeout.assert_called_once_with(1234.0)
    page.set_extra_http_headers.assert_awaited_once_with({"X-Test": "safe"})
    page.wait_for_load_state.assert_any_await("domcontentloaded")
    page.wait_for_load_state.assert_any_await("networkidle")
    locator.first.wait_for.assert_awaited_once_with(state="visible")
    page.wait_for_timeout.assert_awaited_once_with(25.0)
    context.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("http://proxy.example", {"server": "http://proxy.example"}),
        ({"server": "socks5://proxy", "username": 123}, {"server": "socks5://proxy", "username": "123"}),
    ],
)
def test_camoufox_proxy_conversion(value: object, expected: object) -> None:
    assert _proxy_settings(value) == expected


def test_camoufox_proxy_conversion_rejects_unknown_type() -> None:
    with pytest.raises(TypeError, match="URL string or mapping"):
        _proxy_settings(123)
