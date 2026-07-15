from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from scrapling.engines import pw as scrapling_pw_engine

from scrapeyard.config.schema import FetcherType, TargetConfig
from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.engine.browser_debug import (
    BrowserPageActionError,
    BrowserTransportTimeout,
    capture_browser_state,
    default_debug_blob,
    fetch_browser_response,
    run_browser_actions,
)
from scrapeyard.engine.url_guard import URLResolutionError, UnsafeURLError
from scrapeyard.queue.browser_limiter import BrowserExecutionLimiter


def _debug_budget(max_bytes: int) -> RunBudget:
    return RunBudget(
        max_duration_seconds=60,
        max_fetched_bytes=1000,
        max_extracted_records=100,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=max_bytes,
    )


async def test_browser_timeout_translation_preserves_cause_and_debug() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    upstream = PlaywrightTimeoutError("page.goto timed out")
    upstream.debug = {"navigation_stage": "goto"}

    class TimeoutFetcher:
        @classmethod
        async def async_fetch(cls, _url, **_kwargs):
            raise upstream

    with pytest.raises(BrowserTransportTimeout) as exc_info:
        await fetch_browser_response(
            TimeoutFetcher,
            target.url,
            target,
            FetcherType.dynamic,
            {},
            None,
        )

    assert exc_info.value.__cause__ is upstream
    assert exc_info.value.debug == {"navigation_stage": "goto"}


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
    def __init__(
        self,
        url: str,
        resource_type: str,
        headers: dict[str, str] | None = None,
    ):
        self.request = SimpleNamespace(
            url=url,
            resource_type=resource_type,
            headers=headers or {},
        )
        self.aborted = False
        self.continued = False
        self.continued_headers: dict[str, str] | None = None

    async def abort(self):
        self.aborted = True

    async def continue_(self, **kwargs):
        self.continued = True
        self.continued_headers = kwargs.get("headers")


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
            "additional_arguments": {"locale": "en-US"},
        },
    )

    debug = default_debug_blob(FetcherType.dynamic, target, target.url)

    assert debug["browser_settings"]["extra_headers"] == {
        "Authorization": "<redacted>",
        "X-Test": "<redacted>",
    }
    assert debug["browser_settings"]["additional_arguments"] == {"locale": "en-US"}


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
async def test_fetch_browser_response_raises_required_action_failures_swallowed_by_fetcher() -> (
    None
):
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
    ("request_url", "expect_credentials"),
    [
        ("https://example.com/redirected", True),
        ("https://example.com:443/redirected", True),
        ("https://sub.example.com/resource", False),
        ("https://attacker.example/resource", False),
        ("http://example.com/downgrade", False),
    ],
)
async def test_browser_extra_headers_are_scoped_to_exact_target_origin(
    request_url: str,
    expect_credentials: bool,
) -> None:
    target = TargetConfig(
        url="https://example.com/start",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={
            "disable_resources": False,
            "extra_headers": {
                "Authorization": "Bearer target-secret",
                "X-API-Key": "api-secret",
                "X-Shared": "deployment-secret",
            },
        },
    )
    route = FakeRoute(
        request_url,
        "document",
        headers={
            "Accept": "text/html",
            "Authorization": "Bearer target-secret",
            "X-API-Key": "api-secret",
            "X-Shared": "deployment-secret",
        },
    )

    class RouteFetcher:
        @staticmethod
        async def async_fetch(url: str, **_kwargs):
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

    assert route.continued is True
    if expect_credentials:
        assert route.continued_headers is None
    else:
        assert route.continued_headers == {"Accept": "text/html"}


@pytest.mark.asyncio
async def test_blocked_browser_request_diagnostics_are_bounded_and_redacted() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"disable_resources": False},
    )
    routes = [
        FakeRoute(
            "http://user:password@127.0.0.1/" + ("x" * 400) + f"?api_key=secret-{index}",
            "document",
        )
        for index in range(25)
    ]

    class RouteFetcher:
        @staticmethod
        async def async_fetch(url: str, **kwargs):
            last_error: UnsafeURLError | None = None
            for route in routes:
                try:
                    await scrapling_pw_engine.async_intercept_route(route)
                except UnsafeURLError as exc:
                    last_error = exc
            assert last_error is not None
            raise last_error

    with pytest.raises(UnsafeURLError) as exc_info:
        await fetch_browser_response(
            RouteFetcher,
            target.url,
            target,
            FetcherType.dynamic,
            {},
            artifacts_dir=None,
        )

    blocked = exc_info.value.debug["blocked_requests"]
    assert len(blocked) == 20
    assert blocked[0]["url"].endswith("...")
    assert all(len(entry["url"]) <= 300 for entry in blocked)
    assert all("user:password" not in entry["url"] for entry in blocked)
    assert all("secret-" not in entry["url"] for entry in blocked)


