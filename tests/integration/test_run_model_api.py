"""Integration tests for expanded GET /jobs/{id} response and run_id in results."""

from __future__ import annotations

import asyncio

import pytest

from scrapeyard.engine.scraper import TargetResult


def _adhoc_yaml() -> str:
    return """
project: integ
name: run-model-test
execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


@pytest.mark.asyncio
async def test_job_with_no_runs_returns_empty_runs(client):
    """A freshly created scheduled job should have no runs and null timestamps."""
    yaml = """
project: integ
name: no-runs-job
schedule:
  cron: "*/10 * * * *"
  enabled: true
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""
    create = await client.post(
        "/jobs",
        content=yaml,
        headers={"content-type": "application/x-yaml"},
    )
    assert create.status_code == 201
    job_id = create.json()["job_id"]

    detail = await client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    data = detail.json()

    assert data["runs"] == []
    assert data["run_count"] == 0
    assert data["last_run_at"] is None


@pytest.mark.asyncio
async def test_completed_scrape_appears_in_job_runs(client, monkeypatch):
    """After a scrape completes, GET /jobs/{id} should include the run."""

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_adhoc_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    # Poll until the job finishes.
    for _ in range(40):
        r = await client.get(f"/jobs/{job_id}")
        if r.json()["status"] in ("complete", "partial", "failed"):
            break
        await asyncio.sleep(0.05)

    data = r.json()
    assert data["status"] == "complete"

    # Runs array should contain exactly one run.
    assert len(data["runs"]) == 1
    run = data["runs"][0]
    assert run["run_id"] is not None
    assert run["status"] == "complete"
    assert run["trigger"] == "adhoc"
    assert run["config_hash"] is not None
    assert run["started_at"] is not None
    assert run["completed_at"] is not None
    assert run["record_count"] == 1
    assert run["error_count"] == 0


@pytest.mark.asyncio
async def test_run_count_and_last_run_at_reflect_completed_run(client, monkeypatch):
    """run_count and last_run_at should update after a completed scrape."""

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_adhoc_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    for _ in range(40):
        r = await client.get(f"/jobs/{job_id}")
        if r.json()["status"] in ("complete", "partial", "failed"):
            break
        await asyncio.sleep(0.05)

    data = r.json()
    assert data["run_count"] == 1
    assert data["last_run_at"] is not None


@pytest.mark.asyncio
async def test_next_run_at_is_none_for_adhoc_jobs(client, monkeypatch):
    """Ad-hoc jobs are not scheduled, so next_run_at must be None."""

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_adhoc_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    for _ in range(40):
        r = await client.get(f"/jobs/{job_id}")
        if r.json()["status"] in ("complete", "partial", "failed"):
            break
        await asyncio.sleep(0.05)

    data = r.json()
    assert data["next_run_at"] is None


@pytest.mark.asyncio
async def test_results_include_run_id(client, monkeypatch):
    """After a scrape completes, GET /results/{id} should include the correct run_id."""

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_adhoc_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    # Poll until results are available.
    for _ in range(40):
        results_resp = await client.get(f"/results/{job_id}")
        if results_resp.status_code == 200:
            break
        assert results_resp.status_code == 202
        await asyncio.sleep(0.05)
    else:
        pytest.fail("Timed out waiting for results")

    results_data = results_resp.json()
    assert results_data["run_id"] is not None

    # The run_id in results should match the run_id in the job's runs array.
    job_resp = await client.get(f"/jobs/{job_id}")
    job_data = job_resp.json()
    assert len(job_data["runs"]) == 1
    assert results_data["run_id"] == job_data["runs"][0]["run_id"]


@pytest.mark.asyncio
async def test_get_nonexistent_job_returns_404(client):
    """GET /jobs/{id} with a non-existent ID should return 404."""
    resp = await client.get("/jobs/does-not-exist")
    assert resp.status_code == 404
    assert "not found" in resp.json()["error"]


@pytest.mark.asyncio
async def test_results_latest_false_without_run_id_returns_400(client, monkeypatch):
    """GET /results/{id}?latest=false without run_id should return 400."""

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_adhoc_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    for _ in range(40):
        r = await client.get(f"/results/{job_id}")
        if r.status_code == 200:
            break
        await asyncio.sleep(0.05)

    resp = await client.get(f"/results/{job_id}?latest=false")
    assert resp.status_code == 400
    assert "run_id" in resp.json()["error"]


@pytest.mark.asyncio
async def test_delete_job_with_delete_results(client, monkeypatch):
    """DELETE /jobs/{id}?delete_results=true should succeed."""

    async def _fake_scrape_target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "Hello"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _fake_scrape_target)

    response = await client.post(
        "/scrape",
        content=_adhoc_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    for _ in range(40):
        r = await client.get(f"/jobs/{job_id}")
        if r.json()["status"] in ("complete", "partial", "failed"):
            break
        await asyncio.sleep(0.05)

    resp = await client.delete(f"/jobs/{job_id}?delete_results=true")
    assert resp.status_code == 204

    # Confirm job is gone.
    resp = await client.get(f"/jobs/{job_id}")
    assert resp.status_code == 404
