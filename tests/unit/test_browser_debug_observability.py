from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from scrapling.engines import pw as scrapling_pw_engine

from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.engine.browser_debug import (
    BrowserPageActionError,
    capture_browser_state,
    default_debug_blob,
    fetch_browser_response,
    run_browser_actions,
)
from scrapeyard.engine.url_guard import UnsafeURLError


class FakeConsoleMessage:
    def __init__(self, msg_type: str, text: str):
        self.type = msg_type
        self.text = text


class FakeRequestFailure:
    def __init__(self, error_text: str):
        self.error_text = error_text


class FakeRequest:
    def __init__(self, url: str, method: str, resource_type: str, error_text: str):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self._failure = FakeRequestFailure(error_text)

    def failure(self):
        return self._failure


class FakeRoute:
    def __init__(self, url: str, resource_type: str):
        self.request = SimpleNamespace(url=url, resource_type=resource_type)
        self.aborted = False
        self.continued = False

    async def abort(self):
        self.aborted = True

    async def continue_(self):
        self.continued = True


@pytest.mark.asyncio
@pytest.mark.parametrize("fetcher_type", [FetcherType.dynamic, FetcherType.stealthy])
async def test_capture_browser_state_collects_bounded_console_and_request_failures(
    fetcher_type: FetcherType,
) -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=fetcher_type,
        selectors={"title": "h1"},
    )
    capture = default_debug_blob(fetcher_type, target, target.url)

    page = MagicMock()
    page.url = "https://example.com/final"
    page.title = AsyncMock(return_value="Example")
    page.content = AsyncMock(return_value="<html>ok</html>")
    page.on = MagicMock()

    registered_handlers: dict[str, Callable[[object], None]] = {}

    def register(event_name: str, handler: Callable[[object], None]) -> None:
        registered_handlers[event_name] = handler

    page.on.side_effect = register

    await capture_browser_state(
        page,
        browser=target.browser,
        fetcher_type=FetcherType.dynamic,
        artifacts_dir=None,
        capture=capture,
    )

    assert "console" in registered_handlers
    assert "requestfailed" in registered_handlers

    console_handler = registered_handlers["console"]
    requestfailed_handler = registered_handlers["requestfailed"]

    long_text = "x" * 500
    for idx in range(25):
        console_handler(FakeConsoleMessage("warning", f"console-{idx}-{long_text}"))
        requestfailed_handler(
            FakeRequest(
                url=f"https://cdn.example.com/asset-{idx}.js",
                method="GET",
                resource_type="script",
                error_text=f"failure-{idx}-{long_text}",
            )
        )

    assert len(capture["console_messages"]) == 20
    assert len(capture["request_failures"]) == 20
    assert capture["console_messages"][0]["text"].startswith("console-5-")
    assert capture["console_messages"][-1]["text"].startswith("console-24-")
    assert capture["request_failures"][0]["url"] == "https://cdn.example.com/asset-5.js"
    assert capture["request_failures"][-1]["error_text"].startswith("failure-24-")
    assert len(capture["console_messages"][-1]["text"]) < len(f"console-24-{long_text}")
    assert len(capture["request_failures"][-1]["error_text"]) < len(f"failure-24-{long_text}")


@pytest.mark.asyncio
async def test_capture_browser_state_redacts_observability_urls() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    capture = default_debug_blob(FetcherType.dynamic, target, target.url)
    page = MagicMock()
    page.url = "https://example.com/final"
    page.title = AsyncMock(return_value="Example")
    page.content = AsyncMock(return_value="<html>ok</html>")
    page.on = MagicMock()
    registered_handlers: dict[str, Callable[[object], None]] = {}
    page.on.side_effect = lambda event_name, handler: registered_handlers.setdefault(
        event_name,
        handler,
    )

    await capture_browser_state(
        page,
        browser=target.browser,
        fetcher_type=FetcherType.dynamic,
        artifacts_dir=None,
        capture=capture,
    )

    registered_handlers["console"](
        FakeConsoleMessage("error", "failed https://user:pass@example.com?api_key=secret")
    )
    registered_handlers["requestfailed"](
        FakeRequest(
            url="https://user:pass@cdn.example.com/asset.js?access_token=secret",
            method="GET",
            resource_type="script",
            error_text="failed https://example.com/asset.js?session_id=abc",
        )
    )

    assert capture["console_messages"][0]["text"] == (
        "failed https://example.com?api_key=<redacted>"
    )
    assert capture["request_failures"][0]["url"] == (
        "https://cdn.example.com/asset.js?access_token=<redacted>"
    )
    assert capture["request_failures"][0]["error_text"] == (
        "failed https://example.com/asset.js?session_id=<redacted>"
    )


