from __future__ import annotations

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.engine.browser_debug import (
    capture_browser_state,
    default_debug_blob,
    run_browser_actions,
)


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
