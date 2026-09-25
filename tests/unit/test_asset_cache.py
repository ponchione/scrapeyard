from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scrapeyard.engine import asset_cache
from scrapeyard.engine.asset_cache import AssetCache, freshness_seconds

SITE = "example.test"
URL = "https://www.example.test/static/app.js"


def _request(url: str = URL, *, method: str = "GET", resource_type: str = "script") -> Any:
    return SimpleNamespace(url=url, method=method, resource_type=resource_type)


def _response(
    body: bytes = b"window.app = 1;",
    *,
    headers: dict[str, str] | None = None,
    status: int = 200,
    request: Any = None,
) -> Any:
    headers = {"cache-control": "public, max-age=600", **(headers or {})}

    async def all_headers() -> dict[str, str]:
        return headers

    async def read_body() -> bytes:
        return body

    return SimpleNamespace(
        status=status, request=request or _request(), all_headers=all_headers, body=read_body,
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"cache-control": "public, max-age=600"}, 600),
        ({"cache-control": "max-age=600, immutable", "age": "100"}, 500),
        ({"cache-control": 'max-age="60"'}, 60),
        (
            {"expires": "Thu, 01 Jan 2026 01:00:00 GMT", "date": "Thu, 01 Jan 2026 00:00:00 GMT"},
            3600,
        ),
        ({"cache-control": "max-age=600", "vary": "Accept-Encoding"}, 600),
        ({}, None),
        ({"cache-control": "max-age=0"}, None),
        ({"cache-control": "max-age=60", "age": "60"}, None),
        ({"cache-control": "no-store, max-age=600"}, None),
        ({"cache-control": "no-cache, max-age=600"}, None),
        ({"cache-control": "private, max-age=600"}, None),
        ({"cache-control": "max-age=soon"}, None),
        ({"cache-control": "max-age=600", "set-cookie": "id=1"}, None),
        ({"cache-control": "max-age=600", "vary": "Cookie"}, None),
        ({"cache-control": "max-age=600", "vary": "*"}, None),
        ({"expires": "0"}, None),
    ],
)
def test_only_responses_a_private_cache_may_reuse_are_fresh(
    headers: dict[str, str], expected: float | None,
) -> None:
    assert freshness_seconds(headers) == expected


async def test_a_stored_script_is_served_to_later_requests_of_the_same_site() -> None:
    cache = AssetCache()
    response = _response(headers={"content-encoding": "gzip", "content-type": "text/javascript"})
    assert cache.wants(SITE, response)

    await cache.store(SITE, response)

    asset = cache.lookup(SITE, _request())
    assert asset is not None and asset.body == b"window.app = 1;" and asset.status == 200
    # The body is stored decoded; fulfill re-frames it.
    assert asset.headers == {
        "cache-control": "public, max-age=600", "content-type": "text/javascript",
    }
    assert not cache.wants(SITE, response)
    assert cache.lookup("other-shop.test", _request()) is None
    assert cache.lookup(SITE, _request(method="POST")) is None
    assert cache.lookup(SITE, _request(resource_type="fetch")) is None


@pytest.mark.parametrize(
    "response",
    [
        _response(status=206),
        _response(request=_request(resource_type="xhr")),
        _response(request=_request(resource_type="document")),
        _response(request=_request(method="POST")),
    ],
)
def test_other_responses_are_never_candidates(response: Any) -> None:
    assert not AssetCache().wants(SITE, response)


async def test_expired_entries_are_not_served(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(asset_cache.time, "monotonic", lambda: now[0])
    cache = AssetCache()
    await cache.store(SITE, _response())

    now[0] += 599
    assert cache.lookup(SITE, _request()) is not None
    now[0] += 1
    assert cache.lookup(SITE, _request()) is None and cache.size == 0


async def test_the_cache_is_bounded_per_entry_and_in_total() -> None:
    cache = AssetCache(max_bytes=10, max_entry_bytes=6)
    await cache.store(SITE, _response(b"x" * 7))
    assert cache.size == 0
    await cache.store(SITE, _response(b"x" * 7, headers={"content-length": "3"}))
    assert cache.size == 0

    def script(name: str) -> Any:
        return _request(f"https://www.example.test/{name}.js")

    await cache.store(SITE, _response(b"x" * 4, request=script("a")))
    await cache.store(SITE, _response(b"x" * 4, request=script("b")))
    cache.lookup(SITE, script("a"))  # a is now the most recently used
    await cache.store(SITE, _response(b"x" * 4, request=script("c")))

    assert cache.size == 8
    assert cache.lookup(SITE, script("b")) is None
    assert cache.lookup(SITE, script("a")) is not None
    assert cache.lookup(SITE, script("c")) is not None


async def test_a_response_whose_body_is_gone_is_skipped() -> None:
    response = _response()

    async def closed() -> bytes:
        raise RuntimeError("Target page, context or browser has been closed")

    response.body = closed
    cache = AssetCache()

    await cache.store(SITE, response)

    assert cache.lookup(SITE, _request()) is None