def test_default_debug_blob_includes_empty_observability_collections() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )

    debug = default_debug_blob(FetcherType.dynamic, target, target.url)

    assert debug["console_messages"] == []
    assert debug["request_failures"] == []


def test_default_debug_blob_redacts_sensitive_browser_settings() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={
            "extra_headers": {
                "Authorization": "Bearer secret",
                "X-Test": "visible",
            },
            "additional_arguments": {"api_token": "secret"},
        },
    )

    debug = default_debug_blob(FetcherType.dynamic, target, target.url)

    assert debug["browser_settings"]["extra_headers"] == {
        "Authorization": "<redacted>",
        "X-Test": "visible",
    }
    assert debug["browser_settings"]["additional_arguments"] == {"api_token": "<redacted>"}


@pytest.mark.asyncio
async def test_capture_browser_state_runs_configured_browser_actions() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={
            "actions": [
                {"type": "click", "selector": "#accept", "timeout_ms": 1000, "wait_ms": 50},
                {
                    "type": "wait_for_selector",
                    "selector": ".product-card",
                    "timeout_ms": 2000,
                },
                {"type": "scroll", "times": 2, "pixels": 800, "wait_ms": 10},
                {
                    "type": "repeat_click",
                    "selector": "button.load-more",
                    "max_times": 2,
                    "wait_for_selector": ".product-card",
                    "wait_ms": 25,
                    "optional": True,
                },
            ]
        },
    )
    capture = default_debug_blob(FetcherType.dynamic, target, target.url)

    page = MagicMock()
    page.url = "https://example.com/final"
    page.title = AsyncMock(return_value="Example")
    page.content = AsyncMock(return_value="<html>ok</html>")
    page.locator.return_value.click = AsyncMock(return_value=None)
    page.wait_for_selector = AsyncMock(return_value=None)
    page.wait_for_timeout = AsyncMock(return_value=None)
    page.mouse.wheel = AsyncMock(return_value=None)

    await capture_browser_state(
        page,
        browser=target.browser,
        fetcher_type=FetcherType.dynamic,
        artifacts_dir=None,
        capture=capture,
    )

    assert page.locator.call_args_list[0].args == ("#accept",)
    assert page.locator.call_args_list[1].args == ("button.load-more",)
    assert page.locator.call_args_list[2].args == ("button.load-more",)
    assert page.locator.return_value.click.await_count == 3
    assert page.wait_for_selector.await_count == 3
    assert page.wait_for_selector.await_args_list[0].args == (".product-card",)
    assert page.mouse.wheel.await_count == 2
    assert page.wait_for_timeout.await_count == 5


