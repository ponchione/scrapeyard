"""Integration tests for scheduled job lifecycle."""

from __future__ import annotations

import pytest

from scrapeyard.api.dependencies import get_job_store, get_scheduler, get_worker_pool
from scrapeyard.scheduler.cron import SchedulerService


def _scheduled_yaml() -> str:
    return """
project: integ
name: scheduled-job
schedule:
  cron: "*/5 * * * *"
  enabled: true
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


@pytest.mark.asyncio
async def test_scheduled_job_create_list_delete(client):
    create_response = await client.post(
        "/jobs",
        content=_scheduled_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert create_response.status_code == 201
    created = create_response.json()

    job_id = created["job_id"]
    assert created["project"] == "integ"
    assert created["name"] == "scheduled-job"
    assert created["schedule"] == "*/5 * * * *"

    list_response = await client.get("/jobs?project=integ")
    assert list_response.status_code == 200
    jobs = list_response.json()
    assert any(j["job_id"] == job_id for j in jobs)

    delete_response = await client.delete(f"/jobs/{job_id}")
    assert delete_response.status_code == 204

    list_after_delete = await client.get("/jobs?project=integ")
    assert list_after_delete.status_code == 200
    jobs_after_delete = list_after_delete.json()
    assert all(j["job_id"] != job_id for j in jobs_after_delete)


@pytest.mark.asyncio
async def test_scheduler_respects_priority_and_browser(client, monkeypatch):
    scheduler = get_scheduler()
    pool = get_worker_pool()
    enqueued: list[tuple[str, str, bool, str | None, str]] = []

    async def capture_enqueue(
        job_id: str,
        config_yaml: str,
        priority: str = "normal",
        needs_browser: bool = False,
        *,
        run_id: str | None = None,
        trigger: str = "adhoc",
    ):
        enqueued.append((job_id, priority, needs_browser, run_id, trigger))

    monkeypatch.setattr(pool, "enqueue", capture_enqueue)

    yaml = """
project: integ
name: priority-browser
schedule:
  cron: "*/5 * * * *"
  enabled: true
execution:
  priority: high
target:
  url: https://example.com
  fetcher: dynamic
  selectors:
    title: h1
"""
    response = await client.post(
        "/jobs",
        content=yaml,
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 201
    job_id = response.json()["job_id"]

    await scheduler._trigger_job(job_id)

    assert len(enqueued) == 1
    enqueued_job_id, priority, needs_browser, run_id, trigger = enqueued[0]
    assert enqueued_job_id == job_id
    assert priority == "high"
    assert needs_browser is True
    assert run_id is not None
    assert trigger == "scheduled"


@pytest.mark.asyncio
async def test_scheduler_assigns_distinct_run_ids_per_trigger(client, monkeypatch):
    scheduler = get_scheduler()
    pool = get_worker_pool()
    run_ids: list[str | None] = []

    async def capture_enqueue(
        job_id: str,
        config_yaml: str,
        priority: str = "normal",
        needs_browser: bool = False,
        *,
        run_id: str | None = None,
        trigger: str = "adhoc",
    ):
        del job_id, config_yaml, priority, needs_browser, trigger
        run_ids.append(run_id)

    monkeypatch.setattr(pool, "enqueue", capture_enqueue)

    response = await client.post(
        "/jobs",
        content=_scheduled_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 201
    job_id = response.json()["job_id"]

    await scheduler._trigger_job(job_id)
    await scheduler._trigger_job(job_id)

    assert len(run_ids) == 2
    assert run_ids[0] is not None
    assert run_ids[1] is not None
    assert run_ids[0] != run_ids[1]


@pytest.mark.asyncio
async def test_duplicate_scheduled_job_name_returns_409(client):
    response_1 = await client.post(
        "/jobs",
        content=_scheduled_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response_1.status_code == 201

    response_2 = await client.post(
        "/jobs",
        content=_scheduled_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response_2.status_code == 409
    assert "already exists" in response_2.json()["error"]


@pytest.mark.asyncio
async def test_duplicate_scheduled_job_name_allowed_across_projects(client):
    yaml_acme = _scheduled_yaml()
    yaml_other = yaml_acme.replace("project: integ", "project: other", 1)

    response_1 = await client.post(
        "/jobs",
        content=yaml_acme,
        headers={"content-type": "application/x-yaml"},
    )
    response_2 = await client.post(
        "/jobs",
        content=yaml_other,
        headers={"content-type": "application/x-yaml"},
    )

    assert response_1.status_code == 201
    assert response_2.status_code == 201


@pytest.mark.asyncio
async def test_disabled_schedule_stays_paused_after_scheduler_rehydrate(client):
    paused_yaml = _scheduled_yaml().replace("enabled: true", "enabled: false", 1)

    response = await client.post(
        "/jobs",
        content=paused_yaml,
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 201
    job_id = response.json()["job_id"]

    fresh_scheduler = SchedulerService(
        worker_pool=get_worker_pool(),
        job_store=get_job_store(),
        jitter_max_seconds=0,
    )

    try:
        await fresh_scheduler.start()
        aps_job = fresh_scheduler._scheduler.get_job(job_id)
        assert aps_job is not None
        assert aps_job.next_run_time is None
    finally:
        fresh_scheduler.shutdown()

    job = await get_job_store().get_job(job_id)
    assert job.schedule_enabled is False
