"""One target's browser lifetime, using the pinned Scrapling engine settings."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from contextvars import copy_context
from typing import Any, cast

import httpx
from camoufox.async_api import AsyncCamoufox
from scrapling import PlayWrightFetcher
from scrapling.engines.camo import CamoufoxEngine
from scrapling.engines.pw import PlaywrightEngine
from scrapling.engines.toolbelt.custom import Response, StatusText

from scrapeyard.common.async_tools import await_cleanup
from scrapeyard.common.budgets import BudgetExceeded, RunBudget
from scrapeyard.common.traffic import url_site
from scrapeyard.engine.basic_fetch import generate_convincing_referer
from scrapeyard.engine.scrape_models import (
    CLICK_PAGINATION_ATTRIBUTE,
    ClickPaginationResult,
    ClickPaginationSpec,
    ScrapeStop,
)
from scrapeyard.engine.domain_guard import admit_page
from scrapeyard.engine.url_guard import UnsafeURLError

logger = logging.getLogger(__name__)

# In-page check interval while waiting for a clicked page to render different items.
_CLICK_CHANGE_POLL_MS = 50
# Pause before re-arming the wait when a click navigation replaced the document.
_CLICK_NAVIGATION_RETRY_MS = 250
# Bound the actionability wait before falling back to dispatching the click.
_CLICK_ACTIONABLE_TIMEOUT_MS = 10_000

# Hash the current page items (or the body when no item selector is set) in the
# page, so change detection never transfers the rendered document to Python.
_ITEM_FINGERPRINT_SCRIPT = """([query, type]) => {
    let nodes = [];
    try {
        if (!query) {
            nodes = document.body ? [document.body] : [];
        } else if (type === 'xpath') {
            const found = document.evaluate(
                query, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            for (let i = 0; i < found.snapshotLength; i++) nodes.push(found.snapshotItem(i));
        } else {
            nodes = Array.from(document.querySelectorAll(query));
        }
    } catch (error) {
        nodes = document.body ? [document.body] : [];
    }
    let hash = 2166136261 >>> 0;
    for (const node of nodes) {
        const href = node.getAttribute ? (node.getAttribute('href') || '') : '';
        const text = (node.textContent || '') + '\\u0001' + href + '\\u0002';
        for (let i = 0; i < text.length; i++) {
            hash ^= text.charCodeAt(i);
            hash = Math.imul(hash, 16777619) >>> 0;
        }
    }
    return {count: nodes.length, hash: hash.toString(16)};
}"""

# Resolve with the new fingerprint once it differs from the previous one.
_ITEMS_CHANGED_SCRIPT = f"""([query, type, count, hash]) => {{
    const current = ({_ITEM_FINGERPRINT_SCRIPT})([query, type]);
    return current.count !== count || current.hash !== hash ? current : null;
}}"""

# Both pinned Chromium builds stop after 20 redirects. Pin Firefox to that same
# ceiling; reserve the entire chain before releasing an intercepted request.
_MAX_REDIRECTS = 20


# Bound the wait for byte counts of already-finished requests before closing.
_SIZES_SETTLE_SECONDS = 0.5


class _BudgetedRoute:
    """Apply the run budget at the existing guard's actual continue boundary."""

    def __init__(
        self,
        route: Any,
        reserve: Callable[[Any], Awaitable[None]],
        on_abort: Callable[[Any], None],
    ) -> None:
        self._route = route
        self._reserve = reserve
        self._on_abort = on_abort
        self.request = route.request

    async def abort(self, *args: Any, **kwargs: Any) -> None:
        self._on_abort(self.request)
        await self._route.abort(*args, **kwargs)

    async def continue_(self, **kwargs: Any) -> None:
        await self._reserve(self.request)
        await self._route.continue_(**kwargs)


# Responses that carry no body even when they declare a Content-Length.
_BODYLESS_STATUSES = frozenset({204, 304})


def _declared_response_bytes(response: Any) -> int | None:
    """Estimate received bytes from a response's headers, or None without a length.

    The header block is estimated from its lines; the body is the declared,
    encoded Content-Length. Reading both from the response event needs no
    driver round trip, unlike ``request.sizes()`` (about 0.4 ms each).
    """
    headers = response.headers
    # Status line and blank line, then one "name: value" line per header.
    header_bytes = 19 + sum(len(name) + len(value) + 4 for name, value in headers.items())
    if response.status in _BODYLESS_STATUSES or response.request.method == "HEAD":
        return header_bytes
    length = headers.get("content-length", "")
    return header_bytes + int(length) if length.isdigit() else None


async def _received_bytes(request: Any) -> int:
    """Response header and encoded body bytes the browser reports for *request*."""
    sizes = await request.sizes()
    return sum(max(0, int(sizes.get(name) or 0)) for name in (
        "responseHeadersSize", "responseBodySize",
    ))


async def _close_resource(resource: Any, browser: Any) -> None:
    try:
        await resource.close()
    except Exception:
        # Rebrowser raises on context.close() after a browser disconnect.
        # The remaining stack must still stop the driver before a retry.
        if browser.is_connected():
            raise


async def _close_page(page: Any, browser: Any) -> None:
    try:
        # Native keepalive can escape offline routing during pagehide. Deny new
        # document requests before firing unload handlers, including in frames.
        if browser.is_connected() and not page.is_closed():
            for frame in page.frames:
                if not frame.is_detached():
                    await asyncio.wait_for(frame.evaluate("""() => {
                        if (!document.head) return;
                        const meta = document.createElement('meta');
                        meta.httpEquiv = 'Content-Security-Policy';
                        meta.content = "default-src 'none'";
                        document.head.prepend(meta);
                    }"""), timeout=1)
    finally:
        await _close_resource(page, browser)


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
            firefox_options = engine._get_camoufox_options()
            firefox_options["firefox_user_prefs"] = {
                **firefox_options.get("firefox_user_prefs", {}),
                "network.http.redirection-limit": _MAX_REDIRECTS,
                "network.websocket.max-connections": 0,
                "dom.fetchKeepalive.enabled": False,
            }
            manager = cast(Any, AsyncCamoufox)(**firefox_options)
            self._stack.push_async_exit(manager)
            browser = await manager.__aenter__()
            options = {}
        context = await browser.new_context(**{**options, "service_workers": "block"})
        self._stack.push_async_callback(_close_resource, context, browser)
        # No scrape requires a live socket. Omitting connect_to_server prevents
        # the handshake as well as messages, including sockets opened in frames.
        await context.route_web_socket("**/*", lambda socket: socket.close())
        # Camoufox evaluates init scripts in an isolated realm. CSP changes the
        # shared document's native policy, so page/worker code cannot bypass it
        # by using the main realm's original constructors. Firefox's script event
        # covers parser execution before the document-start observer runs.
        await context.add_init_script("""
            (() => {
                const install = () => {
                    if (!document.head) return false;
                    const meta = document.createElement('meta');
                    meta.httpEquiv = 'Content-Security-Policy';
                    meta.content = "worker-src 'none'; connect-src http: https:";
                    document.head.prepend(meta);
                    return true;
                };
                if (!install()) {
                    const observer = new MutationObserver(() => {
                        if (install()) observer.disconnect();
                    });
                    observer.observe(document, {childList: true, subtree: true});
                    document.addEventListener('beforescriptexecute', () => {
                        if (install()) observer.disconnect();
                    }, {once: true, capture: true});
                }
            })();
        """)
        self._context = context

    @staticmethod
    async def _capture_html(page: Any, budget: RunBudget | None) -> str:
        if budget is None:
            return cast(str, await page.content())
        # Bound the browser-to-Python HTML message before constructing the
        # parser. This counts rendered UTF-8 HTML, not wire bytes; browsers
        # have already received and rendered the document.
        captured = await page.evaluate("""limit => {
            let html = document.doctype
                ? new XMLSerializer().serializeToString(document.doctype) + '\\n' : '';
            if (document.documentElement) html += document.documentElement.outerHTML;
            const bytes = new TextEncoder().encode(html).byteLength;
            return {bytes, html: bytes <= limit ? html : null};
        }""", budget.remaining_fetched_bytes)
        content = captured["html"]
        await budget.consume_fetched_bytes(
            len(content.encode("utf-8")) if content is not None else captured["bytes"],
        )
        assert content is not None
        return cast(str, content)

    async def _snapshot_response(
        self,
        page: Any,
        context: Any,
        engine: Any,
        first_response: Any,
        final_response: Any,
        budget: RunBudget | None,
        *,
        history: list[Any],
    ) -> Response:
        content = await self._capture_html(page, budget)
        return Response(
            url=page.url, text=content, body=content.encode("utf-8"),
            status=final_response.status,
            reason=final_response.status_text or StatusText.get(final_response.status),
            encoding=final_response.headers.get("content-type", "") or "utf-8",
            cookies={cookie["name"]: cookie["value"] for cookie in await context.cookies()},
            headers=await first_response.all_headers(),
            request_headers=await first_response.request.all_headers(),
            history=history,
            **engine.adaptor_arguments,
        )

    @staticmethod
    def _locator_query(query: str, selector_type: str) -> str:
        return f"xpath={query}" if selector_type == "xpath" else query

    async def _next_is_available(self, page: Any, spec: ClickPaginationSpec) -> bool:
        candidates = page.locator(self._locator_query(spec.next_query, spec.next_type))
        if await candidates.count() == 0:
            return False
        control = candidates.first
        if not await control.is_visible() or not await control.is_enabled():
            return False
        return bool(await control.get_attribute("aria-disabled") != "true")

    @staticmethod
    async def _item_fingerprint(page: Any, spec: ClickPaginationSpec) -> tuple[int, str]:
        value = await page.evaluate(
            _ITEM_FINGERPRINT_SCRIPT, [spec.item_query, spec.item_type or "css"],
        )
        return int(value["count"]), str(value["hash"])

    async def _wait_for_changed_items(
        self,
        page: Any,
        spec: ClickPaginationSpec,
        previous: tuple[int, str],
        timeout_ms: float,
        budget: RunBudget | None,
    ) -> tuple[int, str] | None:
        """Return the new item fingerprint, or None when the page never changed."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout_ms, _CLICK_CHANGE_POLL_MS) / 1000
        while True:
            if budget is not None:
                budget.check_deadline()
            wait_ms = (deadline - loop.time()) * 1000
            if budget is not None:
                wait_ms = min(wait_ms, budget.remaining_seconds * 1000)
            try:
                changed = await page.wait_for_function(
                    _ITEMS_CHANGED_SCRIPT,
                    arg=[spec.item_query, spec.item_type or "css", *previous],
                    polling=_CLICK_CHANGE_POLL_MS,
                    timeout=max(wait_ms, 1),
                )
                value = await changed.json_value()
                await changed.dispose()
                return int(value["count"]), str(value["hash"])
            except Exception as exc:
                # Stealth Chromium ships its own Playwright build, so match by name.
                if type(exc).__name__ == "TimeoutError":
                    if budget is not None:
                        budget.check_deadline()
                    return None
                # A click can navigate; the old execution context disappears
                # until the next document is ready.
                if loop.time() >= deadline:
                    return None
                await page.wait_for_timeout(_CLICK_NAVIGATION_RETRY_MS)

    @staticmethod
    async def _activate_control(control: Any, timeout_ms: float) -> None:
        """Click like a user; if an overlay intercepts it, dispatch the click on the control."""
        try:
            await control.click(timeout=min(timeout_ms, _CLICK_ACTIONABLE_TIMEOUT_MS))
        except Exception as exc:
            # Stealth Chromium ships its own Playwright build, so match by name.
            if type(exc).__name__ != "TimeoutError":
                raise
            # Sticky banners and signup overlays commonly cover footer pagination.
            # Client-side routers handle the element's click event either way.
            await control.dispatch_event("click")

    async def _click_through_pages(
        self,
        page: Any,
        context: Any,
        engine: Any,
        first_response: Any,
        final_response: Any,
        budget: RunBudget | None,
        spec: ClickPaginationSpec,
    ) -> ClickPaginationResult:
        pages: list[object] = []
        previous = await self._item_fingerprint(page, spec)
        seen = {previous}
        timeout_ms = float(engine.timeout)
        for _ in range(spec.max_pages - 1):
            if budget is not None:
                budget.check_deadline()
            if not await self._next_is_available(page, spec):
                return ClickPaginationResult(pages=pages, stop_reason="exhausted")
            try:
                # Each clicked page is a top-level page for the domain guard.
                await admit_page(page.url)
            except ScrapeStop as stop:
                return ClickPaginationResult(
                    pages=pages, stop_reason=stop.pagination_stop_reason, stop_detail=str(stop),
                )
            try:
                control = page.locator(self._locator_query(spec.next_query, spec.next_type)).first
                await self._activate_control(control, timeout_ms)
                changed = await self._wait_for_changed_items(
                    page, spec, previous, timeout_ms, budget,
                )
                if changed is None:
                    return ClickPaginationResult(pages=pages, stop_reason="repeated_page")
                with suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded")
                if engine.wait_selector:
                    with suppress(Exception):
                        await page.locator(engine.wait_selector).first.wait_for(
                            state=engine.wait_selector_state,
                        )
                await page.wait_for_timeout(engine.wait)
                current = await self._item_fingerprint(page, spec)
            except (BudgetExceeded, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.info("Click pagination stopped: %s", type(exc).__name__)
                return ClickPaginationResult(pages=pages, stop_reason="unknown")
            if spec.item_query is not None and current[0] == 0:
                return ClickPaginationResult(pages=pages, stop_reason="exhausted")
            if current in seen:
                return ClickPaginationResult(pages=pages, stop_reason="repeated_page")
            seen.add(current)
            previous = current
            pages.append(
                await self._snapshot_response(
                    page, context, engine, first_response, final_response, budget, history=[],
                )
            )
        stop_reason = "max_pages" if await self._next_is_available(page, spec) else "exhausted"
        return ClickPaginationResult(pages=pages, stop_reason=stop_reason)

    async def fetch(
        self,
        url: str,
        call_kwargs: dict[str, Any],
        route_handler: Callable[[Any], Awaitable[None]],
        *,
        budget: RunBudget | None = None,
        site: str | None = None,
    ) -> Any:
        kwargs = dict(call_kwargs)
        custom_config = kwargs.pop("custom_config", None) or {}
        click_spec: ClickPaginationSpec | None = kwargs.pop("click_pagination", None)
        engine_cls = PlaywrightEngine if self.fetcher_cls is PlayWrightFetcher else CamoufoxEngine
        engine: Any = engine_cls(
            **kwargs,
            adaptor_arguments={**self.fetcher_cls._generate_parser_arguments(), **custom_config},
        )
        if self._context is not None and not self._context.browser.is_connected():
            raise httpx.NetworkError("Browser session disconnected")
        if self._context is None:
            await self._open(engine)

        context = self._context
        # Playwright's dispatcher survives page fetches and inherits the context
        # of its creation. Bind each route to THIS page's budget/diagnostics.
        route_context = copy_context()
        chains: dict[Any, int] = {}
        route_error: BaseException | None = None
        traffic = budget.traffic if budget is not None else None
        if site is None:
            site = url_site(url)
        sizing: set[asyncio.Future[int]] = set()
        unsized: set[Any] = set()

        async def reserve(request: Any) -> None:
            if budget is not None:
                await budget.reserve_requests(_MAX_REDIRECTS + 1)
                chains[request] = 1
                budget.traffic.request(request.url, site)

        def observe_abort(request: Any) -> None:
            if traffic is not None:
                traffic.blocked(request.url, site)

        def observe_request(request: Any) -> None:
            if request.redirected_from is None:
                return
            root = request.redirected_from
            while root.redirected_from is not None:
                root = root.redirected_from
            if root in chains:
                chains[root] += 1
                if traffic is not None:
                    traffic.request(request.url, site)

        def record_received(request: Any, sized: asyncio.Future[int]) -> None:
            sizing.discard(sized)
            if traffic is not None and not sized.cancelled() and sized.exception() is None:
                traffic.received(request.url, site, sized.result())

        def observe_response(response: Any) -> None:
            if traffic is None:
                return
            declared = _declared_response_bytes(response)
            if declared is not None:
                traffic.received(response.url, site, declared)
            else:
                # Chunked bodies declare no length; ask the browser once they finish.
                unsized.add(response.request)

        def observe_finished(request: Any) -> None:
            if request not in unsized:
                return
            unsized.discard(request)
            sized = asyncio.ensure_future(_received_bytes(request))
            sizing.add(sized)
            sized.add_done_callback(lambda done: record_received(request, done))

        async def guard(route: Any) -> None:
            nonlocal route_error
            try:
                wrapped = (
                    _BudgetedRoute(route, reserve, observe_abort) if budget is not None else route
                )
                await route_context.run(asyncio.ensure_future, route_handler(wrapped))
            except UnsafeURLError:
                # The existing guard has aborted and recorded this subrequest.
                # Preserve its contract: safe content can still be inspected.
                return
            except BaseException as exc:
                route_error = exc
                # Closing a page can release a browser-native request whose
                # frame disappeared before interception. Disable networking
                # before aborting/closing, including unload beacons and icons.
                await context.set_offline(True)
                with suppress(Exception):
                    await route.abort()
                if page is not None:
                    await _close_page(page, browser)

        browser = context.browser
        page = None
        fetch_error: BaseException | None = None
        try:
            context.on("request", observe_request)
            context.on("response", observe_response)
            context.on("requestfinished", observe_finished)
            await context.route("**/*", guard)
            await context.set_offline(False)
            page = await context.new_page()
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
            if route_error is not None:
                raise route_error
            final_response = final_response or first_response
            if final_response is None:
                raise ValueError("Failed to get a response from the page")
            response = await self._snapshot_response(
                page, context, engine, first_response, final_response, budget,
                history=await engine._async_process_response_history(first_response),
            )
            if click_spec is not None:
                click_result = await self._click_through_pages(
                    page, context, engine, first_response, final_response, budget, click_spec,
                )
                if route_error is not None:
                    raise route_error
                setattr(response, CLICK_PAGINATION_ATTRIBUTE, click_result)
            if sizing:
                await asyncio.wait(set(sizing), timeout=_SIZES_SETTLE_SECONDS)
            return response
        except BaseException as exc:
            fetch_error = exc
            if isinstance(exc, (BudgetExceeded, asyncio.CancelledError)):
                raise
            if route_error is not None:
                raise route_error from exc
            if not browser.is_connected():
                raise httpx.NetworkError("Browser session disconnected") from exc
            raise
        finally:
            stopped = False
            try:
                if browser.is_connected():
                    await context.set_offline(True)
                # Popups belong to this context and must not outlive a page fetch.
                for owned_page in list(context.pages):
                    await _close_page(owned_page, browser)
                await context.unroute("**/*", guard)
                context.remove_listener("request", observe_request)
                context.remove_listener("response", observe_response)
                context.remove_listener("requestfinished", observe_finished)
                stopped = True
            except BaseException:
                # A crashed page/context can reject cleanup RPCs while its driver
                # is still connected. Stop the whole session before releasing
                # reservations, and preserve the original fetch/budget failure.
                await await_cleanup(self.aclose())
                stopped = True
                if fetch_error is None:
                    raise
            finally:
                for sized in list(sizing):
                    sized.cancel()
                if budget is not None and stopped:
                    await budget.settle_requests((_MAX_REDIRECTS + 1) * len(chains), sum(chains.values()))
