"""Unit tests for API middleware guards."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from scrapeyard.api.middleware import RateLimitMiddleware


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
