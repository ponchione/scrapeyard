"""Resolved deployment secrets never cross diagnostic or result boundaries."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scrapeyard.api.dependencies import (
    get_result_store,
    get_webhook_dispatcher,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import ErrorType
from scrapeyard.storage.webhook_outbox import SQLiteWebhookOutboxStore
from tests.integration.conftest import poll_until_ready

SENTINEL = "deployment-secret-sentinel-91a7"


def _yaml(mode: str) -> str:
    return f"""
project: integ
name: secret-redaction-{mode}
execution:
  mode: {mode}
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
webhook:
  url: https://hooks.example.com/callback?auth=${{SCRAPEYARD_SECRET_AUDIT_SENTINEL}}
  on: [complete, failed]
target:
  url: https://example.com/resource?auth=${{SCRAPEYARD_SECRET_AUDIT_SENTINEL}}
  selectors:
    title: h1
"""


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("failed", [False, True])
async def test_secret_sentinel_absent_from_all_diagnostic_boundaries(
    client,
    monkeypatch,
    caplog,
    mode,
    failed,
):
    monkeypatch.setenv("SCRAPEYARD_SECRET_AUDIT_SENTINEL", SENTINEL)
    monkeypatch.setenv(
        "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
        '{"integ":["SCRAPEYARD_SECRET_AUDIT_SENTINEL"]}',
    )
    get_settings.cache_clear()
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(get_webhook_dispatcher(), "notify", AsyncMock())

    async def _scrape(target, *_args, **_kwargs):
        if not failed:
            return TargetResult(
                url=target.url,
                status="success",
                data=[{"title": SENTINEL}],
                pages_scraped=1,
                debug={"final_url": target.url, "diagnostic": SENTINEL},
            )
        detail = f"selector rejected ]{SENTINEL}"
        return TargetResult(
            url=target.url,
            status="failed",
            errors=[detail],
            error_type=ErrorType.selector_engine_error,
            error_detail=detail,
            debug={
                "final_url": target.url,
                "selector_failure": {
                    "query": f"]{SENTINEL}",
                    "exception_message": SENTINEL,
                },
            },
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _scrape)
    submitted = await client.post(
        "/scrape",
        content=_yaml(mode),
        headers={"content-type": "application/x-yaml"},
    )
    assert submitted.status_code in {200, 202}
    assert SENTINEL not in submitted.text
    job_id = submitted.json()["job_id"]
    result = await poll_until_ready(
        lambda: client.get(f"/results/{job_id}"),
        lambda response: response.status_code == 200,
    )
    assert SENTINEL not in result.text

    metadata = await get_result_store().get_result_metadata(job_id)
    assert metadata is not None
    artifact = Path(metadata.file_path) / "results.json"
    assert SENTINEL not in artifact.read_text()

    errors = await client.get(f"/errors?job_id={job_id}")
    assert errors.status_code == 200
    assert SENTINEL not in errors.text
    assert SENTINEL.encode() not in (Path(get_settings().db_dir) / "errors.db").read_bytes()

    deliveries = await SQLiteWebhookOutboxStore().list_pending()
    matching_payloads = [
        delivery.payload for delivery in deliveries if delivery.job_id == job_id
    ]
    assert len(matching_payloads) == 1
    assert SENTINEL not in json.dumps(matching_payloads)
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize(
    ("secret", "url", "leaked"),
    [
        (SENTINEL, "${SECRET}", SENTINEL),
        # The scheme comes back lower-cased in the message.
        ("Xk9TqP2mVb7RwL4z", "${SECRET}://example.com/", "xk9tqp2mvb7rwl4z"),
        # Pydantic quotes a long input cut short, which no exact match finds.
        ("tok_" + "A1b2C3d4E5f6G7h8I9j0" * 3, "${SECRET}", "A1b2C3d4E5f6G7h8I9j0"),
        # And escapes a backslash or a quote in it.
        ("pa\\ss'w0rd-0123", "${SECRET}", "pa\\\\ss'w0rd"),
    ],
)
async def test_resolved_secret_is_redacted_from_config_validation_errors(
    client,
    monkeypatch,
    secret,
    url,
    leaked,
):
    monkeypatch.setenv("SCRAPEYARD_SECRET_AUDIT_SENTINEL", secret)
    monkeypatch.setenv(
        "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
        '{"integ":["SCRAPEYARD_SECRET_AUDIT_SENTINEL"]}',
    )
    get_settings.cache_clear()
    destination = url.replace("SECRET", "SCRAPEYARD_SECRET_AUDIT_SENTINEL")
    response = await client.post(
        "/scrape",
        content=f"""
project: integ
name: invalid-secret-destination
target:
  url: "{destination}"
  selectors:
    title: h1
""",
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 422
    message = response.json()["error"]
    assert "target.url" in message
    assert leaked not in message


async def test_config_error_withholds_a_validator_message_about_a_secret(client, monkeypatch):
    # A transform pipeline splits at `:` and `|`, and its error quotes a
    # piece, which no match for the whole secret finds.
    monkeypatch.setenv("SCRAPEYARD_SECRET_AUDIT_SENTINEL", "Abcd1234:Efgh5678|Ijkl")
    monkeypatch.setenv(
        "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
        '{"integ":["SCRAPEYARD_SECRET_AUDIT_SENTINEL"]}',
    )
    get_settings.cache_clear()
    response = await client.post(
        "/scrape",
        content="""
project: integ
name: invalid-secret-transform
target:
  url: https://example.com/
  selectors:
    title:
      query: h1
      transform: "prepend:${SCRAPEYARD_SECRET_AUDIT_SENTINEL}"
    price:
      query: .price
      transform: nosuch
""",
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 422
    message = response.json()["error"]
    assert "target.selectors.title" in message
    for piece in ("Abcd1234", "Efgh5678", "Ijkl"):
        assert piece not in message
    # A message about a value that held no secret is kept.
    assert "Unknown transform: 'nosuch'" in message


async def test_config_error_withholds_a_secret_message_past_a_decoy_union_key(
    client, monkeypatch
):
    # The error's location names the union member `SelectorLong`. An input
    # key spelled the same once led the location's walk into that key, away
    # from the secret, and the transform's message quoted a piece of it.
    monkeypatch.setenv("SCRAPEYARD_SECRET_AUDIT_SENTINEL", "Abcd1234:Efgh5678|Ijkl")
    monkeypatch.setenv(
        "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
        '{"integ":["SCRAPEYARD_SECRET_AUDIT_SENTINEL"]}',
    )
    get_settings.cache_clear()
    response = await client.post(
        "/scrape",
        content="""
project: integ
name: invalid-secret-transform-decoy
target:
  url: https://example.com/
  selectors:
    title:
      query: h1
      transform: "${SCRAPEYARD_SECRET_AUDIT_SENTINEL}"
      SelectorLong:
        transform: x
""",
        headers={"content-type": "application/x-yaml"},
    )

    assert response.status_code == 422
    message = response.json()["error"]
    assert "target.selectors.title.SelectorLong.transform" in message
    for piece in ("Abcd1234", "Efgh5678", "Ijkl"):
        assert piece not in message
