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

from scrapeyard.api.response_utils import error_content
from scrapeyard.api.auth import (
    APICredential,
    AuthenticatedCaller,
    LOCAL_DEVELOPMENT_CALLER,
    PUBLIC_CALLER,
    authenticate_api_key,
    parse_api_credentials,
)
from scrapeyard.runtime.metrics import observe_api_request, observe_rate_limit_state

logger = logging.getLogger(__name__)


class MetricsMiddleware:
    """Record bounded HTTP metrics using route templates, never raw paths."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def measured_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, measured_send)
        finally:
            route = scope.get("route")
            route_label = getattr(route, "path", None)
            if not isinstance(route_label, str):
                path = scope.get("path")
                route_label = (
                    path
                    if path in {"/health", "/health/live", "/health/ready", "/metrics"}
                    else "unmatched"
                )
            observe_api_request(
                str(scope.get("method", "OTHER")),
                route_label,
                status_code,
                time.perf_counter() - started,
            )


class _RequestBodyTooLarge(Exception):
    """Internal signal raised when a streaming request exceeds the body cap."""


class APIVersionHeaderMiddleware:
    """Advertise the stable unversioned-path contract on every HTTP response."""

    def __init__(self, app: ASGIApp, version: str = "1") -> None:
        self.app = app
        self.version = version.encode("ascii")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def versioned_send(message: Message) -> None:
            if scope["type"] == "http" and message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-scrapeyard-api-version"
                ]
                headers.append((b"x-scrapeyard-api-version", self.version))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, versioned_send)


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
        max_keys: int = 10000,
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self.app = app
        self.request_limit = requests
        self.window_seconds = window_seconds
        self.api_keys = set(api_keys or ())
        self.exempt_paths = set(exempt_paths)
        self.clock = clock or time.monotonic
        self.max_keys = max_keys
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
            requests = self._requests_by_key.get(key)
            if requests is None:
                if len(self._requests_by_key) >= self.max_keys:
                    observe_rate_limit_state("api", "saturated")
                    earliest_expiry = min(
                        bucket[-1] + self.window_seconds
                        for bucket in self._requests_by_key.values()
                        if bucket
                    )
                    return max(0.0, earliest_expiry - now)
                requests = deque()
                self._requests_by_key[key] = requests
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
                observe_rate_limit_state("api", "expired")


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
                declared = _parse_content_length(content_length)
            except ValueError:
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

    An empty credential set fails closed unless the explicit local-development
    opt-in is supplied by the application.
    """

    def __init__(
        self,
        app: ASGIApp,
        keys: set[str] | None = None,
        credentials: tuple[APICredential, ...] | None = None,
        exempt_paths: Iterable[str] = (),
        local_development_unauthenticated: bool = False,
    ) -> None:
        self.app = app
        self.credentials = (
            parse_api_credentials("", legacy_keys=set(keys or ()))
            if credentials is None
            else tuple(credentials)
        )
        self.exempt_paths = set(exempt_paths)
        self.local_development_unauthenticated = local_development_unauthenticated
        self._warned_open = False
        self.failed_auth_attempts = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not self.credentials and self.local_development_unauthenticated:
            if not self._warned_open:
                self._warned_open = True
                logger.warning(
                    "API authentication is disabled by the explicit local-development "
                    "opt-in; local-development caller has all scopes"
                )
            _set_caller(scope, LOCAL_DEVELOPMENT_CALLER)
            await self._call_with_audit(scope, receive, send, LOCAL_DEVELOPMENT_CALLER)
            return

        if not self.credentials and scope.get("path") not in self.exempt_paths:
            self._record_auth_failure(scope, reason="unconfigured")
            await _reject(scope, send, 503, "API authentication is not configured")
            return

        if scope.get("path") in self.exempt_paths:
            _set_caller(scope, PUBLIC_CALLER)
            await self._call_with_audit(scope, receive, send, PUBLIC_CALLER)
            return

        try:
            provided = _header_value(scope.get("headers", []), b"x-api-key")
        except ValueError:
            self._record_auth_failure(scope, reason="duplicate_header")
            await _reject(scope, send, 400, "Invalid X-API-Key")
            return
        caller = (
            None
            if provided is None
            else authenticate_api_key(
                provided.decode("latin-1"),
                self.credentials,
            )
        )
        if caller is None:
            self._record_auth_failure(
                scope,
                reason="missing" if provided is None else "invalid",
            )
            await _reject(scope, send, 401, "Missing or invalid API key")
            return

        _set_caller(scope, caller)
        await self._call_with_audit(scope, receive, send, caller)

    def _record_auth_failure(self, scope: Scope, *, reason: str) -> None:
        self.failed_auth_attempts += 1
        logger.warning(
            "API authentication failed reason=%s method=%s path=%s "
            "failure_count=%s",
            reason,
            scope.get("method", "unknown"),
            scope.get("path", "unknown"),
            self.failed_auth_attempts,
        )

    async def _call_with_audit(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        caller: AuthenticatedCaller,
    ) -> None:
        status_code = 500

        async def audit_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, audit_send)
        finally:
            logger.info(
                "API request caller_id=%s credential_name=%s method=%s path=%s "
                "status_code=%s",
                caller.identity,
                caller.credential_name or "none",
                scope.get("method", "unknown"),
                scope.get("path", "unknown"),
                status_code,
            )


def _set_caller(scope: Scope, caller: AuthenticatedCaller) -> None:
    state = scope.setdefault("state", {})
    state["caller"] = caller
    state["caller_identity"] = caller.identity


def _header_value(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    values = _header_values(headers, name)
    if len(values) > 1:
        raise ValueError("duplicate header")
    return values[0] if values else None


def _header_values(headers: Iterable[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    lowered = name.lower()
    return [value for key, value in headers if key.lower() == lowered]


def _parse_content_length(value: bytes) -> int:
    if not value or not value.isdigit():
        raise ValueError("invalid content-length")
    return int(value)


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
    response = JSONResponse(
        status_code=status_code,
        content=error_content(status_code, message),
        headers=headers,
    )
    await response(scope, _noop_receive, send)


async def _noop_receive() -> Message:
    return {"type": "http.disconnect"}
