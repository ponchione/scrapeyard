"""FastAPI application entry point."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from scrapeyard import __version__
from scrapeyard.api.dependencies import (
    RuntimeServices,
    build_runtime_services,
    close_webhook_dispatcher,
    get_job_store,
    get_worker_pool,
    init_rate_limiter,
)
from scrapeyard.api.middleware import (
    APIKeyAuthMiddleware,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
)
from scrapeyard.api.routes import router
from scrapeyard.common.logging import setup_logging
from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.runtime.health import HealthCache, probe_disk, probe_redis, probe_sqlite
from scrapeyard.storage.cleanup import start_cleanup_loop
from scrapeyard.storage.database import close_db, init_db


_health = HealthCache(get_job_store)
logger = logging.getLogger(__name__)


async def _recover_stale_running_jobs() -> None:
    """Fail stale running jobs/runs before workers and scheduler start."""
    settings = get_settings()
    recovered_at = utc_now()
    cutoff = recovered_at - timedelta(seconds=settings.workers_running_lease_seconds * 2)
    recovered_jobs = await get_job_store().recover_stale_running_jobs(cutoff, recovered_at)
    if recovered_jobs:
        logger.warning(
            "Recovered %d stale running job(s) older than %ds",
            recovered_jobs,
            settings.workers_running_lease_seconds * 2,
        )


def _assign_runtime_services(app: FastAPI, services: RuntimeServices) -> None:
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
    with suppress(asyncio.CancelledError):
        await app.state.cleanup_task
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
    await _recover_stale_running_jobs()
    await _startup_runtime_services(app)

    yield

    await _shutdown_runtime_services(app, shutdown_grace_seconds=settings.workers_shutdown_grace_seconds)


app = FastAPI(
    title="Scrapeyard",
    description="Config-driven web scraping microservice",
    version=__version__,
    lifespan=lifespan,
)

_settings_for_middleware = get_settings()
# Order matters: last add_middleware() call becomes outermost. We want the
# size-limit guard to run first so oversized payloads never reach rate limiting,
# auth, or the router. Rate limiting then protects auth/router parsing and queue
# enqueue paths from bursts.
app.add_middleware(
    APIKeyAuthMiddleware,
    keys=_settings_for_middleware.parsed_api_keys(),
    exempt_paths={"/health"},
)
app.add_middleware(
    RateLimitMiddleware,
    requests=_settings_for_middleware.rate_limit_requests,
    window_seconds=_settings_for_middleware.rate_limit_window_seconds,
    api_keys=_settings_for_middleware.parsed_api_keys(),
    exempt_paths={"/health"},
)
app.add_middleware(
    RequestSizeLimitMiddleware,
    max_bytes=_settings_for_middleware.max_request_bytes,
)

app.include_router(router)


@app.get("/health")
async def health() -> JSONResponse:
    """Service health check endpoint with detailed status.

    Returns 200 when all dependencies (Redis, SQLite, disk) are reachable and
    within thresholds; 503 otherwise so that container orchestrators can
    recycle the process rather than sending it live traffic.
    """
    settings = get_settings()
    pool = get_worker_pool()

    uptime = _health.uptime
    projects = await _health.project_summary()

    redis_probe = await probe_redis(pool)
    sqlite_probe = await probe_sqlite()
    disk_probe = probe_disk(settings.storage_results_dir, settings.health_disk_free_min_mb)

    dependencies = {
        "redis": {"ok": redis_probe.ok, "detail": redis_probe.detail},
        "sqlite": {"ok": sqlite_probe.ok, "detail": sqlite_probe.detail},
        "disk": {"ok": disk_probe.ok, "detail": disk_probe.detail},
    }

    all_ok = redis_probe.ok and sqlite_probe.ok and disk_probe.ok
    if not all_ok:
        status = "unhealthy"
    elif pool.active_tasks >= pool.max_concurrent:
        status = "degraded"
    else:
        status = "ok"

    body = {
        "status": status,
        "uptime_seconds": round(uptime, 1),
        "workers": {
            "max_concurrent": pool.max_concurrent,
            "active_tasks": pool.active_tasks,
            "max_browsers": pool.max_browsers,
            "active_browsers": pool.active_browsers,
        },
        "dependencies": dependencies,
        "projects": projects,
    }
    return JSONResponse(status_code=200 if all_ok else 503, content=body)
