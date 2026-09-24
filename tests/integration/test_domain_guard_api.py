"""Admin endpoints for per-domain page budgets and denial cooldowns."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from scrapeyard.api.auth import parse_api_credentials
from scrapeyard.api.dependencies import get_domain_guard
from scrapeyard.api.middleware import APIKeyAuthMiddleware
from scrapeyard.api.routes import router
from scrapeyard.common.settings import get_settings
from scrapeyard.engine.domain_guard import LocalDomainGuard
from scrapeyard.main import http_exception_handler, request_validation_exception_handler

SECRETS = {
    "admin": "domain-admin-secret-00000",
    "project-admin": "project-admin-secret-0000",
    "reader": "domain-reader-secret-0000",
}


@pytest.fixture()
def guard() -> LocalDomainGuard:
    return LocalDomainGuard()


@pytest.fixture()
async def client(test_app, guard) -> AsyncIterator[AsyncClient]:
    del test_app
    credentials = parse_api_credentials(
        json.dumps(
            {
                "admin": {"secret": SECRETS["admin"], "scopes": ["transport-admin", "submit"]},
                "project-admin": {
                    "secret": SECRETS["project-admin"],
                    "scopes": ["transport-admin"],
                    "projects": ["alpha"],
                },
                "reader": {"secret": SECRETS["reader"], "scopes": ["read"]},
            }
        )
    )
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_middleware(APIKeyAuthMiddleware, credentials=credentials)
    app.dependency_overrides[get_domain_guard] = lambda: guard
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _headers(role: str) -> dict[str, str]:
    return {"X-API-Key": SECRETS[role]}


@pytest.mark.asyncio
async def test_admin_inspects_and_clears_a_host(client, guard):
    await guard.consume_page("shop.example.test", 0)
    await guard.consume_page("shop.example.test", 0)
    await guard.start_cooldown("shop.example.test", 600)

    shown = await client.get("/domains/WWW.shop.example.test/guard", headers=_headers("admin"))
    assert shown.status_code == 200
    body = shown.json()
    assert body["host"] == "shop.example.test"
    assert body["pages_today"] == 2
    assert body["cooldown_active"] is True
    assert 0 < body["cooldown_remaining_seconds"] <= 600
    assert body["cooldown_until"]
    assert body["daily_page_limit"] == get_settings().domain_daily_page_limit
    assert body["cooldown_seconds"] == get_settings().domain_denial_cooldown_seconds

    cleared = await client.delete("/domains/shop.example.test/guard", headers=_headers("admin"))
    assert cleared.status_code == 200
    assert cleared.json()["pages_today"] == 0
    assert cleared.json()["cooldown_active"] is False
    assert await guard.cooldown_remaining("shop.example.test") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.parametrize(
    "role,status",
    [(None, 401), ("reader", 403), ("project-admin", 403)],
)
async def test_domain_guard_requires_global_transport_admin(client, guard, method, role, status):
    await guard.start_cooldown("shop.example.test", 600)
    response = await getattr(client, method)(
        "/domains/shop.example.test/guard",
        headers=_headers(role) if role else {},
    )
    assert response.status_code == status
    assert await guard.cooldown_remaining("shop.example.test") > 0


@pytest.mark.asyncio
async def test_invalid_host_is_rejected(client):
    response = await client.get("/domains/bad%20host/guard", headers=_headers("admin"))
    assert response.status_code == 400
    assert "Invalid host" in response.json()["error"]


@pytest.mark.asyncio
async def test_page_cache_submission_requires_a_configured_directory(client):
    settings = get_settings()
    original = settings.page_cache_dir
    settings.page_cache_dir = ""
    try:
        response = await client.post(
            "/scrape",
            content=(
                "project: alpha\nname: cache\nexecution:\n  page_cache: record\n"
                "target:\n  url: https://shop.example.test/\n  selectors:\n    title: h1\n"
            ),
            headers={**_headers("admin"), "content-type": "application/x-yaml"},
        )
    finally:
        settings.page_cache_dir = original
    assert response.status_code == 422
    assert "SCRAPEYARD_PAGE_CACHE_DIR" in response.text
