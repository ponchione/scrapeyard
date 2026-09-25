"""Browser sessions a run's same-site targets share (``execution.reuse_browser``)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from scrapeyard.common.async_tools import await_cleanup
from scrapeyard.common.traffic import url_site
from scrapeyard.config.schema import BrowserConfig, TargetConfig
from scrapeyard.engine.asset_cache import AssetCache
from scrapeyard.engine.browser_session import BrowserSession

# Targets with equal keys may share a browser: registrable domain, fetcher,
# proxy URL (with the run's proxy session) and the whole browser block.
BrowserGroupKey = tuple[str | None, str, str | None, str]

# A long-lived Playwright driver's heap grows as it serves requests (V8 enlarges
# its young generation, then its old space), up to 50-80 MB above a fresh driver,
# and cached bodies cross the driver as base64 when fulfilled. Replacing the
# session after this many routed requests keeps a run's peak memory within about
# 5% of one browser per target.
RECYCLE_AFTER_REQUESTS = 250


def browser_group_key(target: TargetConfig, proxy_url: str | None) -> BrowserGroupKey:
    browser = target.browser or BrowserConfig()
    return (url_site(target.url), target.fetcher.value, proxy_url, browser.model_dump_json())


class BrowserPool:
    """Lend each browser target an idle session of its group, or open a new one.

    A session serves one target at a time, so its routes, budgets and page
    diagnostics stay per target, and each fetch still opens a fresh page. With
    ``concurrency`` above one, a group holds up to that many sessions. The pool
    keeps at most *capacity* sessions: before adding one, it closes an idle
    session of another group. A session that has routed
    :data:`RECYCLE_AFTER_REQUESTS` requests closes when its target finishes.
    All sessions share the run's :class:`AssetCache`, which outlives them.
    """

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        # One byte-bounded cache for the run, partitioned by target site.
        self._assets = AssetCache()
        self._groups: dict[BrowserSession, BrowserGroupKey] = {}
        # Idle sessions in release order; the dict keeps insertion order.
        self._idle: dict[BrowserSession, None] = {}

    @property
    def sessions(self) -> int:
        return len(self._groups)

    async def acquire(
        self, target: TargetConfig, proxy_url: str | None, fetcher_cls: Any,
    ) -> BrowserSession:
        group = browser_group_key(target, proxy_url)
        session = next((idle for idle in self._idle if self._groups[idle] == group), None)
        if session is not None:
            del self._idle[session]
            if session.disconnected:
                # The next fetch relaunches instead of failing its first attempt.
                await await_cleanup(session.aclose())
            return session
        evicted = None
        if len(self._groups) >= self._capacity and self._idle:
            evicted = next(iter(self._idle))
            del self._idle[evicted]
            del self._groups[evicted]
        session = BrowserSession(fetcher_cls, assets=self._assets)
        self._groups[session] = group
        if evicted is not None:
            try:
                await await_cleanup(evicted.aclose())
            except BaseException:
                # Never opened; keep it for the group instead of leaking a slot.
                self._idle[session] = None
                raise
        return session

    async def release(self, session: BrowserSession) -> None:
        if session not in self._groups:
            return
        if session.routed_requests < RECYCLE_AFTER_REQUESTS:
            self._idle[session] = None
            return
        del self._groups[session]
        await await_cleanup(session.aclose())

    async def aclose(self) -> None:
        sessions = list(self._groups)
        self._groups.clear()
        self._idle.clear()
        self._assets.clear()
        # One shielded wait: cancellation cannot leave later browsers open.
        outcomes = await await_cleanup(asyncio.gather(
            *(session.aclose() for session in sessions), return_exceptions=True,
        ))
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome


_ACTIVE_BROWSER_POOL: ContextVar[BrowserPool | None] = ContextVar(
    "scrapeyard_active_browser_pool",
    default=None,
)


@contextmanager
def activate_browser_pool(pool: BrowserPool | None) -> Iterator[None]:
    token = _ACTIVE_BROWSER_POOL.set(pool)
    try:
        yield
    finally:
        _ACTIVE_BROWSER_POOL.reset(token)


def current_browser_pool() -> BrowserPool | None:
    return _ACTIVE_BROWSER_POOL.get()
