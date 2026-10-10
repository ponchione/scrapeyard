"""Hostnames one run recently saw resolve to public addresses only."""

from __future__ import annotations

import asyncio
import time
from typing import Any

# How long one public answer stands in for another lookup of the same name.
# The browser, proxy or remote endpoint resolves the name again by itself
# whatever the guard saw, so a longer life trades no protection for speed; it
# is kept short so a name that moves is checked again within the same run.
PUBLIC_HOST_VERDICT_TTL_SECONDS = 30.0
# A page can name hosts without limit; the oldest verdict makes room.
MAX_PUBLIC_HOST_VERDICTS = 1024


class PublicHostVerdicts:
    """Short-lived memory of hostnames whose every address was public.

    Only that answer is kept. A name that did not resolve, or that resolved to
    a non-public address, loses any verdict it had and is looked up again on
    its next request. Used from the run's event loop only.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PUBLIC_HOST_VERDICT_TTL_SECONDS,
        max_hosts: int = MAX_PUBLIC_HOST_VERDICTS,
        clock: Any = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_hosts = max_hosts
        self._clock = clock
        self._expires: dict[str, float] = {}
        # Names being resolved now, each with the future its other requests
        # wait on instead of starting a lookup of their own.
        self.lookups: dict[str, asyncio.Future[Any]] = {}

    def now(self) -> float:
        return float(self._clock())

    def is_fresh(self, host: str) -> bool:
        expires = self._expires.get(host)
        if expires is None:
            return False
        if self.now() < expires:
            return True
        del self._expires[host]
        return False

    def record(self, host: str, resolved_at: float) -> None:
        """Trust *host* for the verdict lifetime, counted from when its lookup began."""
        self._expires.pop(host, None)
        if len(self._expires) >= self._max_hosts:
            del self._expires[next(iter(self._expires))]
        self._expires[host] = resolved_at + self._ttl_seconds

    def forget(self, host: str) -> None:
        """Drop *host*'s verdict: a lookup has just answered otherwise."""
        self._expires.pop(host, None)

    def clear(self) -> None:
        self._expires.clear()
