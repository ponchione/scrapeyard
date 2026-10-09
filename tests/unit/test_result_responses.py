from __future__ import annotations

import asyncio
import gc
import socket
import threading
import weakref
from collections.abc import AsyncIterator

import pytest
import uvicorn
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

import scrapeyard.api.result_responses as response_module
from scrapeyard.api.response_models import APICompatibility
from scrapeyard.api.result_responses import (
    ResultRequestSlot,
    ResultResponse,
    ResultResponseSizeExceeded,
    ResultResponseThreadPool,
    _render_result_response,
    encoded_result_response,
)
from scrapeyard.api.serializers import serialize_result_response
from scrapeyard.common.run_threads import RunThreadPool
from scrapeyard.runtime.metrics import REGISTRY


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


def test_result_response_preserves_stored_source_identity():
    artifact = _artifact() | {
        "project": "eyebox", "name": "bereli-optics", "run_id": "run-1", "config_hash": "a" * 64,
    }
    payload = serialize_result_response("job-1", run_id="run-1", status="complete", artifact=artifact)
    for key in ("project", "name", "job_id", "run_id", "config_hash"):
        assert payload[key] == artifact[key]


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


async def _encode_with_capacity(request: Request, *, pool: ResultResponseThreadPool, **kwargs):
    async with pool.request_capacity() as slot:
        return await encoded_result_response(request, pool=pool, slot=slot, **kwargs)


