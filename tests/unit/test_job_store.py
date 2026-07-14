"""Tests for SQLiteJobStore CRUD operations."""

from __future__ import annotations

from typing import Any

import pytest

from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import get_db, init_db
from scrapeyard.storage.job_queries import build_list_jobs_with_stats_query
from scrapeyard.storage.job_store import (
    DuplicateJobError,
    SQLiteJobStore,
    is_duplicate_job_integrity_error,
)


@pytest.fixture()
async def store(tmp_path):
    await init_db(str(tmp_path / "db"))
    return SQLiteJobStore()


def _make_job(**overrides: Any) -> Job:
    defaults: dict[str, Any] = {
        "job_id": "j-1",
        "project": "acme",
        "name": "scrape-prices",
        "config_yaml": "target: https://example.com",
    }
    defaults.update(overrides)
    return Job.model_validate(defaults)


def test_is_duplicate_job_integrity_error_matches_sqlite_unique_constraint() -> None:
    assert is_duplicate_job_integrity_error(
        "UNIQUE constraint failed: jobs.project, jobs.name"
    )
    assert is_duplicate_job_integrity_error(
        "sqlite3.IntegrityError: jobs.project, jobs.name"
    )
    assert not is_duplicate_job_integrity_error(
        "UNIQUE constraint failed: jobs.job_id"
    )


async def test_save_job_duplicate_name_raises_duplicate_job_error(store):
    await store.save_job(_make_job(job_id="j-1", project="acme", name="shared"))

    with pytest.raises(DuplicateJobError, match="already exists") as exc_info:
        await store.save_job(_make_job(job_id="j-2", project="acme", name="shared"))

    assert exc_info.value.project == "acme"
    assert exc_info.value.name == "shared"


async def test_save_and_get(store):
    job = _make_job()
    returned_id = await store.save_job(job)
    assert returned_id == "j-1"

    fetched = await store.get_job("j-1")
    assert fetched.job_id == "j-1"
    assert fetched.project == "acme"
    assert fetched.name == "scrape-prices"
    assert fetched.status == JobStatus.queued
    assert fetched.schedule_enabled is True
    assert fetched.current_run_id is None


async def test_get_not_found(store):
    with pytest.raises(KeyError, match="Job not found"):
        await store.get_job("no-such-id")


async def test_rollback_queued_submission(store):
    await store.save_job(_make_job(current_run_id="run-1"))
    assert await store.rollback_queued_submission("j-1", "run-1") is True

    with pytest.raises(KeyError):
        await store.get_job("j-1")


async def test_rollback_queued_submission_nonexistent_is_noop(store):
    assert await store.rollback_queued_submission("no-such-id", "run-1") is False


async def test_result_run_active_lookup_protects_exact_queued_or_running_owner(store):
    await store.save_job(_make_job(current_run_id="run-queued"))

    assert await store.result_run_is_active("acme", "scrape-prices", "run-queued")
    assert not await store.result_run_is_active("acme", "scrape-prices", "other")
    assert not await store.result_run_is_active("other", "scrape-prices", "run-queued")

    async with get_db("jobs.db") as db:
        await db.execute("UPDATE jobs SET status = 'complete' WHERE job_id = 'j-1'")
        await db.commit()
    assert not await store.result_run_is_active(
        "acme", "scrape-prices", "run-queued"
    )


async def test_project_stats_query_uses_summary_without_scanning_runs(store):
    sql, params = build_list_jobs_with_stats_query("acme", limit=10, offset=0)

    async with get_db("jobs.db") as db:
        cursor = await db.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        plan = " ".join(str(row[3]) for row in await cursor.fetchall())

    assert "idx_jobs_project" in plan
    assert "job_runs" not in sql


async def test_save_and_get_preserves_disabled_schedule(store):
    job = _make_job(schedule_cron="*/5 * * * *", schedule_enabled=False)
    await store.save_job(job)

    fetched = await store.get_job("j-1")
    assert fetched.schedule_cron == "*/5 * * * *"
    assert fetched.schedule_enabled is False
