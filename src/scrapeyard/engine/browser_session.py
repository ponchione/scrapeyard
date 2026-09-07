"""One target's browser lifetime, using the pinned Scrapling engine settings."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from contextvars import copy_context
from typing import Any, cast

import httpx
from camoufox.async_api import AsyncCamoufox
from scrapling import PlayWrightFetcher
from scrapling.engines.camo import CamoufoxEngine
from scrapling.engines.pw import PlaywrightEngine
from scrapling.engines.toolbelt.custom import Response, StatusText
from scrapling.engines.toolbelt.fingerprints import generate_convincing_referer

logger = logging.getLogger(__name__)


async def _close_resource(resource: Any, browser: Any) -> None:
    try:
        await resource.close()
    except Exception:
        # Rebrowser raises on context.close() after a browser disconnect.
        # The remaining stack must still stop the driver before a retry.
        if browser.is_connected():
            raise


class BrowserSession:
    """Reuse a context, but create a fresh page and callbacks for every fetch."""

    def __init__(self, fetcher_cls: Any) -> None:
        self.fetcher_cls = fetcher_cls
        self._stack = AsyncExitStack()
        self._context: Any = None

    async def aclose(self) -> None:
        self._context = None
        await self._stack.aclose()

    async def _open(self, engine: Any) -> None:
        if self.fetcher_cls is PlayWrightFetcher:
            if not engine.stealth or engine.real_chrome:
                from playwright import async_api
                runtime_manager: Any = async_api.async_playwright()
            else:
                from rebrowser_playwright.async_api import async_playwright
                runtime_manager = async_playwright()

            # Register before startup: cancellation can interrupt __aenter__
            # after the driver starts, before enter_async_context would own it.
            self._stack.push_async_exit(runtime_manager)
            runtime = await runtime_manager.__aenter__()
            if engine.cdp_url:
                browser = await runtime.chromium.connect_over_cdp(engine._cdp_url_logic())
                # Stopping the driver disconnects CDP; never close the remote browser.
            else:
                browser = await runtime.chromium.launch(**engine._PlaywrightEngine__launch_kwargs())
                self._stack.push_async_callback(_close_resource, browser, browser)
            options = engine._PlaywrightEngine__context_kwargs()
        else:
            manager = cast(Any, AsyncCamoufox)(**engine._get_camoufox_options())
            self._stack.push_async_exit(manager)
            browser = await manager.__aenter__()
            options = {}
        context = await browser.new_context(**options)
        self._stack.push_async_callback(_close_resource, context, browser)
        self._context = context

    async def fetch(
        self,
        url: str,
        call_kwargs: dict[str, Any],
        route_handler: Callable[[Any], Awaitable[None]],
    ) -> Any:
        kwargs = dict(call_kwargs)
        custom_config = kwargs.pop("custom_config", None) or {}
        engine_cls = PlaywrightEngine if self.fetcher_cls is PlayWrightFetcher else CamoufoxEngine
        engine: Any = engine_cls(
            **kwargs,
            adaptor_arguments={**self.fetcher_cls._generate_parser_arguments(), **custom_config},
        )
        if self._context is not None and not self._context.browser.is_connected():
            raise httpx.NetworkError("Browser session disconnected")
        if self._context is None:
            await self._open(engine)

        # Playwright's dispatcher survives page fetches and inherits the context
        # of its creation. Bind each route to THIS page's budget/diagnostics.
        route_context = copy_context()

        async def guard(route: Any) -> None:
            await route_context.run(asyncio.ensure_future, route_handler(route))

        browser = self._context.browser
        page = None
        try:
            page = await self._context.new_page()
            page.set_default_navigation_timeout(engine.timeout)
            page.set_default_timeout(engine.timeout)
            final_response = None

            def handle_response(response: Any) -> None:
                nonlocal final_response
                if response.request.resource_type == "document" and response.request.is_navigation_request():
                    final_response = response

            page.on("response", handle_response)
            if engine.extra_headers:
                await page.set_extra_http_headers(engine.extra_headers)
            await page.route("**/*", guard)
            if self.fetcher_cls is PlayWrightFetcher and engine.stealth:
                for script in engine._PlaywrightEngine__stealth_scripts():
                    await page.add_init_script(path=script)

            first_response = await page.goto(
                url, referer=generate_convincing_referer(url) if engine.google_search else None,
            )
            await page.wait_for_load_state("domcontentloaded")
            if engine.network_idle:
                await page.wait_for_load_state("networkidle")
            if engine.page_action is not None:
                page = await engine.page_action(page)
            if engine.wait_selector:
                # Preserve Scrapling's optional selector-wait behavior.
                try:
                    await page.locator(engine.wait_selector).first.wait_for(state=engine.wait_selector_state)
                    await page.wait_for_load_state("load")
                    await page.wait_for_load_state("domcontentloaded")
                    if engine.network_idle:
                        await page.wait_for_load_state("networkidle")
                except Exception:
                    logger.info("Optional browser selector wait did not complete")
            await page.wait_for_timeout(engine.wait)
            final_response = final_response or first_response
            if final_response is None:
                raise ValueError("Failed to get a response from the page")
            content = await page.content()
            return Response(
                url=page.url, text=content, body=content.encode("utf-8"),
                status=final_response.status,
                reason=final_response.status_text or StatusText.get(final_response.status),
                encoding=final_response.headers.get("content-type", "") or "utf-8",
                cookies={cookie["name"]: cookie["value"] for cookie in await self._context.cookies()},
                headers=await first_response.all_headers(),
                request_headers=await first_response.request.all_headers(),
                history=await engine._async_process_response_history(first_response),
                **engine.adaptor_arguments,
            )
        except Exception as exc:
            if not browser.is_connected():
                raise httpx.NetworkError("Browser session disconnected") from exc
            raise
        finally:
            if page is not None:
                await _close_resource(page, browser)
