"""Tests for SQLiteJobStore run-related query methods."""

from __future__ import annotations

from datetime import datetime

import pytest

from scrapeyard.models.job import Job, JobRun, JobStatus
from scrapeyard.storage.database import get_db, init_db
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


async def _insert_run(
    run_id: str,
    job_id: str,
    status: str,
    trigger: str,
    config_hash: str,
    started_at: str,
    completed_at: str | None = None,
    record_count: int | None = None,
    error_count: int = 0,
) -> None:
    async with get_db("jobs.db") as db:
        await db.execute(
            "INSERT INTO job_runs "
            "(run_id, job_id, status, trigger, config_hash, "
            "started_at, completed_at, record_count, error_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, job_id, status, trigger, config_hash,
                started_at, completed_at, record_count, error_count,
            ),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# get_job_runs
# ---------------------------------------------------------------------------


async def test_get_job_runs_empty(store):
    await store.save_job(_make_job())
    runs = await store.get_job_runs("j-1")
    assert runs == []


async def test_get_job_runs_returns_only_target_job(store):
    await store.save_job(_make_job(job_id="j-1", name="a"))
    await store.save_job(_make_job(job_id="j-2", name="b"))

    await _insert_run("r-1", "j-1", "complete", "adhoc", "aaa", "2026-03-01T10:00:00")
    await _insert_run("r-2", "j-2", "complete", "adhoc", "bbb", "2026-03-01T11:00:00")

    runs = await store.get_job_runs("j-1")
    assert len(runs) == 1
    assert runs[0].run_id == "r-1"
    assert runs[0].job_id == "j-1"


async def test_get_job_runs_ordered_newest_first(store):
    await store.save_job(_make_job())

    await _insert_run("r-old", "j-1", "complete", "scheduled", "aaa", "2026-03-01T08:00:00")
    await _insert_run("r-mid", "j-1", "complete", "scheduled", "aaa", "2026-03-01T12:00:00")
    await _insert_run("r-new", "j-1", "complete", "scheduled", "aaa", "2026-03-01T18:00:00")

    runs = await store.get_job_runs("j-1")
    assert [r.run_id for r in runs] == ["r-new", "r-mid", "r-old"]


async def test_get_job_runs_respects_limit(store):
    await store.save_job(_make_job())

    for i in range(5):
        await _insert_run(
            f"r-{i}", "j-1", "complete", "adhoc", "aaa",
            f"2026-03-01T{10 + i:02d}:00:00",
        )

    runs = await store.get_job_runs("j-1", limit=2)
    assert len(runs) == 2
    # Newest first — r-4 (14:00) then r-3 (13:00)
    assert runs[0].run_id == "r-4"
    assert runs[1].run_id == "r-3"


async def test_get_job_runs_returns_job_run_objects(store):
    await store.save_job(_make_job())
    await _insert_run(
        "r-1", "j-1", "complete", "adhoc", "abc123",
        "2026-03-10T09:00:00", "2026-03-10T09:05:00", 42, 3,
    )

    runs = await store.get_job_runs("j-1")
    assert len(runs) == 1

    run = runs[0]
    assert isinstance(run, JobRun)
    assert run.run_id == "r-1"
    assert run.job_id == "j-1"
    assert run.status == JobStatus.complete
    assert run.trigger == "adhoc"
    assert run.config_hash == "abc123"
    assert run.started_at == datetime(2026, 3, 10, 9, 0, 0)
    assert run.completed_at == datetime(2026, 3, 10, 9, 5, 0)
    assert run.record_count == 42
    assert run.error_count == 3


# ---------------------------------------------------------------------------
# get_job_run_stats
# ---------------------------------------------------------------------------


async def test_get_job_run_stats_no_runs(store):
    await store.save_job(_make_job())
    count, last = await store.get_job_run_stats("j-1")
    assert count == 0
    assert last is None


async def test_get_job_run_stats_correct_count_and_last(store):
    await store.save_job(_make_job())

    await _insert_run("r-1", "j-1", "complete", "adhoc", "aaa", "2026-03-01T10:00:00")
    await _insert_run("r-2", "j-1", "complete", "adhoc", "aaa", "2026-03-05T14:00:00")
    await _insert_run("r-3", "j-1", "failed", "scheduled", "aaa", "2026-03-10T08:00:00")

    count, last = await store.get_job_run_stats("j-1")
    assert count == 3
    assert last == datetime(2026, 3, 10, 8, 0, 0)