@pytest.mark.asyncio
async def test_fetch_browser_response_can_require_resolved_browser_route_dns(monkeypatch) -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
        browser={"disable_resources": False},
    )
    route = FakeRoute("https://unresolved.example/pixel.png", "image")

    def _raise_gaierror(*_args, **_kwargs):
        raise socket.gaierror

    class RouteFetcher:
        @staticmethod
        async def async_fetch(url: str, **kwargs):
            await scrapling_pw_engine.async_intercept_route(route)
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    monkeypatch.setattr("scrapeyard.engine.url_guard.socket.getaddrinfo", _raise_gaierror)

    with pytest.raises(URLResolutionError, match="could not be resolved"):
        await fetch_browser_response(
            RouteFetcher,
            target.url,
            target,
            FetcherType.dynamic,
            {},
            artifacts_dir=None,
            require_resolved_dns=True,
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


@pytest.mark.asyncio
async def test_browser_debug_budget_omits_screenshot_that_does_not_fit(tmp_path) -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    capture = default_debug_blob(FetcherType.dynamic, target, target.url)
    page = MagicMock()
    page.url = target.url
    page.title = AsyncMock(return_value="Example")
    page.content = AsyncMock(return_value="abc")
    page.screenshot = AsyncMock(return_value=b"1234")
    budget = _debug_budget(5)

    await capture_browser_state(
        page,
        browser=target.browser,
        fetcher_type=FetcherType.dynamic,
        artifacts_dir=str(tmp_path / "artifacts"),
        capture=capture,
        budget=budget,
    )

    assert capture["html_excerpt"] == "abc"
    assert capture["screenshot_path"] is None
    assert capture["debug_artifact_limits"] == [
        {
            "limit_name": "browser_debug_bytes",
            "configured_limit": 5,
            "requested_amount": 4,
            "stored_amount": 0,
            "artifact": "screenshot",
            "action": "omitted",
        }
    ]
    assert list(tmp_path.rglob("*.png")) == []
    assert budget.browser_debug_bytes == 3


@pytest.mark.asyncio
async def test_browser_debug_budget_truncates_excerpt_at_utf8_boundary() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    capture = default_debug_blob(FetcherType.dynamic, target, target.url)
    page = MagicMock()
    page.url = target.url
    page.title = AsyncMock(return_value="Example")
    page.content = AsyncMock(return_value="cafés")
    budget = _debug_budget(4)

    await capture_browser_state(
        page,
        browser=target.browser,
        fetcher_type=FetcherType.dynamic,
        artifacts_dir=None,
        capture=capture,
        budget=budget,
    )

    assert capture["html_excerpt"] == "caf"
    assert capture["debug_artifact_limits"][0]["action"] == "truncated"
    assert capture["debug_artifact_limits"][0]["requested_amount"] == 6
    assert capture["debug_artifact_limits"][0]["stored_amount"] == 4
    assert budget.browser_debug_bytes == 4


@pytest.mark.asyncio
async def test_browser_screenshot_exact_boundary_is_written_atomically(tmp_path) -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    capture = default_debug_blob(FetcherType.dynamic, target, target.url)
    page = MagicMock()
    page.url = target.url
    page.title = AsyncMock(return_value="Example")
    page.content = AsyncMock(return_value="")
    page.screenshot = AsyncMock(return_value=b"1234")
    budget = _debug_budget(4)

    await capture_browser_state(
        page,
        browser=target.browser,
        fetcher_type=FetcherType.dynamic,
        artifacts_dir=str(tmp_path / "artifacts"),
        capture=capture,
        budget=budget,
    )

    screenshot_path = Path(capture["screenshot_path"])
    assert screenshot_path.read_bytes() == b"1234"
    assert list(screenshot_path.parent.glob(".*.tmp")) == []
    assert budget.browser_debug_bytes == 4


@pytest.mark.asyncio
async def test_browser_budget_retains_limiter_until_cancellation_acknowledged() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    budget = RunBudget(
        max_duration_seconds=0.01,
        max_fetched_bytes=1000,
        max_extracted_records=100,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1024,
    )
    limiter = BrowserExecutionLimiter(1)
    cancellation_seen = asyncio.Event()
    release_fetch = asyncio.Event()
    second_acquired = asyncio.Event()

    class CancellationResistantFetcher:
        @staticmethod
        async def async_fetch(url: str, **_kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_fetch.wait()
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    async def first_browser() -> None:
        async with limiter.slot():
            await fetch_browser_response(
                CancellationResistantFetcher,
                target.url,
                target,
                FetcherType.dynamic,
                {},
                artifacts_dir=None,
                budget=budget,
            )

    async def second_browser() -> None:
        async with limiter.slot():
            second_acquired.set()

    first = asyncio.create_task(first_browser())
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
    second = asyncio.create_task(second_browser())
    await asyncio.sleep(0)
    assert limiter.active == 1
    assert first.done() is False
    assert second_acquired.is_set() is False

    release_fetch.set()
    with pytest.raises(BudgetExceeded) as exc_info:
        await first
    await second

    assert exc_info.value.limit_name is BudgetLimitName.run_duration_seconds
    assert second_acquired.is_set()
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_browser_limiter_survives_repeated_outer_cancellation() -> None:
    target = TargetConfig(
        url="https://example.com",
        fetcher=FetcherType.dynamic,
        selectors={"title": "h1"},
    )
    budget = RunBudget(
        max_duration_seconds=10,
        max_fetched_bytes=1000,
        max_extracted_records=100,
        max_serialized_result_bytes=4096,
        max_browser_debug_bytes=1024,
    )
    limiter = BrowserExecutionLimiter(1)
    fetch_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_fetch = asyncio.Event()
    second_acquired = asyncio.Event()

    class CancellationResistantFetcher:
        @staticmethod
        async def async_fetch(url: str, **_kwargs):
            fetch_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_fetch.wait()
            return SimpleNamespace(status=200, url=url, text="<html>ok</html>")

    async def first_browser() -> None:
        async with limiter.slot():
            await fetch_browser_response(
                CancellationResistantFetcher,
                target.url,
                target,
                FetcherType.dynamic,
                {},
                artifacts_dir=None,
                budget=budget,
            )

    async def second_browser() -> None:
        async with limiter.slot():
            second_acquired.set()

    first = asyncio.create_task(first_browser())
    await fetch_started.wait()
    first.cancel()
    await cancellation_seen.wait()
    first.cancel()
    second = asyncio.create_task(second_browser())
    await asyncio.sleep(0)

    assert not first.done()
    assert not second_acquired.is_set()
    assert limiter.active == 1

    release_fetch.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert limiter.active == 0
