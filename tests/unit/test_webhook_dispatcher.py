"""Tests for HttpWebhookDispatcher."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from scrapeyard.config.schema import WebhookConfig
from scrapeyard.webhook.dispatcher import HttpWebhookDispatcher


@pytest.fixture
def dispatcher():
    return HttpWebhookDispatcher()


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
