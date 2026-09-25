"""Fresh scripts and stylesheets that shared browsers serve from memory.

Playwright disables the browser's HTTP cache while request routing is on, and
every browser fetch routes all its requests through the URL guard, so each page
downloads the same bundles again. With ``execution.reuse_browser`` a run keeps
this cache instead: a response that a private HTTP cache could reuse is stored
once, and later requests for the same URL from the same site's targets are
fulfilled from memory after the guard has admitted them.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

# Documents, XHR, fetch and everything else always go to the network.
CACHED_RESOURCE_TYPES = frozenset({"script", "stylesheet"})
MAX_CACHE_BYTES = 32 * 1024 * 1024
MAX_ENTRY_BYTES = 8 * 1024 * 1024
# The body is stored decoded and re-framed on fulfill; cookies are never replayed.
_DROPPED_HEADERS = frozenset({
    "connection", "content-encoding", "content-length", "keep-alive", "set-cookie",
    "transfer-encoding",
})


@dataclass(frozen=True)
class CachedAsset:
    status: int
    headers: dict[str, str]
    body: bytes
    expires_at: float


def freshness_seconds(headers: dict[str, str]) -> float | None:
    """Seconds a private cache may reuse a response with lower-cased *headers*.

    Returns None unless the response has an explicit, positive freshness
    lifetime (``max-age``, else ``Expires``), sets no cookie, is not marked
    ``no-store``, ``no-cache`` or ``private``, and varies at most by encoding.
    """
    if "set-cookie" in headers:
        return None
    vary = headers.get("vary", "")
    if vary and any(token.strip().lower() != "accept-encoding" for token in vary.split(",")):
        return None
    lifetime: float | None = None
    for directive in headers.get("cache-control", "").split(","):
        name, _, value = directive.strip().lower().partition("=")
        if name in {"no-store", "no-cache", "private"}:
            return None
        if name == "max-age":
            value = value.strip().strip('"')
            if not value.isdigit():
                return None
            lifetime = float(value)
    if lifetime is None and "expires" in headers:
        try:
            expires = parsedate_to_datetime(headers["expires"])
            date = (
                parsedate_to_datetime(headers["date"])
                if "date" in headers
                else datetime.now(timezone.utc)
            )
            lifetime = (expires - date).total_seconds()
        except (TypeError, ValueError):
            return None
    if lifetime is None:
        return None
    age = headers.get("age", "")
    lifetime -= float(age) if age.isdigit() else 0.0
    return lifetime if lifetime > 0 else None


class AssetCache:
    """Least-recently-used, byte-bounded responses keyed by target site and URL."""

    def __init__(
        self, *, max_bytes: int = MAX_CACHE_BYTES, max_entry_bytes: int = MAX_ENTRY_BYTES,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_entry_bytes = max_entry_bytes
        self._entries: OrderedDict[tuple[str | None, str], CachedAsset] = OrderedDict()
        self._bytes = 0

    @property
    def size(self) -> int:
        return self._bytes

    def lookup(self, site: str | None, request: Any) -> CachedAsset | None:
        """Return a fresh stored response for *request*, if it may use one."""
        if request.method != "GET" or request.resource_type not in CACHED_RESOURCE_TYPES:
            return None
        key = (site, request.url)
        asset = self._entries.get(key)
        if asset is None:
            return None
        if asset.expires_at <= time.monotonic():
            self._remove(key)
            return None
        self._entries.move_to_end(key)
        return asset

    def wants(self, site: str | None, response: Any) -> bool:
        """Cheap check before reading a response's headers and body."""
        request = response.request
        return bool(
            response.status == 200
            and request.method == "GET"
            and request.resource_type in CACHED_RESOURCE_TYPES
            and (site, request.url) not in self._entries
        )

    async def store(self, site: str | None, response: Any) -> None:
        """Keep *response* when a private HTTP cache could reuse it."""
        try:
            headers = await response.all_headers()
            lifetime = freshness_seconds(headers)
            if lifetime is None:
                return
            declared = headers.get("content-length", "")
            if declared.isdigit() and int(declared) > self._max_entry_bytes:
                return
            body = await response.body()
        except Exception:
            # The response failed or its page closed before the body arrived.
            return
        if len(body) > self._max_entry_bytes:
            return
        key = (site, response.request.url)
        self._remove(key)
        self._entries[key] = CachedAsset(
            status=response.status,
            headers={
                name: value for name, value in headers.items() if name not in _DROPPED_HEADERS
            },
            body=body,
            expires_at=time.monotonic() + lifetime,
        )
        self._bytes += len(body)
        while self._bytes > self._max_bytes:
            self._remove(next(iter(self._entries)))

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def _remove(self, key: tuple[str | None, str]) -> None:
        asset = self._entries.pop(key, None)
        if asset is not None:
            self._bytes -= len(asset.body)
