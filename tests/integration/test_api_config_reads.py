"""Config parsing, decryption and redaction cost on the API's polled routes."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Any

import pytest

from scrapeyard.api import serializers
from scrapeyard.api.dependencies import get_job_store, get_scheduler, get_worker_pool
from scrapeyard.api.response_models import JobDetailResponse, JobSummaryResponse
from scrapeyard.api.serializers import serialize_job_detail, serialize_job_summary
from scrapeyard.common.yaml import load_yaml_mapping
from scrapeyard.engine.url_guard import redact_sensitive_config_text
from scrapeyard.storage.database import get_db
from scrapeyard.storage.secret_envelope import protect_text

CONFIG = """\
project: integ
name: {name}
execution:
  mode: async
webhook:
  url: https://example.com/hook
  headers:
    Authorization: Bearer webhook-secret
target:
  url: https://example.com/?api_key=target-secret
  fetcher: basic
  selectors:
    title: h1
"""


@pytest.fixture()
def queued_only(monkeypatch):
    """Accept submissions without running them, so only the API touches configs."""

    class _Queued:
        async def result(self, timeout: float | None = None, *, poll_delay: float = 0.5) -> None:
            raise AssertionError("async submissions are never awaited")

    async def _enqueue(*_args: Any, **_kwargs: Any) -> _Queued:
        return _Queued()

    monkeypatch.setattr(get_worker_pool(), "enqueue", _enqueue)


async def _submit(client, name: str) -> tuple[str, dict[str, Any]]:
    body = CONFIG.format(name=name)
    response = await client.post(
        "/scrape",
        content=body,
        headers={"content-type": "application/x-yaml", "Idempotency-Key": f"key-{name}"},
    )
    assert response.status_code == 202, response.text
    return body, response.json()


def _counting(monkeypatch, target: str, original: Any) -> list[str]:
    threads: list[str] = []

    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, _wrapper)
    return threads


async def test_scrape_parses_the_submitted_yaml_once(client, queued_only, monkeypatch):
    parses = _counting(monkeypatch, "scrapeyard.config.loader.load_yaml_mapping", load_yaml_mapping)

    body, accepted = await _submit(client, "parse-once")

    assert len(parses) == 1
    assert parses[0] != threading.main_thread().name
    exact = await client.get(f"/jobs/{accepted['job_id']}/runs/{accepted['run_id']}")
    assert exact.json()["config_hash"] == hashlib.sha256(body.encode()).hexdigest()


async def test_exact_run_parses_each_config_once_off_the_event_loop(
    client, queued_only, monkeypatch,
):
    body, accepted = await _submit(client, "exact-run-poll")
    parses = _counting(monkeypatch, "scrapeyard.storage.job_store.load_yaml_mapping", load_yaml_mapping)
    path = f"/jobs/{accepted['job_id']}/runs/{accepted['run_id']}"

    polls = [await client.get(path) for _ in range(3)]

    assert {poll.status_code for poll in polls} == {200}
    assert polls[0].json() == polls[1].json() == polls[2].json()
    assert polls[0].json()["name"] == "exact-run-poll"
    assert polls[0].json()["project"] == "integ"
    assert polls[0].json()["config_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert len(parses) == 1
    assert parses[0] != threading.main_thread().name


async def test_exact_run_reads_the_parent_project_without_its_config(
    client, queued_only, monkeypatch,
):
    _body, accepted = await _submit(client, "exact-run-parent")
    async with get_db("jobs.db") as db:
        # The parent's own config is never needed to authorize an exact run.
        await db.execute(
            "UPDATE jobs SET config_yaml = 'syenc:v1:test-v1:AAAA' WHERE job_id = ?",
            (accepted["job_id"],),
        )
        await db.commit()

    exact = await client.get(f"/jobs/{accepted['job_id']}/runs/{accepted['run_id']}")

    assert exact.status_code == 200
    assert exact.json()["name"] == "exact-run-parent"
    missing = await client.get(f"/jobs/no-such-job/runs/{accepted['run_id']}")
    assert missing.status_code == 404
    assert missing.json()["error"] == "Job 'no-such-job' not found"


async def test_exact_run_verifies_the_stored_config_on_every_read(
    client, queued_only, monkeypatch,
):
    _body, accepted = await _submit(client, "exact-run-verify")
    path = f"/jobs/{accepted['job_id']}/runs/{accepted['run_id']}"
    assert (await client.get(path)).status_code == 200
    tampered = protect_text(
        CONFIG.format(name="someone-else"),
        purpose=f"queued_run_snapshots.config_yaml:{accepted['run_id']}",
    )
    async with get_db("jobs.db") as db:
        await db.execute(
            "UPDATE queued_run_snapshots SET config_yaml = ? WHERE run_id = ?",
            (tampered, accepted["run_id"]),
        )
        await db.commit()

    with pytest.raises(ValueError, match="hash mismatch"):
        await client.get(path)


async def test_job_detail_body_is_unchanged_and_redacts_once(client, queued_only, monkeypatch):
    monkeypatch.setattr(serializers, "_redacted_configs", OrderedDict())
    redactions = _counting(
        monkeypatch,
        "scrapeyard.api.serializers.redact_sensitive_config_text",
        redact_sensitive_config_text,
    )
    _body, accepted = await _submit(client, "detail-poll")
    job_id = accepted["job_id"]

    details = [await client.get(f"/jobs/{job_id}") for _ in range(3)]

    store = get_job_store()
    job = await store.get_job(job_id)
    run_count, last_run_at = await store.get_job_run_stats(job_id)
    # The body the route built before it cached redaction and folded the stats read.
    expected = JobDetailResponse.model_validate(
        serialize_job_detail(
            job,
            redacted_config_yaml=redact_sensitive_config_text(job.config_yaml),
            runs=await store.get_job_runs(job_id, limit=10),
            run_count=run_count,
            last_run_at=last_run_at,
            next_run_at=get_scheduler().get_next_run_time(job_id),
        )
    ).model_dump(mode="json")
    assert {detail.status_code for detail in details} == {200}
    assert [detail.json() for detail in details] == [expected] * 3
    assert "webhook-secret" not in details[0].text
    assert "target-secret" not in details[0].text
    assert len(redactions) == 1
    assert redactions[0] != threading.main_thread().name


async def test_job_list_body_is_unchanged_without_decrypting_configs(
    client, queued_only, monkeypatch,
):
    _body, accepted = await _submit(client, "list-summary")
    store = get_job_store()
    job = await store.get_job(accepted["job_id"])
    run_count, last_run_at = await store.get_job_run_stats(accepted["job_id"])

    def _no_decrypt(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("GET /jobs decrypted a config")

    monkeypatch.setattr("scrapeyard.storage.job_rows.reveal_text", _no_decrypt)
    listed = await client.get("/jobs", params={"project": "integ"})

    assert listed.status_code == 200
    # The body the route built from fully decrypted jobs.
    assert listed.json() == [
        JobSummaryResponse.model_validate(
            serialize_job_summary(job, run_count=run_count, last_run_at=last_run_at)
        ).model_dump(mode="json")
    ]
