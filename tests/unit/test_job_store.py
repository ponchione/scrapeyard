"""Tests for SQLiteJobStore CRUD operations."""

from __future__ import annotations

from datetime import datetime

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import init_db
from scrapeyard.storage.job_store import SQLiteJobStore


@pytest.fixture()
async def store(tmp_path):
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


def _make_job(**overrides) -> Job:
    defaults = {
        "job_id": "j-1",
        "project": "acme",
        "name": "scrape-prices",
        "config_yaml": "target: https://example.com",
    }
    defaults.update(overrides)
    return Job(**defaults)


async def test_save_and_get(store):
    job = _make_job()
    returned_id = await store.save_job(job)
    assert returned_id == "j-1"

    fetched = await store.get_job("j-1")
    assert fetched.job_id == "j-1"
    assert fetched.project == "acme"
    assert fetched.name == "scrape-prices"
    assert fetched.status == JobStatus.queued
    assert fetched.run_count == 0


async def test_get_not_found(store):
    with pytest.raises(KeyError, match="Job not found"):
        await store.get_job("no-such-id")


async def test_list_jobs_filters_by_project(store):
    await store.save_job(_make_job(job_id="j-1", project="acme", name="a"))
    await store.save_job(_make_job(job_id="j-2", project="acme", name="b"))
    await store.save_job(_make_job(job_id="j-3", project="other", name="c"))

    acme_jobs = await store.list_jobs("acme")
    assert len(acme_jobs) == 2
    assert {j.job_id for j in acme_jobs} == {"j-1", "j-2"}

    other_jobs = await store.list_jobs("other")
    assert len(other_jobs) == 1


async def test_list_jobs_empty(store):
    jobs = await store.list_jobs("nonexistent")
    assert jobs == []


async def test_delete_job(store):
    await store.save_job(_make_job())
    await store.delete_job("j-1")

    with pytest.raises(KeyError):
        await store.get_job("j-1")


async def test_delete_nonexistent_is_silent(store):
    await store.delete_job("no-such-id")


async def test_update_job(store):
    job = _make_job()
    await store.save_job(job)

    updated = job.model_copy(update={
        "status": JobStatus.running,
        "updated_at": datetime(2026, 1, 1, 12, 0, 0),
        "run_count": 1,
    })
    await store.update_job(updated)

    fetched = await store.get_job("j-1")
    assert fetched.status == JobStatus.running
    assert fetched.run_count == 1
    assert fetched.updated_at == datetime(2026, 1, 1, 12, 0, 0)


async def test_update_nonexistent_raises(store):
    job = _make_job(job_id="ghost")
    with pytest.raises(KeyError, match="Job not found"):
        await store.update_job(job)
