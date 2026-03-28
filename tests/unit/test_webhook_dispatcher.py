"""Tests for HttpWebhookDispatcher."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import pytest_asyncio
import respx

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.webhook.dispatcher import HttpWebhookDispatcher


@pytest_asyncio.fixture
async def dispatcher():
    instance = HttpWebhookDispatcher()
    try:
        yield instance
    finally:
        await instance.shutdown(timeout=0.1)


@pytest.mark.asyncio
async def test_dispatch_sends_post_with_payload(dispatcher):
    """Successful dispatch: POST to url with JSON body and custom headers."""
    config = WebhookConfig(
        url="https://hooks.example.com/callback",
        headers={"X-Secret": "abc123"},
        timeout=5,
    )
    payload = {"event": "job.complete", "job_id": "j-1"}

    with respx.mock:
        route = respx.post("https://hooks.example.com/callback").mock(
            return_value=httpx.Response(200)
        )

        await dispatcher.dispatch(config, payload)

    assert route.called
    request = route.calls[0].request
    assert request.headers["X-Secret"] == "abc123"
    assert request.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_dispatch_logs_info_on_success(dispatcher, caplog):
    """Successful dispatch logs INFO with URL, status code, and response time."""
    config = WebhookConfig(url="https://hooks.example.com/callback")
    payload = {"event": "job.complete"}

    with respx.mock:
        respx.post("https://hooks.example.com/callback").mock(
            return_value=httpx.Response(200)
        )

        with caplog.at_level(logging.INFO, logger="scrapeyard.webhook.dispatcher"):
            await dispatcher.dispatch(config, payload)

    assert any("200" in r.message and "hooks.example.com" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_dispatch_logs_warning_on_connection_error(dispatcher, caplog):
    """Connection error is caught and logged at WARNING, not propagated."""
    config = WebhookConfig(url="https://hooks.example.com/callback")
    payload = {"event": "job.failed"}

    with respx.mock:
        respx.post("https://hooks.example.com/callback").mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with caplog.at_level(logging.WARNING, logger="scrapeyard.webhook.dispatcher"):
            await dispatcher.dispatch(config, payload)  # must not raise

    assert any("failed" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_dispatch_logs_warning_on_non_2xx(dispatcher, caplog):
    """Non-2xx response is logged at WARNING, not propagated."""
    config = WebhookConfig(url="https://hooks.example.com/callback")
    payload = {"event": "job.complete"}

    with respx.mock:
        respx.post("https://hooks.example.com/callback").mock(
            return_value=httpx.Response(500)
        )

        with caplog.at_level(logging.WARNING, logger="scrapeyard.webhook.dispatcher"):
            await dispatcher.dispatch(config, payload)  # must not raise

    assert any("500" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_dispatch_logs_warning_on_timeout(dispatcher, caplog):
    """Timeout is caught and logged at WARNING, not propagated."""
    config = WebhookConfig(url="https://hooks.example.com/callback", timeout=1)
    payload = {"event": "job.complete"}

    with respx.mock:
        respx.post("https://hooks.example.com/callback").mock(
            side_effect=httpx.ReadTimeout("read timed out")
        )

        with caplog.at_level(logging.WARNING, logger="scrapeyard.webhook.dispatcher"):
            await dispatcher.dispatch(config, payload)  # must not raise

    assert any("failed" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_dispatch_does_not_log_payload_at_info(dispatcher, caplog):
    """Full payload must NOT appear at INFO level — only at DEBUG."""
    config = WebhookConfig(url="https://hooks.example.com/callback")
    payload = {"event": "job.complete", "secret_data": "s3cret"}

    with respx.mock:
        respx.post("https://hooks.example.com/callback").mock(
            return_value=httpx.Response(200)
        )

        with caplog.at_level(logging.INFO, logger="scrapeyard.webhook.dispatcher"):
            await dispatcher.dispatch(config, payload)

    info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert all("s3cret" not in m for m in info_messages)


@pytest.mark.asyncio
async def test_dispatch_reuses_shared_client_and_closes_on_shutdown():
    """Dispatcher should reuse one AsyncClient across multiple deliveries."""
    client = AsyncMock()
    client.post = AsyncMock(return_value=httpx.Response(200))
    client.aclose = AsyncMock()
    client_factory = Mock(return_value=client)

    dispatcher = HttpWebhookDispatcher(client_factory=client_factory)
    config = WebhookConfig(url="https://hooks.example.com/callback")

    await dispatcher.dispatch(config, {"event": "job.complete"})
    await dispatcher.dispatch(config, {"event": "job.partial"})

    assert client_factory.call_count == 1
    assert client.post.await_count == 2

    await dispatcher.shutdown(timeout=0.1)

    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_tracks_background_tasks_until_shutdown():
    """Submitted webhook tasks should be tracked and drained on shutdown."""
    dispatcher = HttpWebhookDispatcher()
    config = WebhookConfig(url="https://hooks.example.com/callback")
    payload = {"event": "job.complete"}
    release = asyncio.Event()
    deliveries: list[dict] = []

    async def _slow_dispatch(_config, submitted_payload):
        deliveries.append(submitted_payload)
        await release.wait()

    dispatcher.dispatch = _slow_dispatch  # type: ignore[method-assign]

    await dispatcher.submit(config, payload)
    await asyncio.sleep(0)

    assert dispatcher.pending_tasks == 1

    shutdown_task = asyncio.create_task(dispatcher.shutdown(timeout=1))
    await asyncio.sleep(0)
    assert not shutdown_task.done()

    release.set()
    await shutdown_task

    assert deliveries == [payload]
    assert dispatcher.pending_tasks == 0
