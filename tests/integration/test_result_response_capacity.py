"""Result downloads count against result request capacity until they are sent."""

from __future__ import annotations

import asyncio
import json

import pytest

from scrapeyard.api.dependencies import (
    get_job_store,
    get_result_response_thread_pool,
    get_result_store,
)
from scrapeyard.models.job import Job


async def _download(app, path: str, *, gate: asyncio.Event | None = None):
    sending = asyncio.Event()
    messages: list[dict] = []
    raw_path, _, query = path.partition("?")

    async def receive() -> dict:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.body":
            sending.set()
            if gate is not None:
                await gate.wait()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": raw_path,
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    task = asyncio.create_task(app(scope, receive, send))
    return task, sending, messages


@pytest.mark.asyncio
async def test_result_download_holds_request_capacity_until_its_body_is_sent(test_app):
    job_id = "capacity-job"
    await get_job_store().save_job(
        Job(job_id=job_id, project="integ", name="capacity", config_yaml="target: x")
    )
    await get_result_store().save_result(job_id, [{"title": "held"}], run_id="run-1")
    path = f"/results/{job_id}?run_id=run-1&latest=false"
    slots = get_result_response_thread_pool().max_workers
    gate = asyncio.Event()

    stalled = [await _download(test_app, path, gate=gate) for _ in range(slots)]
    try:
        for _task, sending, _messages in stalled:
            await asyncio.wait_for(sending.wait(), timeout=2)
        waiting_task, waiting_sending, waiting_messages = await _download(test_app, path)
        await asyncio.sleep(0.05)
        # Every slot is held by a body still being sent to its client.
        assert waiting_messages == []

        gate.set()
        await asyncio.wait_for(
            asyncio.gather(waiting_task, *(task for task, _sending, _messages in stalled)),
            timeout=2,
        )
    finally:
        gate.set()

    for _task, _sending, messages in [*stalled, (waiting_task, waiting_sending, waiting_messages)]:
        assert messages[0]["status"] == 200
        body = b"".join(bytes(message["body"]) for message in messages[1:])
        assert json.loads(body)["results"] == [{"title": "held"}]
        headers = dict(messages[0]["headers"])
        assert int(headers[b"content-length"]) == len(body)
        assert b"content-encoding" not in headers