async def test_get_job_run_stats_only_counts_target_job(store):
    await store.save_job(_make_job(job_id="j-1", name="a"))
    await store.save_job(_make_job(job_id="j-2", name="b"))

    await _insert_run("r-1", "j-1", "complete", "adhoc", "aaa", "2026-03-01T10:00:00")
    await _insert_run("r-2", "j-1", "complete", "adhoc", "aaa", "2026-03-02T10:00:00")
    await _insert_run("r-3", "j-2", "complete", "adhoc", "bbb", "2026-03-03T10:00:00")

    count, last = await store.get_job_run_stats("j-1")
    assert count == 2
    assert last == datetime(2026, 3, 2, 10, 0, 0)


# ---------------------------------------------------------------------------
# list_jobs_with_stats
# ---------------------------------------------------------------------------


async def test_list_jobs_with_stats_all_jobs(store):
    await store.save_job(_make_job(job_id="j-1", project="acme", name="a"))
    await store.save_job(_make_job(job_id="j-2", project="other", name="b"))

    results = await store.list_jobs_with_stats()
    assert len(results) == 2
    ids = {r[0].job_id for r in results}
    assert ids == {"j-1", "j-2"}


async def test_list_jobs_with_stats_filters_by_project(store):
    await store.save_job(_make_job(job_id="j-1", project="acme", name="a"))
    await store.save_job(_make_job(job_id="j-2", project="acme", name="b"))
    await store.save_job(_make_job(job_id="j-3", project="other", name="c"))

    results = await store.list_jobs_with_stats(project="acme")
    assert len(results) == 2
    assert {r[0].job_id for r in results} == {"j-1", "j-2"}


async def test_list_jobs_with_stats_returns_tuples(store):
    await store.save_job(_make_job(job_id="j-1", name="a"))
    await _insert_run("r-1", "j-1", "complete", "adhoc", "aaa", "2026-03-01T10:00:00")

    results = await store.list_jobs_with_stats()
    assert len(results) == 1

    job, run_count, last_run_at = results[0]
    assert isinstance(job, Job)
    assert job.job_id == "j-1"
    assert run_count == 1
    assert last_run_at == datetime(2026, 3, 1, 10, 0, 0)


async def test_list_jobs_with_stats_no_runs(store):
    await store.save_job(_make_job(job_id="j-1", name="a"))

    results = await store.list_jobs_with_stats()
    assert len(results) == 1

    job, run_count, last_run_at = results[0]
    assert job.job_id == "j-1"
    assert run_count == 0
    assert last_run_at is None


async def test_list_jobs_with_stats_aggregated(store):
    await store.save_job(_make_job(job_id="j-1", name="a"))
    await store.save_job(_make_job(job_id="j-2", name="b"))

    await _insert_run("r-1", "j-1", "complete", "adhoc", "aaa", "2026-03-01T10:00:00")
    await _insert_run("r-2", "j-1", "complete", "adhoc", "aaa", "2026-03-05T15:00:00")
    await _insert_run("r-3", "j-2", "complete", "adhoc", "bbb", "2026-03-03T12:00:00")

    results = await store.list_jobs_with_stats()
    by_id = {r[0].job_id: (r[1], r[2]) for r in results}

    assert by_id["j-1"] == (2, datetime(2026, 3, 5, 15, 0, 0))
    assert by_id["j-2"] == (1, datetime(2026, 3, 3, 12, 0, 0))


async def test_list_jobs_with_stats_orders_by_latest_activity_desc(store):
    await store.save_job(
        _make_job(
            job_id="j-1",
            name="run-newest",
            created_at=datetime(2026, 3, 1, 8, 0, 0),
        )
    )
    await store.save_job(
        _make_job(
            job_id="j-2",
            name="updated-no-run",
            created_at=datetime(2026, 3, 1, 7, 0, 0),
            updated_at=datetime(2026, 3, 5, 9, 0, 0),
        )
    )
    await store.save_job(
        _make_job(
            job_id="j-3",
            name="run-older",
            created_at=datetime(2026, 3, 1, 6, 0, 0),
        )
    )

    await _insert_run("r-1", "j-1", "complete", "adhoc", "aaa", "2026-03-06T12:00:00")
    await _insert_run("r-2", "j-3", "complete", "adhoc", "bbb", "2026-03-04T12:00:00")

    results = await store.list_jobs_with_stats()

    assert [job.job_id for job, _, _ in results] == ["j-1", "j-2", "j-3"]


async def test_list_jobs_with_stats_respects_limit_and_offset(store):
    for i in range(4):
        await store.save_job(
            _make_job(
                job_id=f"j-{i}",
                name=f"job-{i}",
                created_at=datetime(2026, 3, 1, 8 + i, 0, 0),
            )
        )

    results = await store.list_jobs_with_stats(limit=2, offset=1)

    assert [job.job_id for job, _, _ in results] == ["j-2", "j-1"]
