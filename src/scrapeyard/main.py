"""FastAPI application entry point."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scrapeyard import __version__
from scrapeyard.api.dependencies import (
    RuntimeServices,
    build_runtime_services,
    close_webhook_dispatcher,
    get_job_store,
    get_worker_pool,
    init_rate_limiter,
)
from scrapeyard.api.routes import router
from scrapeyard.common.logging import setup_logging
from scrapeyard.common.settings import get_settings
from scrapeyard.runtime.health import HealthCache
from scrapeyard.storage.cleanup import start_cleanup_loop
from scrapeyard.storage.database import close_db, init_db


_health = HealthCache(get_job_store)


def _assign_runtime_services(app: FastAPI, services: RuntimeServices) -> None:
    app.state.job_store = services.job_store
    app.state.error_store = services.error_store
    app.state.result_store = services.result_store
    app.state.webhook_dispatcher = services.webhook_dispatcher
    app.state.worker_pool = services.worker_pool
    app.state.scheduler = services.scheduler


async def _startup_runtime_services(app: FastAPI) -> None:
    services = build_runtime_services()
    _assign_runtime_services(app, services)
    await services.webhook_dispatcher.startup()
    await services.worker_pool.start()
    init_rate_limiter(redis=services.worker_pool.redis)
    await services.scheduler.start()
    app.state.cleanup_task = start_cleanup_loop(services.result_store)


async def _shutdown_runtime_services(app: FastAPI, *, shutdown_grace_seconds: int) -> None:
    app.state.cleanup_task.cancel()
    try:
        await app.state.cleanup_task
    except asyncio.CancelledError:
        pass
    app.state.scheduler.shutdown()
    await app.state.worker_pool.stop()
    await close_webhook_dispatcher(timeout=shutdown_grace_seconds)
    await close_db()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Orchestrate startup and shutdown in dependency order."""
    _health.mark_started()

    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level)
    await init_db(settings.db_dir)
    await _startup_runtime_services(app)

    yield

    await _shutdown_runtime_services(app, shutdown_grace_seconds=settings.workers_shutdown_grace_seconds)


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
