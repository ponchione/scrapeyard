"""Bounded async transport for production basic HTTP fetches."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx
from scrapling.engines.toolbelt.custom import Response
from scrapling.engines.toolbelt.fingerprints import (
    generate_convincing_referer,
    generate_headers,
)

from scrapeyard.common.budgets import RunBudget

_STREAM_CHUNK_BYTES = 64 * 1024


def _request_headers(url: str, supplied: object, *, stealthy: bool) -> dict[str, str]:
    headers = {
        str(name): str(value)
        for name, value in (supplied.items() if isinstance(supplied, Mapping) else ())
    }
    names = {name.lower() for name in headers}
    generated = generate_headers(browser_mode=False)
    if stealthy:
        for name, value in generated.items():
            if name.lower() not in names:
                headers[str(name)] = str(value)
        if "referer" not in names:
            headers["referer"] = generate_convincing_referer(url)
    elif "user-agent" not in names:
        user_agent = generated.get("User-Agent")
        if user_agent is not None:
            headers["User-Agent"] = str(user_agent)
    return headers


def _declared_content_length(response: httpx.Response) -> int | None:
    if "transfer-encoding" in response.headers:
        return None
    values = response.headers.get_list("content-length")
    if len(values) != 1 or not values[0].isdigit():
        return None
    return int(values[0])


def _decode_body(body: bytes, encoding: str) -> str:
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _apply_cookie_jar(
    cookie_jar: httpx.Cookies,
    logical_url: str,
    headers: dict[str, str],
) -> httpx.Request:
    """Apply RFC cookie matching against the logical URL, not a pinned IP."""

    logical_request = httpx.Request("GET", logical_url)
    if "cookie" not in {name.lower() for name in headers}:
        strict_jar = CookieJar(
            policy=DefaultCookiePolicy(
                strict_ns_domain=DefaultCookiePolicy.DomainStrict,
            )
        )
        for cookie in cookie_jar.jar:
            strict_jar.set_cookie(cookie)
        httpx.Cookies(strict_jar).set_cookie_header(logical_request)
        cookie_header = logical_request.headers.get("cookie")
        if cookie_header is not None:
            headers["Cookie"] = cookie_header
    return logical_request


def _response_cookie_metadata(cookies: httpx.Cookies) -> dict[str, str]:
    """Return non-throwing Scrapling metadata for a possibly duplicate jar.

    Cookie routing retains the full domain/path-aware jar.  Scrapling's
    name-only metadata mapping uses the last value in jar order when response
    cookies repeat a name at different paths or domains.
    """

    return {cookie.name: cookie.value for cookie in cookies.jar}


async def fetch_streaming_response(
    fetcher_cls: Any,
    url: str,
    call_kwargs: dict[str, Any],
    *,
    budget: RunBudget | None,
) -> Any:
    """Stream one response and close it at the byte or monotonic run limit."""

    kwargs = dict(call_kwargs)
    proxy = kwargs.pop("proxy", None)
    retries = int(kwargs.pop("retries", 3) or 0)
    timeout = kwargs.pop("timeout", 10)
    follow_redirects = bool(kwargs.pop("follow_redirects", False))
    stealthy = bool(kwargs.pop("stealthy_headers", True))
    custom_config = kwargs.pop("custom_config", None) or {}
    if not isinstance(custom_config, dict):
        raise ValueError("Custom parser config must be a mapping")
    extensions = kwargs.pop("extensions", None)
    headers = _request_headers(url, kwargs.pop("headers", None), stealthy=stealthy)
    cookie_jar = kwargs.pop("cookie_jar", None)
    cookie_url = str(kwargs.pop("cookie_url", url))
    if cookie_jar is not None and not isinstance(cookie_jar, httpx.Cookies):
        raise TypeError("cookie_jar must be an httpx.Cookies instance")
    logical_request = (
        _apply_cookie_jar(cookie_jar, cookie_url, headers)
        if cookie_jar is not None
        else None
    )
    parser_arguments = {
        **fetcher_cls._generate_parser_arguments(),
        **custom_config,
    }

    transport = httpx.AsyncHTTPTransport(
        proxy=proxy,
        retries=retries,
        trust_env=False,
    )

    async def _request() -> Any:
        async with httpx.AsyncClient(
            transport=transport,
            trust_env=False,
        ) as client, client.stream(
                "GET",
                url,
                headers=headers,
                follow_redirects=follow_redirects,
                timeout=timeout,
                extensions=extensions,
            **kwargs,
        ) as response:
            if cookie_jar is not None and logical_request is not None:
                logical_response = httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    request=logical_request,
                )
                cookie_jar.extract_cookies(logical_response)
            if budget is not None:
                declared = _declared_content_length(response)
                if declared is not None:
                    await budget.check_fetched_capacity(declared)
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES):
                if budget is not None:
                    await budget.consume_fetched_bytes(len(chunk))
                body.extend(chunk)
            body_bytes = bytes(body)
            encoding = response.encoding or "utf-8"
            return Response(
                url=str(response.url),
                text=_decode_body(body_bytes, encoding),
                body=body_bytes,
                status=response.status_code,
                reason=response.reason_phrase,
                encoding=encoding,
                cookies=_response_cookie_metadata(response.cookies),
                headers=dict(response.headers),
                request_headers=dict(response.request.headers),
                method=response.request.method,
                history=[],
                **parser_arguments,
            )

    if budget is None:
        return await _request()
    task: asyncio.Task[Any] | None = None
    try:
        budget.check_deadline()
        task = asyncio.create_task(_request())
        done, _pending = await asyncio.wait(
            {task},
            timeout=budget.remaining_seconds,
        )
    except BaseException:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    assert task is not None
    if task in done:
        return task.result()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    budget.exhaust_duration()
