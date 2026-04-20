from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.engine.browser_debug import capture_browser_state, default_debug_blob


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

    registered_handlers: dict[str, object] = {}

    def register(event_name: str, handler: object) -> None:
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
