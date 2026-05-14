"""Unit tests for API middleware guards."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from scrapeyard.api.middleware import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    _api_key_is_valid,
)


class ManualClock:
    def __init__(self, current: float = 100.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current


def _rate_limited_app(
    *,
    requests: int = 2,
    window_seconds: float = 10.0,
    api_keys: set[str] | None = None,
    clock: ManualClock | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        requests=requests,
        window_seconds=window_seconds,
        api_keys=api_keys or set(),
        exempt_paths={"/health"},
        clock=clock,
    )
    return app


def _size_limited_app(*, max_bytes: int = 3) -> FastAPI:
    app = FastAPI()

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"len": len(body)}

    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=max_bytes)
    return app


@pytest.mark.asyncio
async def test_rate_limit_rejects_after_configured_requests_and_sets_retry_after() -> None:
    clock = ManualClock()
    app = _rate_limited_app(requests=2, window_seconds=10.0, clock=clock)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/limited")).status_code == 200
        assert (await client.get("/limited")).status_code == 200
        response = await client.get("/limited")

    assert response.status_code == 429
    assert response.json() == {"error": "Rate limit exceeded"}
    assert response.headers["retry-after"] == "10"


@pytest.mark.asyncio
async def test_rate_limit_window_expires_old_requests() -> None:
    clock = ManualClock()
    app = _rate_limited_app(requests=1, window_seconds=10.0, clock=clock)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/limited")).status_code == 200
        assert (await client.get("/limited")).status_code == 429
        clock.current += 10.1
        assert (await client.get("/limited")).status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_uses_valid_api_key_before_client_ip() -> None:
    clock = ManualClock()
    app = _rate_limited_app(
        requests=1,
        window_seconds=10.0,
        api_keys={"key-a", "key-b"},
        clock=clock,
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/limited", headers={"X-API-Key": "key-a"})).status_code == 200
        assert (await client.get("/limited", headers={"X-API-Key": "key-a"})).status_code == 429
        assert (await client.get("/limited", headers={"X-API-Key": "key-b"})).status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_invalid_api_keys_fall_back_to_client_ip() -> None:
    clock = ManualClock()
    app = _rate_limited_app(requests=1, window_seconds=10.0, api_keys={"real-key"}, clock=clock)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/limited", headers={"X-API-Key": "bad-a"})).status_code == 200
        response = await client.get("/limited", headers={"X-API-Key": "bad-b"})

    assert response.status_code == 429


def test_api_key_validation_treats_non_ascii_input_as_invalid() -> None:
    assert _api_key_is_valid("kéy", {"key"}) is False


@pytest.mark.asyncio
async def test_rate_limit_exempt_paths_are_not_limited_or_counted() -> None:
    clock = ManualClock()
    app = _rate_limited_app(requests=1, window_seconds=10.0, clock=clock)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/limited")).status_code == 200
        assert (await client.get("/limited")).status_code == 429


@pytest.mark.asyncio
async def test_size_limit_rejects_declared_oversized_body() -> None:
    transport = ASGITransport(app=_size_limited_app(max_bytes=3))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=b"abcd")

    assert response.status_code == 413
    assert response.json() == {"error": "Request body too large"}


@pytest.mark.asyncio
async def test_size_limit_rejects_negative_content_length() -> None:
    app_called = False

    async def app(_scope, _receive, _send):
        nonlocal app_called
        app_called = True

    middleware = RequestSizeLimitMiddleware(app, max_bytes=3)
    messages = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/echo",
            "headers": [(b"content-length", b"-1")],
        },
        receive,
        send,
    )

    assert app_called is False
    assert messages[0]["status"] == 400


@pytest.mark.asyncio
async def test_size_limit_rejects_streaming_oversized_body() -> None:
    async def chunks():
        yield b"ab"
        yield b"cd"

    transport = ASGITransport(app=_size_limited_app(max_bytes=3))

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/echo", content=chunks())

    assert response.status_code == 413
    assert response.json() == {"error": "Request body too large"}