def _free_request_slots(pool: ResultResponseThreadPool) -> int:
    return pool._request_slots._value


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
        _encode_with_capacity(
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
        _encode_with_capacity(
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
        assert _free_request_slots(pool) == 1
    finally:
        if not task.done():
            task.cancel()
        pool.shutdown()


async def _send_response(response: Response, *, on_body=None) -> list[dict]:
    messages: list[dict] = []

    async def receive() -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        messages.append(message)
        if on_body is not None and message["type"] == "http.response.body":
            await on_body(message)

    await response({"type": "http", "method": "GET"}, receive, send)
    return messages


@pytest.mark.asyncio
async def test_result_response_sends_the_same_bytes_and_headers_in_chunks(monkeypatch):
    monkeypatch.setattr(response_module, "_RESPONSE_SEND_CHUNK_BYTES", 4)
    pool = ResultResponseThreadPool(1)
    body = memoryview(bytearray(b'{"results":["caf\xc3\xa9"]}'))
    try:
        async with pool.request_capacity() as slot:
            response = ResultResponse(body, slot)
        assert _free_request_slots(pool) == 0

        messages = await _send_response(response)

        plain = Response(content=bytes(body), media_type="application/json")
        assert messages[0] == {
            "type": "http.response.start",
            "status": 200,
            "headers": plain.raw_headers,
        }
        chunks = messages[1:]
        assert [len(chunk["body"]) for chunk in chunks] == [4, 4, 4, 4, 4, 1]
        assert b"".join(bytes(chunk["body"]) for chunk in chunks) == plain.body
        assert [chunk["more_body"] for chunk in chunks] == [True] * 5 + [False]
        assert _free_request_slots(pool) == 1
    finally:
        pool.shutdown()


@pytest.mark.asyncio
async def test_result_response_holds_its_request_slot_until_the_body_is_sent():
    pool = ResultResponseThreadPool(1)
    sending = asyncio.Event()
    release = asyncio.Event()

    async def stall(_message: dict) -> None:
        sending.set()
        await release.wait()

    try:
        async with pool.request_capacity() as slot:
            response = ResultResponse(memoryview(bytearray(b"{}")), slot)
        task = asyncio.create_task(_send_response(response, on_body=stall))
        await sending.wait()
        assert _free_request_slots(pool) == 0
        release.set()
        await task
        assert _free_request_slots(pool) == 1
    finally:
        release.set()
        pool.shutdown()


@pytest.mark.asyncio
async def test_stalled_or_unsent_result_response_releases_its_slot_after_the_hold(monkeypatch):
    monkeypatch.setattr(response_module, "_RESPONSE_SLOT_HOLD_SECONDS", 0.01)
    pool = ResultResponseThreadPool(1)
    try:
        async with pool.request_capacity() as slot:
            ResultResponse(memoryview(bytearray(b"{}")), slot)
        await asyncio.wait_for(pool._request_slots.acquire(), timeout=1)
        pool._request_slots.release()

        async def stall_forever(_message: dict) -> None:
            await asyncio.Event().wait()

        async with pool.request_capacity() as slot:
            response = ResultResponse(memoryview(bytearray(b"{}")), slot)
        task = asyncio.create_task(_send_response(response, on_body=stall_forever))
        await asyncio.wait_for(pool._request_slots.acquire(), timeout=1)
        pool._request_slots.release()
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The send's own release after the hold does not free a second slot.
        assert _free_request_slots(pool) == 1
    finally:
        pool.shutdown()


class _Body(bytearray):
    """A bytearray that can be weakly referenced."""


@pytest.mark.parametrize("http", ["httptools", "h11"])
async def test_stalled_result_download_is_closed_and_releases_its_body(monkeypatch, http):
    monkeypatch.setattr(response_module, "_RESPONSE_SEND_STALL_SECONDS", 0.3)
    size = 16 * 1024 * 1024
    bodies = [_Body(size)]
    body_ref = weakref.ref(bodies[0])
    slot_released = asyncio.Event()

    async def app(scope, receive, send) -> None:
        response = ResultResponse(memoryview(bodies.pop()), ResultRequestSlot(slot_released.set))
        await response(scope, receive, send)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            http=http,
            ws="none",
            lifespan="off",
            log_level="critical",
        )
    )
    serving = asyncio.create_task(server.serve())
    client = socket.socket()
    try:
        while not server.started:
            assert not serving.done(), serving.exception()
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(client, ("127.0.0.1", port))
        await loop.sock_sendall(client, b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
        received = await loop.sock_recv(client, 4096)
        assert received.startswith(b"HTTP/1.1 200")

        # The client stops reading with almost all of the body unsent.
        await asyncio.wait_for(slot_released.wait(), timeout=5)
        for _ in range(100):
            gc.collect()
            if body_ref() is None:
                break
            await asyncio.sleep(0.05)
        assert body_ref() is None

        # Reading again drains what the server had buffered, then hits its close.
        while chunk := await asyncio.wait_for(loop.sock_recv(client, 65536), timeout=5):
            received += chunk
        assert len(received) < size
    finally:
        client.close()
        server.should_exit = server.force_exit = True
        await asyncio.wait_for(serving, timeout=5)


@pytest.mark.asyncio
async def test_request_capacity_releases_a_handed_off_slot_when_the_request_fails():
    pool = ResultResponseThreadPool(1)
    try:
        with pytest.raises(RuntimeError):
            async with pool.request_capacity() as slot:
                ResultResponse(memoryview(bytearray(b"{}")), slot)
                raise RuntimeError("handler failed after rendering")
        assert _free_request_slots(pool) == 1
    finally:
        pool.shutdown()


def _work_gauge(name: str, kind: str) -> float | None:
    return REGISTRY.get_sample_value(name, {"kind": kind})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pool_type", "kind"),
    [(RunThreadPool, "run_threads"), (ResultResponseThreadPool, "result_response_threads")],
)
async def test_owned_thread_pools_report_capacity_and_lingering_work(pool_type, kind) -> None:
    pool = pool_type(1)
    started = threading.Event()
    release = threading.Event()

    def blocking_work(**_kwargs) -> None:
        started.set()
        release.wait()

    try:
        assert _work_gauge("scrapeyard_work_capacity", kind) == 1
        task = asyncio.create_task(pool.run(blocking_work))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert (pool.active, pool.lingering) == (1, 1)
        assert _work_gauge("scrapeyard_active_work", kind) == 1
        assert _work_gauge("scrapeyard_active_work", f"lingering_{kind}") == 1

        release.set()
        for _ in range(50):
            if pool.active == 0:
                break
            await asyncio.sleep(0.01)
        assert (pool.active, pool.lingering) == (0, 0)
        assert _work_gauge("scrapeyard_active_work", kind) == 0
        assert _work_gauge("scrapeyard_active_work", f"lingering_{kind}") == 0
    finally:
        release.set()
        pool.shutdown()
