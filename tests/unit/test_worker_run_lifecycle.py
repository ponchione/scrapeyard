"""Tests for worker run lifecycle: creation, finalization, crash, and supersession."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import (
    ErrorRecord,
    ErrorType,
    Job,
    JobStatus,
)
from scrapeyard.queue.worker import _run_superseded, scrape_task
from scrapeyard.storage.database import init_db, reset_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(
    job_id: str = "job-1",
    status: JobStatus = JobStatus.queued,
    current_run_id: str | None = None,
) -> Job:
    return Job(
        job_id=job_id,
        project="test",
        name="lifecycle-test",
        config_yaml="",
        status=status,
        current_run_id=current_run_id,
    )


def _make_target(url: str = "http://example.com") -> MagicMock:
    target = MagicMock(url=url, proxy=None)
    target.fetcher.value = "basic"
    return target


_SIMPLE_YAML = "project: test\nname: x\ntarget:\n  url: http://x\n  selectors:\n    t: h1"


def _patch_config(cfg: MagicMock) -> MagicMock:
    """Apply common config mock defaults."""
    cfg.project = "test"
    cfg.name = "lifecycle-test"
    cfg.resolved_targets.return_value = [_make_target()]
    cfg.execution.concurrency = 1
    cfg.execution.delay_between = 0
    cfg.execution.domain_rate_limit = 0
    cfg.execution.fail_strategy = MagicMock(value="partial")
    cfg.adaptive = False
    cfg.schedule = None
    cfg.retry = MagicMock()
    cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
    cfg.output.group_by = "target"
    cfg.webhook = None
    cfg.proxy = None
    return cfg


# ---------------------------------------------------------------------------
# _run_superseded — pure function tests
# ---------------------------------------------------------------------------


class TestRunSuperseded:
    def test_returns_false_when_run_id_is_none(self):
        job = _make_job(current_run_id="run-1")
        assert _run_superseded(job, None) is False

    def test_returns_false_when_ids_match(self):
        job = _make_job(current_run_id="run-1")
        assert _run_superseded(job, "run-1") is False

    def test_returns_true_when_ids_differ(self):
        job = _make_job(current_run_id="run-1")
        assert _run_superseded(job, "run-2") is True

    def test_returns_true_when_job_has_no_current_run(self):
        """job.current_run_id is None but run_id is set -> superseded."""
        job = _make_job(current_run_id=None)
        assert _run_superseded(job, "run-1") is True


# ---------------------------------------------------------------------------
# Run creation — verifies INSERT into job_runs
# ---------------------------------------------------------------------------


class TestRunCreation:
    @pytest.mark.asyncio
    async def test_run_id_present_inserts_job_runs_row(self, tmp_path):
        """When run_id is provided, a job_runs row is inserted with status='running'."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job(current_run_id="run-abc")
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        success_result = TargetResult(
            url="http://example.com", status="success", data=[{"title": "A"}],
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=success_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-abc",
                trigger="scheduled",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        # Read back the row from the real DB.
        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute(
                "SELECT run_id, job_id, status, trigger, config_hash "
                "FROM job_runs WHERE run_id = ?",
                ("run-abc",),
            )
            row = await cursor.fetchone()

        assert row is not None
        run_id, job_id, status, trigger, config_hash = row
        assert run_id == "run-abc"
        assert job_id == "job-1"
        assert status in ("running", "complete")  # May be finalized already.
        assert trigger == "scheduled"
        expected_hash = hashlib.sha256(_SIMPLE_YAML.encode()).hexdigest()
        assert config_hash == expected_hash

        reset_db()

    @pytest.mark.asyncio
    async def test_config_hash_is_sha256_of_yaml(self, tmp_path):
        """config_hash stored in job_runs is SHA-256 of the config_yaml string."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        yaml_text = "project: test\nname: hash-check\ntarget:\n  url: http://x\n  selectors:\n    t: h1"
        expected_hash = hashlib.sha256(yaml_text.encode()).hexdigest()

        job = _make_job(current_run_id="run-hash")
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        success_result = TargetResult(
            url="http://example.com", status="success", data=[{"title": "A"}],
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=success_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", yaml_text,
                run_id="run-hash",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute(
                "SELECT config_hash FROM job_runs WHERE run_id = ?",
                ("run-hash",),
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == expected_hash

        reset_db()

    @pytest.mark.asyncio
    async def test_no_run_id_skips_insert(self, tmp_path):
        """When run_id is None, no job_runs row is created."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job()
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        success_result = TargetResult(
            url="http://example.com", status="success", data=[{"title": "A"}],
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=success_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id=None,
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute("SELECT COUNT(*) FROM job_runs")
            count = (await cursor.fetchone())[0]

        assert count == 0

        reset_db()


# ---------------------------------------------------------------------------
# Run finalization — verifies UPDATE of job_runs after success
# ---------------------------------------------------------------------------


class TestRunFinalization:
    @pytest.mark.asyncio
    async def test_successful_run_updates_status_and_counts(self, tmp_path):
        """After a successful scrape, job_runs is updated with final status, counts."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job(current_run_id="run-fin")
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        success_result = TargetResult(
            url="http://example.com", status="success",
            data=[{"title": "A"}, {"title": "B"}, {"title": "C"}],
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=success_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-fin",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute(
                "SELECT status, completed_at, record_count, error_count "
                "FROM job_runs WHERE run_id = ?",
                ("run-fin",),
            )
            row = await cursor.fetchone()

        assert row is not None
        status, completed_at, record_count, error_count = row
        assert status == "complete"
        assert completed_at is not None
        assert record_count == 3
        assert error_count == 0

        reset_db()

    @pytest.mark.asyncio
    async def test_finalization_counts_errors_from_error_db(self, tmp_path):
        """error_count in job_runs is queried from errors.db for the run_id."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        # Pre-insert error rows into errors.db for this run.
        async with aiosqlite.connect(tmp_path / "db" / "errors.db") as db:
            for i in range(3):
                await db.execute(
                    "INSERT INTO errors "
                    "(job_id, run_id, project, target_url, attempt, timestamp, "
                    "error_type, fetcher_used, action_taken, resolved) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "job-1", "run-err", "test", "http://x",
                        1, "2024-01-01T00:00:00", "http_error",
                        "basic", "fail", 0,
                    ),
                )
            await db.commit()

        job = _make_job(current_run_id="run-err")
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        # A success result so the job completes normally.
        success_result = TargetResult(
            url="http://example.com", status="success",
            data=[{"title": "A"}],
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=success_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-err",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute(
                "SELECT error_count FROM job_runs WHERE run_id = ?",
                ("run-err",),
            )
            row = await cursor.fetchone()

        assert row is not None
        assert row[0] == 3

        reset_db()

    @pytest.mark.asyncio
    async def test_failed_run_finalized_with_failed_status(self, tmp_path):
        """When all targets fail, the run row should be updated to 'failed'."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job(current_run_id="run-fail")
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        fail_result = TargetResult(
            url="http://example.com", status="failed",
            data=[], errors=["timeout"],
            error_type=ErrorType.timeout,
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=fail_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-fail",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute(
                "SELECT status, completed_at, record_count "
                "FROM job_runs WHERE run_id = ?",
                ("run-fail",),
            )
            row = await cursor.fetchone()

        assert row is not None
        status, completed_at, record_count = row
        assert status == "failed"
        assert completed_at is not None
        assert record_count == 0

        reset_db()

    @pytest.mark.asyncio
    async def test_no_finalization_when_run_id_is_none(self, tmp_path):
        """When run_id is None, no finalization UPDATE happens (no rows to update)."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job()
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        success_result = TargetResult(
            url="http://example.com", status="success",
            data=[{"title": "A"}],
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=success_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id=None,
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        # No rows should exist at all.
        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute("SELECT COUNT(*) FROM job_runs")
            count = (await cursor.fetchone())[0]

        assert count == 0

        reset_db()


# ---------------------------------------------------------------------------
# Run crash handling — verifies crash recovery marks run as failed
# ---------------------------------------------------------------------------


class TestRunCrashHandling:
    @pytest.mark.asyncio
    async def test_crash_marks_run_failed(self, tmp_path):
        """On exception, run_id row is updated to status='failed' with completed_at."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        # Pre-insert a running row (simulating the creation step completing
        # before the crash occurs after it).
        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            await db.execute(
                "INSERT INTO job_runs "
                "(run_id, job_id, status, trigger, config_hash, started_at) "
                "VALUES (?, ?, 'running', 'adhoc', 'abc', '2024-01-01T00:00:00')",
                ("run-crash", "job-1"),
            )
            await db.commit()

        job = _make_job()
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        # Make load_config raise so we hit the crash handler.
        with patch("scrapeyard.queue.worker.load_config", side_effect=RuntimeError("boom")), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-crash",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute(
                "SELECT status, completed_at FROM job_runs WHERE run_id = ?",
                ("run-crash",),
            )
            row = await cursor.fetchone()

        assert row is not None
        status, completed_at = row
        assert status == "failed"
        assert completed_at is not None

        reset_db()

    @pytest.mark.asyncio
    async def test_crash_does_not_overwrite_already_finalized_run(self, tmp_path):
        """The AND status='running' guard prevents overwriting a completed run."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        # Pre-insert a row that is already 'complete' — the crash handler
        # should NOT overwrite it.
        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            await db.execute(
                "INSERT INTO job_runs "
                "(run_id, job_id, status, trigger, config_hash, started_at, "
                "completed_at, record_count, error_count) "
                "VALUES (?, ?, 'complete', 'adhoc', 'abc', "
                "'2024-01-01T00:00:00', '2024-01-01T00:01:00', 5, 0)",
                ("run-no-overwrite", "job-1"),
            )
            await db.commit()

        job = _make_job()
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        with patch("scrapeyard.queue.worker.load_config", side_effect=RuntimeError("boom")), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-no-overwrite",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute(
                "SELECT status, record_count FROM job_runs WHERE run_id = ?",
                ("run-no-overwrite",),
            )
            row = await cursor.fetchone()

        assert row is not None
        status, record_count = row
        assert status == "complete"
        assert record_count == 5

        reset_db()

    @pytest.mark.asyncio
    async def test_crash_no_run_id_skips_db_update(self, tmp_path):
        """When run_id is None, crash handler does not attempt any DB update."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job()
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        with patch("scrapeyard.queue.worker.load_config", side_effect=RuntimeError("boom")), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id=None,
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        # No rows should exist in job_runs.
        async with aiosqlite.connect(tmp_path / "db" / "jobs.db") as db:
            cursor = await db.execute("SELECT COUNT(*) FROM job_runs")
            count = (await cursor.fetchone())[0]

        assert count == 0

        reset_db()

    @pytest.mark.asyncio
    async def test_crash_db_failure_does_not_reraise(self, tmp_path):
        """If the crash-handler DB update itself fails, the error is logged, not raised."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)
        # Reset DB so get_db raises RuntimeError("Database not initialised").
        reset_db()

        job = _make_job()
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        # load_config raises to trigger the crash handler, then get_db will
        # also fail because we reset_db. The task must not raise.
        with patch("scrapeyard.queue.worker.load_config", side_effect=RuntimeError("boom")), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )

            # Should not raise.
            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-db-fail",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=AsyncMock(),
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )


