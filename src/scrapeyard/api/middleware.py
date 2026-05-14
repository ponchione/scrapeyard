"""ASGI middlewares: API key auth and request body size cap."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import secrets
import time
from collections import deque
from collections.abc import Callable, Iterable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class _RequestBodyTooLarge(Exception):
    """Internal signal raised when a streaming request exceeds the body cap."""


class RateLimitMiddleware:
    """In-memory sliding-window HTTP request limiter.

    Requests are counted globally per validated API key when a known key is
    presented, otherwise per immediate client IP address. This intentionally
    does not trust forwarding headers; deployments behind a proxy should make
    the proxy enforce its own edge limits or pass authenticated API keys.
    """

    def __init__(
        self,
        app: ASGIApp,
        requests: int,
        window_seconds: float,
        api_keys: set[str] | None = None,
        exempt_paths: Iterable[str] = (),
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.app = app
        self.request_limit = requests
        self.window_seconds = window_seconds
        self.api_keys = set(api_keys or ())
        self.exempt_paths = set(exempt_paths)
        self.clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._requests_by_key: dict[str, deque[float]] = {}
        self._last_prune_at = 0.0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._disabled() or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        key = self._key_for_scope(scope)
        retry_after = await self._record_or_reject(key, self.clock())
        if retry_after is not None:
            await _reject(
                scope,
                send,
                429,
                "Rate limit exceeded",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
            return

        await self.app(scope, receive, send)

    def _disabled(self) -> bool:
        return self.request_limit <= 0 or self.window_seconds <= 0

    def _key_for_scope(self, scope: Scope) -> str:
        try:
            provided = _header_value(scope.get("headers", []), b"x-api-key")
        except ValueError:
            provided = None
        if provided is not None:
            api_key = provided.decode("latin-1")
            if _api_key_is_valid(api_key, self.api_keys):
                digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
                return f"api:{digest}"

        client = scope.get("client")
        host = client[0] if client else "unknown"
        return f"ip:{host}"

    async def _record_or_reject(self, key: str, now: float) -> float | None:
        async with self._lock:
            cutoff = now - self.window_seconds
            self._prune_expired_keys(cutoff, now)
            requests = self._requests_by_key.setdefault(key, deque())
            while requests and requests[0] <= cutoff:
                requests.popleft()

            if len(requests) >= self.request_limit:
                return max(0.0, requests[0] + self.window_seconds - now)

            requests.append(now)
            return None

    def _prune_expired_keys(self, cutoff: float, now: float) -> None:
        if now < self._last_prune_at + self.window_seconds:
            return
        self._last_prune_at = now
        for key, requests in list(self._requests_by_key.items()):
            if not requests or requests[-1] <= cutoff:
                self._requests_by_key.pop(key, None)


class RequestSizeLimitMiddleware:
    """Reject requests whose body exceeds *max_bytes*.

    Enforced via Content-Length header when present, and via a receive-wrapper
    byte counter for chunked transfers.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_lengths = _header_values(scope.get("headers", []), b"content-length")
        if len(content_lengths) > 1:
            await _reject(scope, send, 400, "Invalid Content-Length")
            return
        if content_lengths:
            content_length = content_lengths[0]
            try:
                declared = int(content_length)
            except ValueError:
                await _reject(scope, send, 400, "Invalid Content-Length")
                return
            if declared < 0:
                await _reject(scope, send, 400, "Invalid Content-Length")
                return
            if declared > self.max_bytes:
                await _reject(scope, send, 413, "Request body too large")
                return

        consumed = 0

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        response_started = False

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _RequestBodyTooLarge:
            if not response_started:
                await _reject(scope, send, 413, "Request body too large")


class APIKeyAuthMiddleware:
    """Require a valid ``X-API-Key`` header on every non-exempt request.

    If *keys* is empty, the middleware is a no-op (useful for local dev) and
    logs a single warning on the first request to make the state obvious.
    """

    def __init__(
        self,
        app: ASGIApp,
        keys: set[str],
        exempt_paths: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.keys = set(keys)
        self.exempt_paths = set(exempt_paths)
        self._warned_open = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self.keys:
            if not self._warned_open:
                self._warned_open = True
                logger.warning(
                    "API key auth is disabled (SCRAPEYARD_API_KEYS is empty); "
                    "all endpoints are unauthenticated"
                )
            await self.app(scope, receive, send)
            return

        if scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        try:
            provided = _header_value(scope.get("headers", []), b"x-api-key")
        except ValueError:
            await _reject(scope, send, 400, "Invalid X-API-Key")
            return
        if provided is None or not _api_key_is_valid(provided.decode("latin-1"), self.keys):
            await _reject(scope, send, 401, "Missing or invalid API key")
            return

        await self.app(scope, receive, send)


def _header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    values = _header_values(headers, name)
    if len(values) > 1:
        raise ValueError("duplicate header")
    return values[0] if values else None


def _header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    lowered = name.lower()
    return [value for key, value in headers if key.lower() == lowered]


def _api_key_is_valid(provided: str, keys: set[str]) -> bool:
    valid = False
    for key in keys:
        try:
            valid |= secrets.compare_digest(provided, key)
        except TypeError:
            continue
    return valid


async def _reject(
    scope: Scope,
    send: Send,
    status_code: int,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    response = JSONResponse(status_code=status_code, content={"error": message}, headers=headers)
    await response(scope, _noop_receive, send)


async def _noop_receive() -> Message:
    return {"type": "http.disconnect"}
