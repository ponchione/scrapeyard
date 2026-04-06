"""FastAPI application entry point."""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scrapeyard import __version__
from scrapeyard.api.dependencies import (
    close_webhook_dispatcher,
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_webhook_dispatcher,
    get_worker_pool,
    init_rate_limiter,
)
from scrapeyard.api.routes import router
from scrapeyard.common.logging import setup_logging
from scrapeyard.common.settings import get_settings
from scrapeyard.storage.cleanup import start_cleanup_loop
from scrapeyard.storage.database import close_db, init_db


class HealthCache:
    """Encapsulates health-endpoint state: uptime tracking and project summary cache."""

    def __init__(self, cache_ttl_seconds: float = 5.0) -> None:
        self.start_time: float = 0.0
        self._projects_cache: dict[str, dict] = {}
        self._projects_cache_refreshed_at: float = 0.0
        self._cache_ttl = cache_ttl_seconds

    def mark_started(self) -> None:
        self.start_time = time.monotonic()

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.start_time if self.start_time else 0.0

    async def project_summary(self) -> dict[str, dict]:
        now = time.monotonic()
        if now - self._projects_cache_refreshed_at < self._cache_ttl:
            return self._projects_cache

        rows: list[tuple[str, str, int]] = []
        try:
            rows = await get_job_store().summary_by_project()
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

        self._projects_cache = summary
        self._projects_cache_refreshed_at = now
        return summary


_health = HealthCache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Orchestrate startup and shutdown in dependency order."""
    _health.mark_started()

    # 1. Settings & logging
    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level)

    # 2. Database
    await init_db(settings.db_dir)

    # 3. Storage instances
    app.state.job_store = get_job_store()
    app.state.error_store = get_error_store()
    app.state.result_store = get_result_store()
    app.state.webhook_dispatcher = get_webhook_dispatcher()
    await app.state.webhook_dispatcher.startup()

    # 4. Worker pool
    pool = get_worker_pool()
    app.state.worker_pool = pool
    await pool.start()

    # Initialize rate limiter with the pool's Redis connection for cross-job
    # domain rate limiting. Falls back to local when Redis is unavailable.
    init_rate_limiter(redis=pool.redis)

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
    await close_webhook_dispatcher(timeout=settings.workers_shutdown_grace_seconds)
    await close_db()


app = FastAPI(
    title="Scrapeyard",
    description="Config-driven web scraping microservice",
    version=__version__,
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health")
async def health() -> dict:
    """Service health check endpoint with detailed status."""
    pool = get_worker_pool()

    uptime = _health.uptime
    projects = await _health.project_summary()

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
