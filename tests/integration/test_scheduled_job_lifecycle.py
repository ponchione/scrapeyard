"""Integration tests for scheduled job lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib

import pytest

from scrapeyard.api.dependencies import get_job_store, get_scheduler, get_worker_pool
from scrapeyard.scheduler.cron import SchedulerService
from scrapeyard.engine.scraper import TargetResult
from tests.integration.conftest import poll_until_ready


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


def _managed_yaml(
    *,
    cron: str = "0 9 * * *",
    timezone_name: str = "America/New_York",
    enabled: bool = True,
) -> str:
    return f"""
project: integ
name: managed-schedule
schedule:
  cron: "{cron}"
  timezone: "{timezone_name}"
  enabled: {str(enabled).lower()}
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

    cancel_response = await client.post(f"/jobs/{job_id}/cancel")
    assert cancel_response.status_code == 204
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
async def test_scheduler_assigns_distinct_run_ids_per_completed_trigger(client, monkeypatch):
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
    assert run_ids[0] is not None
    assert await get_job_store().fail_queued_run(job_id, run_ids[0], datetime.now(timezone.utc))
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


@pytest.mark.asyncio
async def test_update_enables_disabled_schedule_and_changes_timezone(client):
    created = await client.post(
        "/jobs",
        content=_managed_yaml(enabled=False),
        headers={"content-type": "application/x-yaml"},
    )
    job_id = created.json()["job_id"]
    replacement = _managed_yaml(
        cron="30 14 * * 1-5",
        timezone_name="Europe/London",
        enabled=True,
    )

    updated = await client.put(
        f"/jobs/{job_id}",
        content=replacement,
        headers={"content-type": "application/x-yaml"},
    )

    assert updated.status_code == 200
    assert updated.json()["schedule_cron"] == "30 14 * * 1-5"
    assert updated.json()["schedule_timezone"] == "Europe/London"
    assert updated.json()["schedule_enabled"] is True
    assert updated.json()["config_hash"] == hashlib.sha256(
        replacement.encode("utf-8")
    ).hexdigest()
    stored = await get_job_store().get_job(job_id)
    assert stored.config_yaml == replacement
    aps_job = get_scheduler()._scheduler.get_job(job_id)
    assert aps_job is not None
    assert str(aps_job.trigger.timezone) == "Europe/London"
    assert aps_job.next_run_time is not None


@pytest.mark.asyncio
async def test_pause_resume_survives_scheduler_restart(client):
    created = await client.post(
        "/jobs",
        content=_managed_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    job_id = created.json()["job_id"]

    paused = await client.post(f"/jobs/{job_id}/pause")
    assert paused.status_code == 200
    assert paused.json()["schedule_enabled"] is False
    assert get_scheduler()._scheduler.get_job(job_id).next_run_time is None

    fresh = SchedulerService(
        worker_pool=get_worker_pool(),
        job_store=get_job_store(),
        jitter_max_seconds=0,
    )
    try:
        await fresh.start()
        assert fresh._scheduler.get_job(job_id).next_run_time is None
    finally:
        fresh.shutdown()

    resumed = await client.post(f"/jobs/{job_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["schedule_enabled"] is True
    assert get_scheduler()._scheduler.get_job(job_id).next_run_time is not None


@pytest.mark.asyncio
async def test_manual_trigger_uses_documented_config_version(client, monkeypatch):
    async def _target(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[{"title": "manual"}],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _target)
    yaml = _managed_yaml(enabled=False)
    created = await client.post(
        "/jobs",
        content=yaml,
        headers={"content-type": "application/x-yaml"},
    )
    job_id = created.json()["job_id"]

    triggered = await client.post(f"/jobs/{job_id}/trigger")
    assert triggered.status_code == 202
    payload = triggered.json()
    assert payload["trigger"] == "manual"
    assert payload["config_hash"] == hashlib.sha256(yaml.encode("utf-8")).hexdigest()

    detail = await poll_until_ready(
        lambda: client.get(f"/jobs/{job_id}"),
        lambda response: bool(response.json()["runs"]),
        failure_message="Timed out waiting for manual run history",
    )
    run = detail.json()["runs"][0]
    assert run["run_id"] == payload["run_id"]
    assert run["trigger"] == "manual"
    assert run["config_hash"] == payload["config_hash"]


@pytest.mark.asyncio
async def test_update_and_second_manual_trigger_conflict_with_active_run(
    client,
    monkeypatch,
):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _target(*_args, **_kwargs):
        started.set()
        await release.wait()
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _target)
    created = await client.post(
        "/jobs",
        content=_managed_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    job_id = created.json()["job_id"]
    first = await client.post(f"/jobs/{job_id}/trigger")
    assert first.status_code == 202
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        update = await client.put(
            f"/jobs/{job_id}",
            content=_managed_yaml(cron="0 10 * * *"),
            headers={"content-type": "application/x-yaml"},
        )
        second = await client.post(f"/jobs/{job_id}/trigger")
    finally:
        release.set()
    assert update.status_code == 409
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_failed_scheduler_update_restores_database_state(
    client,
    monkeypatch,
):
    created = await client.post(
        "/jobs",
        content=_managed_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    job_id = created.json()["job_id"]
    scheduler = get_scheduler()
    original_register = scheduler.register_job
    calls = 0

    def _fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("scheduler unavailable")
        return original_register(*args, **kwargs)

    monkeypatch.setattr(scheduler, "register_job", _fail_once)
    failed = await client.post(f"/jobs/{job_id}/pause")

    assert failed.status_code == 503
    stored = await get_job_store().get_job(job_id)
    assert stored.schedule_enabled is True
    assert scheduler._scheduler.get_job(job_id).next_run_time is not None


@pytest.mark.asyncio
async def test_failed_initial_registration_rolls_back_job_creation(
    client,
    monkeypatch,
):
    scheduler = get_scheduler()
    monkeypatch.setattr(
        scheduler,
        "register_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    response = await client.post(
        "/jobs",
        content=_managed_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    assert response.status_code == 503
    jobs = await get_job_store().list_jobs_with_stats(project="integ")
    assert jobs == []
