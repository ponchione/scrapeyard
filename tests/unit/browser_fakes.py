"""A browser session that runs a test script instead of launching a browser."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from scrapling import PlayWrightFetcher

from scrapeyard.common.budgets import RunBudget
from scrapeyard.engine.browser_session import BrowserSession

BrowserScript = Callable[[str, dict[str, Any], Callable[[Any], Awaitable[None]]], Awaitable[Any]]


class ScriptedBrowserSession(BrowserSession):
    """Replace the browser behind ``BrowserSession.fetch`` with *script*.

    The script receives what the real fetch receives: the URL, the call
    kwargs (including ``page_action``) and the route handler the scraper
    installs for every request the browser would make.
    """

    def __init__(self, script: BrowserScript, fetcher_cls: Any = PlayWrightFetcher) -> None:
        super().__init__(fetcher_cls)
        self._script = script

    async def fetch(
        self,
        url: str,
        call_kwargs: dict[str, Any],
        route_handler: Callable[[Any], Awaitable[None]],
        *,
        budget: RunBudget | None = None,
        site: str | None = None,
    ) -> Any:
        return await self._script(url, call_kwargs, route_handler)
