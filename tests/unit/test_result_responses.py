from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

import scrapeyard.api.result_responses as response_module
from scrapeyard.api.response_models import APICompatibility
from scrapeyard.api.result_responses import (
    ResultResponseSizeExceeded,
    ResultResponseThreadPool,
    _render_result_response,
    encoded_result_response,
)
from scrapeyard.api.serializers import serialize_result_response


def _artifact(*, grouped: bool = True) -> dict:
    return {
        "job_id": "job-1",
        "completed_at": "2026-07-15T12:00:00+00:00",
        "errors": [],
        "targets": [
            {
                "status": "success",
                "count": 1,
                "observed_count": 1,
            }
        ],
        "results": (
            {"example.com": {"data": [{"title": "café"}]}}
            if grouped
            else [{"title": "café", "_source": "example.com"}]
        ),
    }


@pytest.mark.parametrize(
    "compatibility",
    [APICompatibility.v1, APICompatibility.legacy_v0],
)
def test_off_loop_renderer_preserves_jsonresponse_contract(compatibility):
    artifact = _artifact()
    payload = serialize_result_response(
        "job-1",
        run_id="run-1",
        status="complete",
        artifact=artifact,
        compatibility=compatibility,
    )

    rendered = _render_result_response(
        "job-1",
        "run-1",
        "complete",
        artifact,
        compatibility,
        1_000_000,
        stop_requested=lambda: False,
    )

    assert bytes(rendered) == JSONResponse(payload).body
    assert isinstance(rendered.obj, bytearray)


@pytest.mark.parametrize("grouped", [True, False])
def test_near_limit_grouped_and_merged_render_use_one_mutable_buffer(grouped):
    artifact = _artifact(grouped=grouped)
    large_value = "x" * 250_000
    if grouped:
        artifact["results"]["example.com"]["data"][0]["value"] = large_value
    else:
        artifact["results"][0]["value"] = large_value

    rendered = _render_result_response(
        "job-1",
        "run-1",
        "complete",
        artifact,
        APICompatibility.v1,
        300_000,
        stop_requested=lambda: False,
    )

    assert 250_000 < len(rendered) < 300_000
    assert isinstance(rendered.obj, bytearray)


def test_response_renderer_stops_before_extending_past_limit():
    with pytest.raises(ResultResponseSizeExceeded):
        _render_result_response(
            "job-1",
            "run-1",
            "complete",
            _artifact(),
            APICompatibility.v1,
            32,
            stop_requested=lambda: False,
        )


@pytest.mark.asyncio
async def test_request_capacity_bounds_artifact_loading_before_rendering():
    pool = ResultResponseThreadPool(1)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with pool.request_capacity():
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        async with pool.request_capacity():
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    try:
        await first_entered.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()
    finally:
        release_first.set()
        pool.shutdown()


async def _request(receive: AsyncIterator[dict]) -> Request:
    iterator = receive.__aiter__()

    async def receive_message() -> dict:
        return await anext(iterator)

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/results/job-1",
            "headers": [],
        },
        receive=receive_message,
    )


@pytest.mark.asyncio
async def test_large_response_render_keeps_event_loop_responsive(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_iter = response_module.iter_json_bytes

    def delayed_iter(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        yield from real_iter(*args, **kwargs)

    async def request_messages() -> AsyncIterator[dict]:
        await asyncio.Event().wait()
        yield {"type": "http.disconnect"}

    monkeypatch.setattr(response_module, "iter_json_bytes", delayed_iter)
    pool = ResultResponseThreadPool(1)
    request = await _request(request_messages())
    task = asyncio.create_task(
        encoded_result_response(
            request,
            pool=pool,
            job_id="job-1",
            run_id="run-1",
            status="complete",
            artifact=_artifact(),
            compatibility=APICompatibility.v1,
            max_bytes=1_000_000,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(progressed.set)
        await asyncio.wait_for(progressed.wait(), timeout=0.05)
        release.set()
        response = await asyncio.wait_for(task, timeout=1)
        assert response.status_code == 200
    finally:
        release.set()
        if not task.done():
            task.cancel()
        pool.shutdown()


@pytest.mark.asyncio
async def test_client_disconnect_stops_response_work_and_releases_capacity(monkeypatch):
    started = threading.Event()
    disconnect = asyncio.Event()

    def slow_iter(*_args, **_kwargs):
        started.set()
        while True:
            threading.Event().wait(0.001)
            yield b"x"

    async def request_messages() -> AsyncIterator[dict]:
        await disconnect.wait()
        yield {"type": "http.disconnect"}

    monkeypatch.setattr(response_module, "iter_json_bytes", slow_iter)
    pool = ResultResponseThreadPool(1)
    request = await _request(request_messages())
    task = asyncio.create_task(
        encoded_result_response(
            request,
            pool=pool,
            job_id="job-1",
            run_id="run-1",
            status="complete",
            artifact=_artifact(),
            compatibility=APICompatibility.v1,
            max_bytes=1_000_000,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 1)
        disconnect.set()
        with pytest.raises(ClientDisconnect):
            await asyncio.wait_for(task, timeout=1)
        for _index in range(100):
            if pool.active == 0:
                break
            await asyncio.sleep(0.001)
        assert pool.active == 0
        assert pool.lingering == 0
    finally:
        if not task.done():
            task.cancel()
        pool.shutdown()
