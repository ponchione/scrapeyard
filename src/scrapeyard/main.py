"""FastAPI application entry point."""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scrapeyard.api.dependencies import get_scheduler, get_worker_pool
from scrapeyard.api.routes import router

_start_time: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start scheduler and worker pool on startup; shut down on exit."""
    global _start_time  # noqa: PLW0603
    _start_time = time.monotonic()
    scheduler = get_scheduler()
    pool = get_worker_pool()
    await scheduler.start()
    await pool.start()
    yield
    scheduler.shutdown()
    await pool.stop()


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
