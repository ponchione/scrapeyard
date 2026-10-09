"""Bounded off-loop rendering for potentially large result responses."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from functools import partial
from typing import Any, TypeVar

from fastapi import Request, Response
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

from scrapeyard.api.response_models import APICompatibility
from scrapeyard.api.serializers import serialize_result_response
from scrapeyard.common.json_encoding import iter_json_bytes
from scrapeyard.runtime.metrics import ACTIVE_WORK, WORK_CAPACITY


T = TypeVar("T")

# A rendered body is sent in pieces of this size, so the server's flow control
# paces it to the client instead of copying it whole into the transport buffer.
_RESPONSE_SEND_CHUNK_BYTES = 256 * 1024
# A body still unsent after this long stops counting against request capacity,
# so a stalled connection cannot hold a slot until TCP gives up on it; the
# transfer itself continues. It is below Eyebox's 30 s request timeout, so a
# download waiting behind stalled ones still gets a slot in time.
_RESPONSE_SLOT_HOLD_SECONDS = 20.0


class ResultResponseEncodingCancelled(RuntimeError):
    """Raised inside a renderer after its client or request disappears."""


class ResultResponseSizeExceeded(ValueError):
    """Raised before a rendered result response crosses its byte ceiling."""


class ResultRequestSlot:
    """One request-capacity slot, released exactly once."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._released = False
        self.handed_off = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._release()


class ResultResponse(Response):
    """Rendered result JSON that keeps its request slot until it has been sent.

    The bytes and headers, including ``Content-Length``, are those of a plain
    ``Response`` with the same body.
    """

    def __init__(self, body: memoryview, slot: ResultRequestSlot) -> None:
        super().__init__(content=body, media_type="application/json")
        slot.handed_off = True
        self._slot = slot
        # Also releases the slot of a response that is never sent.
        self._hold_timer = asyncio.get_running_loop().call_later(
            _RESPONSE_SLOT_HOLD_SECONDS,
            slot.release,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            body = memoryview(self.body)
            size = len(body)
            start = 0
            while True:
                end = min(start + _RESPONSE_SEND_CHUNK_BYTES, size)
                await send(
                    {
                        "type": "http.response.body",
                        "body": body[start:end],
                        "more_body": end < size,
                    }
                )
                if end == size:
                    break
                start = end
        finally:
            self._hold_timer.cancel()
            self._slot.release()


class ResultResponseThreadPool:
    """Dedicated bounded executor with cooperative cancellation ownership."""

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError("Result response thread capacity must be positive")
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="scrapeyard-result-response",
        )
        self._slots = asyncio.Semaphore(max_workers)
        self._request_slots = asyncio.Semaphore(max_workers)
        self._state_lock = threading.Lock()
        self._active = 0
        self._lingering = 0
        WORK_CAPACITY.labels("result_response_threads").set(max_workers)
        ACTIVE_WORK.labels("result_response_threads").set(0)
        ACTIVE_WORK.labels("lingering_result_response_threads").set(0)

    @property
    def active(self) -> int:
        with self._state_lock:
            return self._active

    @property
    def lingering(self) -> int:
        with self._state_lock:
            return self._lingering

    @asynccontextmanager
    async def request_capacity(self) -> AsyncIterator[ResultRequestSlot]:
        """Bound result artifact loading, rendering and sending as one unit.

        A ``ResultResponse`` built with the slot takes it over and releases it
        once its body has been sent, so a rendered body in transit still
        counts against this capacity.
        """

        await self._request_slots.acquire()
        slot = ResultRequestSlot(self._request_slots.release)
        try:
            yield slot
        except BaseException:
            slot.release()
            raise
        if not slot.handed_off:
            slot.release()

    async def run(
        self,
        function: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Render with one owned slot and signal cancellation to the worker."""

        await self._slots.acquire()
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        lingering = False
        try:
            future = self._executor.submit(
                partial(
                    function,
                    *args,
                    stop_requested=stop.is_set,
                    **kwargs,
                )
            )
        except BaseException:
            self._slots.release()
            raise

        with self._state_lock:
            self._active += 1
            ACTIVE_WORK.labels("result_response_threads").set(self._active)

        def release_slot(completed: Future[T]) -> None:
            del completed
            with self._state_lock:
                self._active -= 1
                if lingering:
                    self._lingering -= 1
                ACTIVE_WORK.labels("result_response_threads").set(self._active)
                ACTIVE_WORK.labels("lingering_result_response_threads").set(
                    self._lingering
                )
            try:
                loop.call_soon_threadsafe(self._slots.release)
            except RuntimeError:
                pass

        future.add_done_callback(release_slot)
        wrapped = asyncio.wrap_future(future, loop=loop)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            stop.set()
            with self._state_lock:
                if not future.done():
                    lingering = True
                    self._lingering += 1
                    ACTIVE_WORK.labels("lingering_result_response_threads").set(
                        self._lingering
                    )
            raise

    def shutdown(self) -> None:
        """Wait for owned renderers and reject future submissions."""

        self._executor.shutdown(wait=True, cancel_futures=True)


def _render_result_response(
    job_id: str,
    run_id: str,
    status: str,
    artifact: Any,
    compatibility: APICompatibility,
    max_bytes: int,
    *,
    stop_requested: Callable[[], bool],
) -> memoryview:
    payload = serialize_result_response(
        job_id,
        run_id=run_id,
        status=status,
        artifact=artifact,
        compatibility=compatibility,
    )
    encoded = bytearray()
    for chunk in iter_json_bytes(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        default=None,
    ):
        if stop_requested():
            raise ResultResponseEncodingCancelled
        observed = len(encoded) + len(chunk)
        if observed > max_bytes:
            raise ResultResponseSizeExceeded(
                f"Result response exceeds the {max_bytes}-byte response ceiling"
            )
        encoded.extend(chunk)
    if stop_requested():
        raise ResultResponseEncodingCancelled
    return memoryview(encoded)


async def _wait_for_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def encoded_result_response(
    request: Request,
    *,
    pool: ResultResponseThreadPool,
    slot: ResultRequestSlot,
    job_id: str,
    run_id: str,
    status: str,
    artifact: Any,
    compatibility: APICompatibility,
    max_bytes: int,
) -> Response:
    """Render result JSON off-loop and stop promptly after disconnect/cancel.

    The response takes over *slot* from ``pool.request_capacity()``.
    """

    render_task = asyncio.create_task(
        pool.run(
            _render_result_response,
            job_id,
            run_id,
            status,
            artifact,
            compatibility,
            max_bytes,
        )
    )
    disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _pending = await asyncio.wait(
            {render_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if render_task not in done:
            render_task.cancel()
            with suppress(asyncio.CancelledError):
                await render_task
            raise ClientDisconnect
        body = render_task.result()
    except BaseException:
        if not render_task.done():
            render_task.cancel()
            with suppress(asyncio.CancelledError):
                await render_task
        raise
    finally:
        disconnect_task.cancel()
        with suppress(asyncio.CancelledError):
            await disconnect_task
    return ResultResponse(body, slot)