@pytest.mark.asyncio
async def test_click_selector_omits_timeout_when_configured_as_none() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"click_selector": "#accept", "click_timeout_ms": None},
    )
    capture = default_debug_blob(FetcherType.dynamic, target, target.url)

    page = MagicMock()
    page.url = target.url
    page.title = AsyncMock(return_value="Example")
    page.content = AsyncMock(return_value="<html>ok</html>")
    page.locator.return_value.click = AsyncMock(return_value=None)

    await capture_browser_state(
        page,
        browser=target.browser,
        fetcher_type=FetcherType.dynamic,
        artifacts_dir=None,
        capture=capture,
    )

    page.locator.assert_called_once_with("#accept")
    page.locator.return_value.click.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_optional_repeat_click_stops_without_raising_when_button_disappears() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={
            "actions": [
                {
                    "type": "repeat_click",
                    "selector": "button.load-more",
                    "max_times": 3,
                    "optional": True,
                }
            ]
        },
    )
    page = MagicMock()
    page.locator.return_value.click = AsyncMock(side_effect=TimeoutError())

    assert target.browser is not None
    await run_browser_actions(page, target.browser.actions)

    page.locator.assert_called_once_with("button.load-more")
    page.locator.return_value.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_browser_response_raises_required_action_failures_swallowed_by_fetcher() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"actions": [{"type": "click", "selector": "#accept"}]},
    )
    page = MagicMock()
    page.locator.return_value.click = AsyncMock(side_effect=RuntimeError("button missing"))

    class SwallowingFetcher:
        @staticmethod
        async def async_fetch(url: str, **kwargs):
            try:
                await kwargs["page_action"](page)
            except Exception:
                pass
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    with pytest.raises(BrowserPageActionError, match="button missing") as exc_info:
        await fetch_browser_response(
            SwallowingFetcher,
            target.url,
            target,
            FetcherType.dynamic,
            {},
            artifacts_dir=None,
        )

    assert exc_info.value.debug["page_action_error"]["exception_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_fetch_browser_response_redacts_page_action_exception_text() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"actions": [{"type": "click", "selector": "#accept"}]},
    )
    page = MagicMock()
    page.locator.return_value.click = AsyncMock(
        side_effect=RuntimeError("failed https://user:pass@example.com/hook?api_key=secret")
    )

    class SwallowingFetcher:
        @staticmethod
        async def async_fetch(url: str, **kwargs):
            try:
                await kwargs["page_action"](page)
            except Exception:
                pass
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    with pytest.raises(BrowserPageActionError) as exc_info:
        await fetch_browser_response(
            SwallowingFetcher,
            target.url,
            target,
            FetcherType.dynamic,
            {},
            artifacts_dir=None,
        )

    message = str(exc_info.value)
    debug_message = exc_info.value.debug["page_action_error"]["message"]
    assert "user:pass" not in message
    assert "api_key=secret" not in message
    assert "user:pass" not in debug_message
    assert "api_key=secret" not in debug_message
    assert "https://example.com/hook?api_key=<redacted>" in message


@pytest.mark.asyncio
async def test_fetch_browser_response_blocks_non_public_browser_routes() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"disable_resources": False},
    )
    route = FakeRoute("http://127.0.0.1/private", "document")

    class RouteFetcher:
        @staticmethod
        async def async_fetch(url: str, **kwargs):
            assert kwargs["disable_resources"] is True
            await scrapling_pw_engine.async_intercept_route(route)
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    with pytest.raises(UnsafeURLError, match="non-public"):
        await fetch_browser_response(
            RouteFetcher,
            target.url,
            target,
            FetcherType.dynamic,
            {},
            artifacts_dir=None,
        )

    assert route.aborted is True
    assert route.continued is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("browser_config", "expected_aborted", "expected_continued"),
    [
        ({"disable_resources": False}, False, True),
        (None, True, False),
    ],
)
async def test_fetch_browser_response_applies_configured_resource_blocking(
    browser_config: dict[str, bool] | None,
    expected_aborted: bool,
    expected_continued: bool,
) -> None:
    route = FakeRoute("https://8.8.8.8/pixel.png", "image")
    target_kwargs = {} if browser_config is None else {"browser": browser_config}
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        **target_kwargs,
    )

    class RouteFetcher:
        @staticmethod
        async def async_fetch(url: str, **kwargs):
            await scrapling_pw_engine.async_intercept_route(route)
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    await fetch_browser_response(
        RouteFetcher,
        target.url,
        target,
        FetcherType.dynamic,
        {},
        artifacts_dir=None,
    )

    assert route.aborted is expected_aborted
    assert route.continued is expected_continued


@pytest.mark.asyncio
async def test_fetch_browser_response_closes_page_after_unsafe_final_url() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    page = MagicMock()
    page.url = "http://127.0.0.1/private"
    page.close = AsyncMock(return_value=None)

    class SwallowingFetcher:
        @staticmethod
        async def async_fetch(url: str, **kwargs):
            try:
                await kwargs["page_action"](page)
            except Exception:
                pass
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    with pytest.raises(UnsafeURLError, match="non-public"):
        await fetch_browser_response(
            SwallowingFetcher,
            target.url,
            target,
            FetcherType.dynamic,
            {},
            artifacts_dir=None,
        )

    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_browser_state_rejects_non_public_final_url_before_content_capture() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    capture = default_debug_blob(FetcherType.dynamic, target, target.url)
    page = MagicMock()
    page.url = "http://127.0.0.1/private"
    page.title = AsyncMock(return_value="Private")
    page.content = AsyncMock(return_value="<html>private</html>")

    with pytest.raises(UnsafeURLError, match="non-public"):
        await capture_browser_state(
            page,
            browser=target.browser,
            fetcher_type=FetcherType.dynamic,
            artifacts_dir=None,
            capture=capture,
        )

    page.title.assert_not_awaited()
    page.content.assert_not_awaited()
