"""Supported browser fetcher adapters for current browser automation bindings."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, AsyncContextManager, cast

from camoufox.async_api import AsyncCamoufox
from playwright.async_api import Browser
from scrapling import DynamicFetcher as ScraplingDynamicFetcher
from scrapling import StealthyFetcher as ScraplingStealthyFetcher
from scrapling.engines._browsers._controllers import AsyncDynamicSession
from scrapling.engines._browsers._stealth import AsyncStealthySession
from scrapling.engines.toolbelt.custom import Response

from scrapeyard.engine.scrapling_compat import install_scrapling_compatibility

install_scrapling_compatibility()


class DynamicFetcher:
    """Dispatch Chromium with its native sandbox explicitly enabled.

    Scrapling does not currently expose Playwright's ``chromium_sandbox`` launch
    option.  Constructing its supported session objects before entering them lets
    Scrapeyard turn the sandbox on without changing navigation, proxy, screenshot,
    or selector behavior.
    """

    @classmethod
    async def async_fetch(cls, url: str, **kwargs: Any) -> Response:
        stealth = bool(kwargs.pop("stealth", False))
        if stealth:
            kwargs.pop("nstbrowser_mode", None)
            return await _fetch_chromium(
                url,
                kwargs,
                fetcher=ScraplingStealthyFetcher,
                session_type=AsyncStealthySession,
            )

        for unsupported in (
            "hide_canvas",
            "humanize",
            "os_randomize",
            "geoip",
            "disable_ads",
            "additional_arguments",
            "nstbrowser_mode",
        ):
            kwargs.pop(unsupported, None)
        return await _fetch_chromium(
            url,
            kwargs,
            fetcher=ScraplingDynamicFetcher,
            session_type=AsyncDynamicSession,
        )


async def _fetch_chromium(
    url: str,
    kwargs: dict[str, Any],
    *,
    fetcher: Any,
    session_type: Any,
) -> Response:
    selector_config = kwargs.get("selector_config", {}) or kwargs.pop(
        "custom_config", {}
    )
    if not isinstance(selector_config, dict):
        raise TypeError("Argument `selector_config` must be a dictionary.")
    kwargs["selector_config"] = {
        **fetcher._generate_parser_arguments(),
        **selector_config,
    }

    session = session_type(**kwargs)
    browser_options = getattr(session, "_browser_options", None)
    if not isinstance(browser_options, dict):
        raise RuntimeError("Scrapling did not expose Chromium launch options")
    if not kwargs.get("cdp_url"):
        browser_options["chromium_sandbox"] = True
    async with session as active_session:
        return cast(Response, await active_session.fetch(url))


class CamoufoxFetcher:
    """Camoufox adapter that returns Scrapling's current ``Response`` type."""

    @classmethod
    async def async_fetch(cls, url: str, **kwargs: Any) -> Response:
        timeout = float(kwargs.pop("timeout", 30_000))
        kwargs.pop("disable_resources", None)
        network_idle = bool(kwargs.pop("network_idle", False))
        page_setup = kwargs.pop("page_setup", None)
        page_action = kwargs.pop("page_action", None)
        wait_selector = kwargs.pop("wait_selector", None)
        wait_selector_state = kwargs.pop("wait_selector_state", "attached")
        wait = float(kwargs.pop("wait", 0))
        extra_headers = dict(kwargs.pop("extra_headers", None) or {})
        useragent = kwargs.pop("useragent", None)
        proxy = _proxy_settings(kwargs.pop("proxy", None))
        selector_config = dict(
            kwargs.pop("selector_config", None)
            or kwargs.pop("custom_config", None)
            or {}
        )

        additional = dict(kwargs.pop("additional_arguments", None) or {})
        locale = additional.pop("locale", None)
        launch_options: dict[str, Any] = {
            "headless": kwargs.pop("headless", True),
            "proxy": proxy,
            "humanize": kwargs.pop("humanize", None),
            "geoip": kwargs.pop("geoip", False),
            "block_webrtc": kwargs.pop("block_webrtc", False),
            "allow_webgl": not bool(kwargs.pop("hide_canvas", False)),
            "locale": locale,
            **additional,
        }
        if useragent:
            launch_options["config"] = {"navigator.userAgent": useragent}
            launch_options["i_know_what_im_doing"] = True
        launch_options = {
            name: value for name, value in launch_options.items() if value is not None
        }

        camoufox = cast(
            Callable[..., AsyncContextManager[Browser]],
            AsyncCamoufox,
        )
        async with camoufox(**launch_options) as browser:
            # Camoufox owns viewport/fingerprint emulation at browser launch.
            # Asking current Playwright to apply its default viewport sends the
            # newer ``isMobile`` protocol member, which Camoufox's Firefox
            # protocol intentionally does not implement. A no-viewport context
            # preserves Camoufox's generated window dimensions and avoids
            # coupling this adapter to Playwright's Firefox protocol additions.
            context = await browser.new_context(no_viewport=True)
            try:
                page = await context.new_page()
                page.set_default_navigation_timeout(timeout)
                page.set_default_timeout(timeout)
                if extra_headers:
                    await page.set_extra_http_headers(extra_headers)
                if page_setup is not None:
                    await page_setup(page)

                navigation = await page.goto(url)
                await page.wait_for_load_state("domcontentloaded")
                if network_idle:
                    await page.wait_for_load_state("networkidle")
                if page_action is not None:
                    await page_action(page)
                if wait_selector:
                    await page.locator(wait_selector).first.wait_for(
                        state=wait_selector_state
                    )
                if wait:
                    await page.wait_for_timeout(wait)

                content = await page.content()
                final_url = page.url
                status = navigation.status if navigation is not None else 200
                reason = navigation.status_text if navigation is not None else "OK"
                headers = (
                    await navigation.all_headers() if navigation is not None else {}
                )
                request_headers = (
                    await navigation.request.all_headers()
                    if navigation is not None
                    else extra_headers
                )
                cookies = {
                    str(cookie["name"]): str(cookie["value"])
                    for cookie in await context.cookies()
                }
                return Response(
                    url=final_url,
                    content=content,
                    status=status,
                    reason=reason,
                    cookies=cookies,
                    headers=headers,
                    request_headers=request_headers,
                    **selector_config,
                )
            finally:
                await context.close()


def _proxy_settings(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"server": value}
    if isinstance(value, dict):
        return {str(name): str(item) for name, item in value.items()}
    raise TypeError("Browser proxy must be a URL string or mapping")