# ---------------------------------------------------------------------------
# Error logging with run_id
# ---------------------------------------------------------------------------


class TestErrorLoggingWithRunId:
    @pytest.mark.asyncio
    async def test_errors_tagged_with_correct_run_id(self, tmp_path):
        """_log_error creates ErrorRecord with the run_id from the task."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job(current_run_id="run-err-tag")
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        error_store = AsyncMock()
        logged_errors: list[ErrorRecord] = []

        async def capture_error(record: ErrorRecord) -> None:
            logged_errors.append(record)

        error_store.log_error.side_effect = capture_error

        fail_result = TargetResult(
            url="http://example.com", status="failed",
            data=[], errors=["connection refused"],
            error_type=ErrorType.network_error,
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=fail_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id="run-err-tag",
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=error_store,
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        assert len(logged_errors) > 0
        for record in logged_errors:
            assert record.run_id == "run-err-tag"
            assert record.job_id == "job-1"

        reset_db()

    @pytest.mark.asyncio
    async def test_errors_use_empty_string_when_run_id_none(self, tmp_path):
        """When run_id is None, errors use empty string as run_id."""
        db_dir = str(tmp_path / "db")
        await init_db(db_dir)

        job = _make_job()
        job_store = AsyncMock()
        job_store.get_job.return_value = job
        job_store.update_job = AsyncMock()

        error_store = AsyncMock()
        logged_errors: list[ErrorRecord] = []

        async def capture_error(record: ErrorRecord) -> None:
            logged_errors.append(record)

        error_store.log_error.side_effect = capture_error

        fail_result = TargetResult(
            url="http://example.com", status="failed",
            data=[], errors=["timeout"],
            error_type=ErrorType.timeout,
        )

        with patch("scrapeyard.queue.worker.load_config") as mock_load, \
             patch("scrapeyard.queue.worker.scrape_target", return_value=fail_result), \
             patch("scrapeyard.queue.worker.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                adaptive_dir=str(tmp_path / "adaptive"),
                workers_running_lease_seconds=300,
                proxy_url="",
            )
            _patch_config(mock_load.return_value)

            await scrape_task(
                "job-1", _SIMPLE_YAML,
                run_id=None,
                job_store=job_store,
                result_store=AsyncMock(),
                error_store=error_store,
                circuit_breaker=MagicMock(),
                rate_limiter=LocalDomainRateLimiter(),
            )

        assert len(logged_errors) > 0
        for record in logged_errors:
            assert record.run_id == ""

        reset_db()
