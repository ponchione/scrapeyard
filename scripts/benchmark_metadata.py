"""Compare SQLite layouts using real stores and in-process GET /jobs requests.

Scratch databases only. This measures file/connection consolidation with existing
transaction boundaries, not a migration or atomic result-publication redesign.
Run: poetry run python scripts/benchmark_metadata.py
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import sqlite3
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from statistics import median, quantiles
from tempfile import TemporaryDirectory
from unittest.mock import patch

import aiosqlite
import httpx

from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    reset_cached_dependencies,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.config.loader import load_config
from scrapeyard.models.job import ActionTaken, ErrorRecord, ErrorType, Job, JobStatus
from scrapeyard.storage import database
from scrapeyard.webhook.payload import build_terminal_webhook_delivery


LAYOUTS = ("separate", "shared_connection", "shared_file")
CONFIG = """project: benchmark
name: benchmark
target:
  url: https://example.com
  selectors:
    title: h1
webhook:
  url: https://example.com/hook
  on: [complete]
"""
OPERATION = ContextVar("operation", default="setup")


def p95(values):
    return round(quantiles(values, n=100, method="inclusive")[94], 3)


async def measure(args, layout, root):
    manager = database.DatabaseManager()
    await manager.init(str(root / "db"))
    try:
        if layout != "separate":
            # Fresh scratch tables only: deliberately NOT a legacy migration.
            migrations = database._load_migrations(database._resolve_sql_dir())
            async with manager.get("jobs.db") as db:
                for name in ("errors.db", "results_meta.db"):
                    for migration in migrations[name]:
                        await database._apply_migration(db, migration)
            if layout == "shared_file":
                for name in ("errors.db", "results_meta.db"):
                    db = await aiosqlite.connect(root / "db" / "jobs.db")
                    manager._connections[name] = db
                    db.row_factory = aiosqlite.Row
                    await database._apply_connection_pragmas(db)

        waits = {key: [] for key in ("reader", "writer")}
        holds = {key: [] for key in waits}
        original_get = manager.get

        @asynccontextmanager
        async def measured_get(name):
            started = time.perf_counter()
            async with original_get("jobs.db" if layout == "shared_connection" else name) as db:
                acquired = time.perf_counter()
                operation = OPERATION.get()
                if operation in waits:
                    waits[operation].append((acquired - started) * 1000)
                try:
                    yield db
                finally:
                    if operation in holds:
                        holds[operation].append((time.perf_counter() - acquired) * 1000)

        with patch.object(database, "_default_manager", manager), patch.object(
            manager, "get", measured_get,
        ):
            return await workload(args, layout, waits, holds)
    finally:
        await manager.close()


async def workload(args, layout, waits, holds):
    # Import after the scratch settings are installed; lifespan is not started.
    from scrapeyard.main import app

    jobs, errors, results = get_job_store(), get_error_store(), get_result_store()
    config = load_config(CONFIG)
    for index in range(args.history):
        await jobs.save_job(Job(
            job_id=f"history-{index}", project="benchmark", name=f"history-{index}",
            status=JobStatus.complete, config_yaml=CONFIG,
        ))

    writer_slots, reader_slots = asyncio.Semaphore(4), asyncio.Semaphore(16)
    latencies = []
    completed_ids = []

    async def write(index):
        async with writer_slots:
            OPERATION.set("writer")
            job_id, run_id = f"job-{index}", f"run-{index}"
            started = utc_now()
            await jobs.save_job(Job(
                job_id=job_id, project="benchmark", name=job_id, config_yaml=CONFIG,
                current_run_id=run_id, updated_at=started,
            ))
            assert await jobs.claim_run(
                run_id, job_id, "adhoc", hashlib.sha256(CONFIG.encode()).hexdigest(), started,
            )
            await errors.log_error(ErrorRecord(
                job_id=job_id, run_id=run_id, project="benchmark",
                target_url="https://example.com", attempt=1,
                error_type=ErrorType.network_error, fetcher_used="basic",
                action_taken=ActionTaken.retry,
            ))
            saved = await results.save_result(
                job_id, {"results": [{"title": "recovered"}]},
                run_id=run_id, record_count=1,
            )
            error_count = await errors.count_errors_for_run(run_id)
            finished = utc_now()
            await jobs.finalize_owned_run(
                job_id, run_id, "complete", 1, error_count, finished,
                started - timedelta(seconds=1),
                webhook_delivery=build_terminal_webhook_delivery(
                    config=config, job_id=job_id, run_id=run_id, status=JobStatus.complete,
                    result_path=saved.file_path, result_count=1, error_count=error_count,
                    started_at=started, completed_at=finished,
                ),
            )
            assert error_count == 1
            assert Path(saved.file_path, "results.json").is_file()
            completed_ids.append(job_id)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://benchmark",
        headers={"X-API-Key": "metadata-benchmark-key-0000"},
    ) as client:
        assert (await client.get("/jobs?limit=100")).status_code == 200

        async def read():
            async with reader_slots:
                OPERATION.set("reader")
                started = time.perf_counter()
                response = await client.get("/jobs?limit=100")
                latencies.append((time.perf_counter() - started) * 1000)
                assert response.status_code == 200, response.text
                assert len(response.json()) == 100

        started = time.perf_counter()
        for batch in range(args.batches):
            tasks = [asyncio.create_task(write(batch * 12 + index)) for index in range(12)]
            tasks.extend(asyncio.create_task(read()) for _ in range(80))
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.perf_counter() - started

    # Verify committed counts, readable encrypted configs, and every artifact.
    expected = args.batches * 12
    for name, table, count in (
        ("jobs.db", "jobs", args.history + expected),
        ("jobs.db", "job_runs", expected),
        ("jobs.db", "webhook_deliveries", expected),
        ("errors.db", "errors", expected),
        ("results_meta.db", "results_meta", expected),
    ):
        async with database.get_db(name) as db:
            row = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            assert row[0] == count, (layout, table, row[0], count)
            integrity = await (await db.execute("PRAGMA integrity_check")).fetchall()
            assert [row[0] for row in integrity] == ["ok"]
    for job_id in completed_ids:
        job = await jobs.get_job(job_id)
        assert job.status == JobStatus.complete and job.config_yaml == CONFIG
        result = await results.get_result(job_id)
        assert result.status == "complete"
        assert result.data == {"results": [{"title": "recovered"}]}

    return {
        "layout": layout,
        "jobs": expected,
        "reads": len(latencies),
        "elapsed_seconds": round(elapsed, 3),
        "jobs_per_second": round(expected / elapsed, 2),
        "api_p50_ms": round(median(latencies), 3),
        "api_p95_ms": p95(latencies),
        "connection_wait_p95_ms": {
            key: p95(values) for key, values in waits.items()
        },
        "connection_held_p95_ms": {
            key: p95(values) for key, values in holds.items()
        },
    }


async def benchmark(args):
    print(json.dumps({
        "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(), "history": args.history,
        "batches": args.batches, "repeats": args.repeats,
        "workers": 4, "readers": 16,
    }), flush=True)
    for repeat in range(args.repeats):
        # Rotate order to avoid always giving the same layout a cold host.
        offset = repeat % len(LAYOUTS)
        for layout in LAYOUTS[offset:] + LAYOUTS[:offset]:
            with TemporaryDirectory(prefix="scrapeyard-metadata-") as directory:
                root = Path(directory)
                with patch.dict(os.environ, {
                    "SCRAPEYARD_DB_DIR": str(root / "db"),
                    "SCRAPEYARD_STORAGE_RESULTS_DIR": str(root / "results"),
                    "SCRAPEYARD_ADAPTIVE_DIR": str(root / "adaptive"),
                    "SCRAPEYARD_LOG_DIR": str(root / "logs"),
                    "SCRAPEYARD_API_KEYS": "",
                    "SCRAPEYARD_API_CREDENTIALS": json.dumps({"benchmark": {
                        "secret": "metadata-benchmark-key-0000", "scopes": ["read"],
                    }}),
                    "SCRAPEYARD_RATE_LIMIT_REQUESTS": str(
                        args.repeats * len(LAYOUTS) * (args.batches * 80 + 1)
                    ),
                    "SCRAPEYARD_ENCRYPTION_KEYS": '{"benchmark":"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="}',
                    "SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID": "benchmark",
                }):
                    get_settings.cache_clear()
                    reset_cached_dependencies()
                    try:
                        result = await measure(args, layout, root)
                        print(json.dumps({"repeat": repeat + 1, **result}), flush=True)
                    finally:
                        reset_cached_dependencies()
                        get_settings.cache_clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=int, default=1000)
    parser.add_argument("--batches", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.history < 100 or args.batches < 1 or args.repeats < 1:
        parser.error("history must be >= 100; batches and repeats must be positive")
    logging.disable(logging.INFO)
    asyncio.run(benchmark(args))
