"""FastAPI application entry point."""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scrapeyard import __version__
from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_worker_pool,
    init_rate_limiter,
)
from scrapeyard.api.routes import router
from scrapeyard.common.logging import setup_logging
from scrapeyard.common.settings import get_settings
from scrapeyard.storage.cleanup import start_cleanup_loop
from scrapeyard.storage.database import close_db, get_db, init_db

_start_time: float = 0.0
_projects_cache: dict[str, dict] = {}
_projects_cache_refreshed_at: float = 0.0
_PROJECTS_CACHE_TTL_SECONDS = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Orchestrate startup and shutdown in dependency order."""
    global _start_time  # noqa: PLW0603
    _start_time = time.monotonic()

    # 1. Settings & logging
    settings = get_settings()
    setup_logging(settings.log_dir)

    # 2. Database
    await init_db(settings.db_dir)

    # 3. Storage instances
    app.state.job_store = get_job_store()
    app.state.error_store = get_error_store()
    app.state.result_store = get_result_store()

    # 4. Worker pool
    pool = get_worker_pool()
    app.state.worker_pool = pool
    await pool.start()

    # Initialize rate limiter with the pool's Redis connection for cross-job
    # domain rate limiting. Falls back to local when Redis is unavailable.
    init_rate_limiter(redis=getattr(pool, "_redis", None))

    # 5. Scheduler (re-registers persisted jobs on start)
    scheduler = get_scheduler()
    app.state.scheduler = scheduler
    await scheduler.start()

    # 6. Cleanup loop
    app.state.cleanup_task = start_cleanup_loop(app.state.result_store)

    yield

    # Shutdown (reverse order)
    app.state.cleanup_task.cancel()
    try:
        await app.state.cleanup_task
    except asyncio.CancelledError:
        pass
    scheduler.shutdown()
    await pool.stop()
    await close_db()


app = FastAPI(
    title="Scrapeyard",
    description="Config-driven web scraping microservice",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(router)


async def _project_health_summary() -> dict[str, dict]:
    global _projects_cache_refreshed_at  # noqa: PLW0603
    global _projects_cache  # noqa: PLW0603
    now = time.monotonic()
    if now - _projects_cache_refreshed_at < _PROJECTS_CACHE_TTL_SECONDS:
        return _projects_cache

    rows: list[tuple[str, str, int]] = []
    try:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT project, status, COUNT(*) FROM jobs GROUP BY project, status"
            )
            rows = await cursor.fetchall()
    except RuntimeError:
        rows = []

    summary: dict[str, dict] = {}
    for project, status, count in rows:
        project_entry = summary.setdefault(
            project,
            {
                "job_count": 0,
                "status": "healthy",
                "status_counts": {
                    "queued": 0,
                    "running": 0,
                    "complete": 0,
                    "partial": 0,
                    "failed": 0,
                },
            },
        )
        project_entry["job_count"] += count
        if status in project_entry["status_counts"]:
            project_entry["status_counts"][status] += count

    for project_entry in summary.values():
        counts = project_entry["status_counts"]
        if counts["failed"] > 0:
            project_entry["status"] = "failing"
        elif counts["partial"] > 0 or counts["running"] > 0:
            project_entry["status"] = "degraded"
        else:
            project_entry["status"] = "healthy"

    _projects_cache = summary
    _projects_cache_refreshed_at = now
    return summary


@app.get("/health")
async def health() -> dict:
    """Service health check endpoint with detailed status."""
    pool = get_worker_pool()

    uptime = time.monotonic() - _start_time if _start_time else 0.0
    projects = await _project_health_summary()

    # Determine overall status based on active task load.
    status = "ok"
    if pool.active_tasks >= pool.max_concurrent:
        status = "degraded"

    return {
        "status": status,
        "uptime_seconds": round(uptime, 1),
        "workers": {
            "max_concurrent": pool.max_concurrent,
            "active_tasks": pool.active_tasks,
            "max_browsers": pool.max_browsers,
            "active_browsers": pool.active_browsers,
        },
        "projects": projects,
    }
