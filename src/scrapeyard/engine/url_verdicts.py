"""Run the public-URL guard off the event loop, once per hostname while fresh."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from scrapeyard.common.async_tools import shield
from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.host_verdicts import PublicHostVerdicts
from scrapeyard.common.run_threads import run_thread_work
from scrapeyard.engine.url_guard import (
    ResolvedPublicURL,
    UnsafeURLError,
    URLResolutionError,
    public_dns_hostname,
)

# What a shared lookup reports when it ended without an answer: its request
# was cancelled, ran out of budget, or failed in some way of its own.
_ABANDONED = object()
# The lexical checks scan the whole URL and a page chooses its length
# (Chromium allows 2 MB): about 40 ns a character for ASCII, and quadratic in
# a non-ASCII label, which Python's IDNA encoder converts before it checks the
# length. So only short ASCII URLs, which is how a browser spells every request
# it makes, are scanned on the event loop. Any other URL is checked on a run
# thread and resolved every time, as all of them used to be.
_MAX_INLINE_URL_CHARS = 2048
_REFUSALS = (UnsafeURLError, URLResolutionError)


def _run_verdicts(run_budget: RunBudget | None) -> PublicHostVerdicts | None:
    # Anything but a run's own store means "resolve every time".
    verdicts = getattr(run_budget, "public_hosts", None)
    return verdicts if isinstance(verdicts, PublicHostVerdicts) else None


def _scans_inline(url: str) -> bool:
    return len(url) <= _MAX_INLINE_URL_CHARS and url.isascii()


def _verdict_hostname(url: str, scheme_kwargs: dict[str, Any]) -> str | None:
    """Run the lexical checks here and return the hostname a verdict is kept under.

    ``None`` when the URL has no verdict to use: it is an IP literal, or the
    checks refuse it, which the caller's own check then reports.
    """

    try:
        return public_dns_hostname(url, **scheme_kwargs)
    except ValueError:
        return None


async def assert_public_url_in_run(
    check: Callable[..., None],
    url: str,
    *,
    run_budget: RunBudget | None,
    allow_unresolved: bool = True,
    allowed_schemes: tuple[str, ...] | None = None,
) -> None:
    """Apply *check*, the caller's ``assert_public_url``, to *url* within a run.

    The lexical checks run on every call. The lookup is skipped only when this
    run resolved the same hostname to public addresses within the verdict
    lifetime. Otherwise *check* runs on a run thread, and requests that arrive
    for that hostname while it does wait for its answer instead of asking
    again. Only a public answer outlives the lookup: an unresolved or
    non-public one is never remembered, and it drops a verdict the run still
    held for that hostname.

    Without a run budget there is no run to remember anything, and every call
    resolves as before.
    """

    scheme_kwargs: dict[str, Any] = (
        {} if allowed_schemes is None else {"allowed_schemes": allowed_schemes}
    )
    verdicts = _run_verdicts(run_budget)
    scanned = verdicts is not None and _scans_inline(url)
    host = _verdict_hostname(url, scheme_kwargs) if scanned else None
    if run_budget is None or verdicts is None or host is None:
        try:
            await run_thread_work(
                check,
                url,
                run_budget=run_budget,
                allow_unresolved=allow_unresolved,
                **scheme_kwargs,
            )
        except _REFUSALS:
            if verdicts is not None and not scanned:
                # Its hostname was not read here, so no one verdict can be dropped.
                verdicts.clear()
            raise
        return

    # Every check was a deadline checkpoint when each one hopped to a thread.
    run_budget.check_deadline()
    waited = False
    while not verdicts.is_fresh(host):
        shared = verdicts.lookups.get(host)
        if shared is not None and not waited:
            outcome = await run_budget.wait_for(shield(shared))
            waited = True
            if outcome is _ABANDONED:
                continue
            if outcome is None or (isinstance(outcome, URLResolutionError) and allow_unresolved):
                return
            # Each request raises its own instance: callers attach diagnostics.
            raise type(outcome)(*outcome.args)
        # Nobody is resolving it, or the lookup this request waited for was
        # abandoned and another began: that one is not waited for in turn.
        resolve = _resolve_for_waiters if shared is None else _resolve
        try:
            await resolve(check, url, host, verdicts, run_budget, scheme_kwargs)
        except URLResolutionError:
            if not allow_unresolved:
                raise
        return


async def _resolve(
    check: Callable[..., None],
    url: str,
    host: str,
    verdicts: PublicHostVerdicts,
    run_budget: RunBudget,
    scheme_kwargs: dict[str, Any],
) -> None:
    """Look *host* up now: remember a public answer, drop its verdict on any other."""

    started = verdicts.now()
    try:
        # Resolution is always required here, so that "resolved and public"
        # can be told from "did not resolve"; only the former is remembered.
        await run_thread_work(
            check, url, run_budget=run_budget, allow_unresolved=False, **scheme_kwargs
        )
    except _REFUSALS:
        verdicts.forget(host)
        raise
    verdicts.record(host, started)


async def _resolve_for_waiters(
    check: Callable[..., None],
    url: str,
    host: str,
    verdicts: PublicHostVerdicts,
    run_budget: RunBudget,
    scheme_kwargs: dict[str, Any],
) -> None:
    """Look *host* up once for every request of the run that waits on it meanwhile."""

    shared: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    verdicts.lookups[host] = shared
    outcome: Any = _ABANDONED
    try:
        await _resolve(check, url, host, verdicts, run_budget, scheme_kwargs)
        outcome = None
    except _REFUSALS as exc:
        outcome = exc
        raise
    finally:
        del verdicts.lookups[host]
        shared.set_result(outcome)


async def resolve_public_url_in_run(
    resolve: Callable[[str], ResolvedPublicURL],
    url: str,
    *,
    run_budget: RunBudget | None,
) -> ResolvedPublicURL:
    """Apply *resolve*, the caller's ``resolve_public_url``, and remember the answer.

    The connection is still pinned to an address resolved for it alone. A
    success means every address of the hostname was public, which is the
    verdict the guard would reach, so the run's later checks of that hostname
    can use it. A refusal drops the verdict instead.
    """

    verdicts = _run_verdicts(run_budget)
    if verdicts is None:
        return await run_thread_work(resolve, url, run_budget=run_budget)
    scanned = _scans_inline(url)
    host = _verdict_hostname(url, {}) if scanned else None
    started = verdicts.now()
    try:
        resolved = await run_thread_work(resolve, url, run_budget=run_budget)
    except _REFUSALS:
        if host is not None:
            verdicts.forget(host)
        elif not scanned:
            verdicts.clear()
        raise
    if host is not None:
        verdicts.record(host, started)
    return resolved
