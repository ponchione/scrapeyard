"""FastAPI application entry point."""

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.api.routes import router
from scrapeyard.common.logging import setup_logging
from scrapeyard.common.settings import get_settings
from scrapeyard.storage.cleanup import start_cleanup_loop
from scrapeyard.storage.database import init_db, reset_db

_start_time: float = 0.0


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

    # 5. Scheduler (re-registers persisted jobs on start)
    scheduler = get_scheduler()
    app.state.scheduler = scheduler
    await scheduler.start()

    # 6. Cleanup loop
    app.state.cleanup_task = start_cleanup_loop()

    yield

    # Shutdown (reverse order)
    app.state.cleanup_task.cancel()
    try:
        await app.state.cleanup_task
    except asyncio.CancelledError:
        pass
    scheduler.shutdown()
    await pool.stop()
    reset_db()


app = FastAPI(
    title="Scrapeyard",
    description="Config-driven web scraping microservice",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(router)


@app.get("/health")
async def health() -> dict:
    """Service health check endpoint with detailed status."""
    pool = get_worker_pool()

    uptime = time.monotonic() - _start_time if _start_time else 0.0

    # Determine overall status based on active task load.
    status = "ok"
    if pool.active_tasks >= pool._max_concurrent:
        status = "degraded"

    return {
        "status": status,
        "uptime_seconds": round(uptime, 1),
        "workers": {
            "max_concurrent": pool._max_concurrent,
            "active_tasks": pool.active_tasks,
            "max_browsers": pool._max_browsers,
            "active_browsers": 0,
        },
    }
