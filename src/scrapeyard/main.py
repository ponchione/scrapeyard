"""FastAPI application entry point."""
import asyncio
import logging
import shutil
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

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
    APIVersionHeaderMiddleware,
    MetricsMiddleware,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
)
from scrapeyard.api.auth import AuthScope, authorize_request, parse_api_credentials
from scrapeyard.api.routes import router
from scrapeyard.api.response_models import (
    ERROR_RESPONSES,
    ErrorEnvelope,
    HealthResponse,
    LivenessResponse,
)
from scrapeyard.api.response_utils import error_content
from scrapeyard.common.logging import setup_logging
from scrapeyard.common.async_tools import AwaitableCancelled, MonotonicDeadline
from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.queue.reconciliation import (
    reconcile_stale_queued_jobs,
    start_queued_reconciliation_loop,
)
from scrapeyard.queue.terminal_reconciliation import (
    reconcile_terminal_webhook_intents,
)
from scrapeyard.runtime.health import (
    HealthCache,
    ProbeResult,
    probe_disk,
    probe_asyncio_task,
    probe_background_service,
    probe_redis,
    probe_result_storage,
    probe_sqlite,
)
from scrapeyard.runtime.instance_guard import (
    SingleInstanceLock,
    instance_identity,
    instance_lock_path,
    validate_single_process_configuration,
)
from scrapeyard.runtime.metrics import (
    ACTIVE_WORK,
    BACKGROUND_TASK,
    DISK_FREE_BYTES,
    METRICS_REFRESH_DURATION,
    METRICS_REFRESH_FAILURES,
    QUEUE_DEPTH,
    QUEUE_OLDEST_AGE,
    WEBHOOK_BACKLOG,
    WEBHOOK_OLDEST_AGE,
    WORK_CAPACITY,
    render_metrics,
)
from scrapeyard.storage.cleanup import start_cleanup_loop
from scrapeyard.storage.database import close_db, init_db
from scrapeyard.storage.secret_envelope import migrate_persisted_secrets
from scrapeyard.storage.secret_envelope import (
    EncryptionKeyring,
    SecretKeyConfigurationError,
)


_health = HealthCache(get_job_store)
logger = logging.getLogger(__name__)
_metrics_refresh_lock = asyncio.Lock()
_metrics_refreshed_at = 0.0


async def _recover_stale_running_jobs() -> None:
    """Fail stale running jobs/runs before workers and scheduler start."""
    settings = get_settings()
    recovered_at = utc_now()
    cutoff = recovered_at - timedelta(
        seconds=settings.workers_running_heartbeat_timeout_seconds
    )
    recoveries = await get_job_store().recover_stale_running_jobs(cutoff, recovered_at)
    for recovery in recoveries:
        logger.warning(
            "Recovered running state job_id=%s run_id=%s last_heartbeat=%s "
            "timeout_seconds=%s recovery_action=%s",
            recovery.job_id,
            recovery.run_id,
            (
                recovery.last_heartbeat_at.isoformat()
                if recovery.last_heartbeat_at is not None
                else None
            ),
            settings.workers_running_heartbeat_timeout_seconds,
            recovery.action,
        )


def _ensure_runtime_directories() -> None:
    settings = get_settings()
    for path in (settings.storage_results_dir, settings.adaptive_dir):
        Path(path).mkdir(parents=True, exist_ok=True)


def _assign_runtime_services(app: FastAPI, services: RuntimeServices) -> None:
    app.state.worker_pool = services.worker_pool
    app.state.scheduler = services.scheduler
    app.state.webhook_dispatcher = services.webhook_dispatcher
    app.state.webhook_outbox_store = services.webhook_outbox_store


async def _startup_runtime_services(app: FastAPI) -> None:
    services = build_runtime_services()
    _assign_runtime_services(app, services)
    app.state.terminal_intent_reconciliation = (
        await reconcile_terminal_webhook_intents(
            job_store=get_job_store(),
            result_store=services.result_store,
        )
    )
    await services.webhook_dispatcher.startup()
    await services.worker_pool.start()
    init_rate_limiter(redis=services.worker_pool.redis)
    app.state.queued_reconciliation = await reconcile_stale_queued_jobs(
        job_store=get_job_store(),
        worker_pool=services.worker_pool,
        queued_claim_timeout_seconds=(
            get_settings().workers_queued_claim_timeout_seconds
        ),
    )
    app.state.queued_reconciliation_task = start_queued_reconciliation_loop(
        job_store=get_job_store(),
        worker_pool=services.worker_pool,
        queued_claim_timeout_seconds=(
            get_settings().workers_queued_claim_timeout_seconds
        ),
        interval_seconds=(
            get_settings().workers_queued_reconciliation_interval_seconds
        ),
        batch_size=get_settings().workers_queued_reconciliation_batch_size,
    )
    await services.scheduler.start()
    app.state.cleanup_task = start_cleanup_loop(
        services.result_store,
        services.webhook_outbox_store,
        interval_hours=get_settings().storage_cleanup_interval_seconds / 3600,
        job_store=get_job_store(),
    )


async def _shutdown_runtime_services(
    app: FastAPI,
    *,
    shutdown_grace_seconds: float,
) -> None:
    deadline = MonotonicDeadline(shutdown_grace_seconds)
    failures: list[tuple[str, Exception]] = []

    def remaining() -> float:
        value = deadline.remaining
        return 0.0 if value is None else value

    def record_failure(phase: str, exc: Exception) -> None:
        failures.append((phase, exc))
        logger.exception("Runtime shutdown phase failed phase=%s", phase)

    background_tasks = (
        ("cleanup", getattr(app.state, "cleanup_task", None)),
        (
            "queued_reconciliation",
            getattr(app.state, "queued_reconciliation_task", None),
        ),
    )
    for phase, task in background_tasks:
        if task is None:
            continue
        task.cancel()
        try:
            await deadline.run(task)
        except AwaitableCancelled:
            pass
        except asyncio.TimeoutError as exc:
            record_failure(phase, exc)
        except Exception as exc:
            record_failure(phase, exc)
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        try:
            scheduler.shutdown()
        except Exception as exc:
            record_failure("scheduler", exc)
    worker_pool = getattr(app.state, "worker_pool", None)
    if worker_pool is not None:
        try:
            await worker_pool.stop(timeout=remaining())
        except Exception as exc:
            record_failure("worker", exc)
            if bool(getattr(worker_pool, "shutdown_pending", False)):
                phases = ", ".join(phase for phase, _exc in failures)
                raise RuntimeError(
                    f"Runtime shutdown failed for phase(s): {phases}; "
                    "shared services retained for live worker tasks"
                ) from exc
    try:
        await close_webhook_dispatcher(timeout=remaining())
    except Exception as exc:
        record_failure("webhook", exc)
    try:
        await deadline.run(close_db())
    except Exception as exc:
        record_failure("database", exc)
    if failures:
        phases = ", ".join(phase for phase, _exc in failures)
        raise RuntimeError(f"Runtime shutdown failed for phase(s): {phases}") from failures[0][1]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Orchestrate startup and shutdown in dependency order."""
    _health.mark_started()

    settings = get_settings()
    setup_logging(settings.log_dir, settings.log_level)
    validate_single_process_configuration(arguments=sys.argv[1:])
    identity = instance_identity(
        db_dir=settings.db_dir,
        queue_name=settings.queue_name,
        redis_dsn=settings.redis_dsn,
    )
    instance_lock = SingleInstanceLock(instance_lock_path(settings.db_dir), identity)
    instance_lock.acquire()
    app.state.instance_lock = instance_lock
    logger.info(
        "Acquired single-instance guard path=%s queue=%s redis=%s",
        instance_lock.path,
        identity.queue_name,
        identity.redis,
    )
    if getattr(settings, "qualification_mode", False):
        logger.critical(
            "LOCAL DESTRUCTIVE QUALIFICATION MODE ENABLED crash_point=%s "
            "remote_trigger_available=false",
            getattr(settings, "qualification_crash_point", "") or "none",
        )
    try:
        # Validate write capability even when every durable table is empty.
        EncryptionKeyring.from_settings(required=True)
        await init_db(settings.db_dir)
        await migrate_persisted_secrets()
        _ensure_runtime_directories()
        await _recover_stale_running_jobs()
        await _startup_runtime_services(app)

        yield
    finally:
        release_instance_lock = True
        try:
            await _shutdown_runtime_services(
                app,
                shutdown_grace_seconds=settings.workers_shutdown_grace_seconds,
            )
        except BaseException:
            worker_pool = getattr(app.state, "worker_pool", None)
            release_instance_lock = not bool(
                getattr(worker_pool, "shutdown_pending", False)
            )
            raise
        finally:
            if release_instance_lock:
                instance_lock.release()
                app.state.instance_lock = None
                logger.info("Released single-instance guard path=%s", instance_lock.path)
            else:
                logger.critical(
                    "Retaining single-instance guard for unresolved worker shutdown path=%s",
                    instance_lock.path,
                )


app = FastAPI(
    title="Scrapeyard",
    description="Config-driven web scraping microservice",
    version=__version__,
    lifespan=lifespan,
)


@app.exception_handler(SecretKeyConfigurationError)
async def _secret_key_configuration_error_handler(
    _request: Request,
    _exc: SecretKeyConfigurationError,
) -> JSONResponse:
    """Keep a post-start keyring failure sanitized and explicitly unavailable."""

    logger.error("Encryption key configuration became invalid after startup")
    return JSONResponse(
        status_code=503,
        content=error_content("Service encryption configuration is unavailable"),
    )

_settings_for_middleware = get_settings()
_credentials_for_middleware = parse_api_credentials(
    _settings_for_middleware.api_credentials,
    legacy_keys=_settings_for_middleware.parsed_api_keys(),
)
# Order matters: last add_middleware() call becomes outermost. We want the
# size-limit guard to run first so oversized payloads never reach rate limiting,
# auth, or the router. Rate limiting then protects auth/router parsing and queue
# enqueue paths from bursts.
app.add_middleware(
    APIKeyAuthMiddleware,
    credentials=_credentials_for_middleware,
    exempt_paths={"/health", "/health/live"},
)
app.add_middleware(
    RateLimitMiddleware,
    requests=_settings_for_middleware.rate_limit_requests,
    window_seconds=_settings_for_middleware.rate_limit_window_seconds,
    api_keys={credential.secret for credential in _credentials_for_middleware},
    exempt_paths={"/health", "/health/live"},
)
app.add_middleware(
    RequestSizeLimitMiddleware,
    max_bytes=_settings_for_middleware.max_request_bytes,
)
app.add_middleware(APIVersionHeaderMiddleware, version="1")
app.add_middleware(MetricsMiddleware)

app.include_router(router)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=error_content(exc.status_code, message),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    details = [
        {
            "location": list(error.get("loc", ())),
            "message": str(error.get("msg", "Invalid value")),
            "type": str(error.get("type", "validation_error")),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=error_content(
            422,
            "Request validation failed",
            code="validation_error",
            details=details,
        ),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return the public JSON contract without exposing internal exception text."""
    logger.error(
        "Unhandled API exception method=%s path=%s error_type=%s",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content=error_content(500, "Internal server error"),
        # Starlette's outer error middleware sends this response outside the
        # user-middleware stack. The version middleware deduplicates the field
        # whenever a response does traverse that stack.
        headers={"X-Scrapeyard-API-Version": "1"},
    )


async def _timed_async_probe(
    name: str,
    awaitable: Awaitable[ProbeResult],
    timeout: float,
) -> ProbeResult:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        return ProbeResult(False, f"{name} probe timed out after {timeout:g}s")
    except Exception as exc:
        return ProbeResult(False, f"{name} probe failed: {type(exc).__name__}")


async def _timed_sync_probe(
    name: str,
    function: Callable[[], ProbeResult],
    timeout: float,
) -> ProbeResult:
    return await _timed_async_probe(name, asyncio.to_thread(function), timeout)


def _background_probes() -> dict[str, ProbeResult]:
    return {
        "worker": probe_background_service(
            "worker", getattr(app.state, "worker_pool", None)
        ),
        "scheduler": probe_background_service(
            "scheduler", getattr(app.state, "scheduler", None)
        ),
        "cleanup": probe_asyncio_task(
            "cleanup", getattr(app.state, "cleanup_task", None)
        ),
        "webhook": probe_background_service(
            "webhook", getattr(app.state, "webhook_dispatcher", None)
        ),
    }


async def health(
    *,
    allowed_projects: frozenset[str] | None = None,
) -> JSONResponse:
    """Service health check endpoint with detailed status.

    Returns 200 when all dependencies (Redis, SQLite, disk) are reachable and
    within thresholds; 503 otherwise so that container orchestrators can
    recycle the process rather than sending it live traffic.
    """
    settings = get_settings()
    pool = get_worker_pool()

    uptime = _health.uptime
    projects = await _health.project_summary() if settings.health_include_projects else {}
    if allowed_projects is not None:
        projects = {
            project: summary
            for project, summary in projects.items()
            if project in allowed_projects
        }

    timeout = settings.health_probe_timeout_seconds
    redis_probe = await _timed_async_probe("redis", probe_redis(pool), timeout)
    queue_depths: dict[str, int | None]
    try:
        queue_depths = cast(
            dict[str, int | None],
            await asyncio.wait_for(pool.queue_depths(), timeout=timeout),
        )
    except Exception as exc:
        queue_depths = {"high": None, "normal": None, "low": None}
        redis_probe = ProbeResult(
            False,
            f"redis queue depth probe failed: {exc}",
        )
    sqlite_names = ("jobs.db", "errors.db", "results_meta.db")
    sqlite_results = await asyncio.gather(
        *(
            _timed_async_probe(name, probe_sqlite(name), timeout)
            for name in sqlite_names
        )
    )
    sqlite_probe = ProbeResult(
        all(result.ok for result in sqlite_results),
        next((result.detail for result in sqlite_results if not result.ok), None),
    )
    artifact_probe = await _timed_sync_probe(
        "result storage",
        lambda: probe_result_storage(settings.storage_results_dir),
        timeout,
    )
    disk_probe = await _timed_sync_probe(
        "disk",
        lambda: probe_disk(
            settings.storage_results_dir,
            settings.health_disk_free_min_mb,
        ),
        timeout,
    )
    background_probes = _background_probes()

    dependencies = {
        "redis": {"ok": redis_probe.ok, "detail": redis_probe.detail},
        "sqlite": {"ok": sqlite_probe.ok, "detail": sqlite_probe.detail},
        "sqlite_jobs": {"ok": sqlite_results[0].ok, "detail": sqlite_results[0].detail},
        "sqlite_errors": {"ok": sqlite_results[1].ok, "detail": sqlite_results[1].detail},
        "sqlite_results": {"ok": sqlite_results[2].ok, "detail": sqlite_results[2].detail},
        "result_storage": {"ok": artifact_probe.ok, "detail": artifact_probe.detail},
        "disk": {"ok": disk_probe.ok, "detail": disk_probe.detail},
    }

    all_ok = (
        redis_probe.ok
        and sqlite_probe.ok
        and artifact_probe.ok
        and disk_probe.ok
        and all(probe.ok for probe in background_probes.values())
    )
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
            "queue_depths": queue_depths,
        },
        "dependencies": dependencies,
        "background_tasks": {
            name: {"ok": probe.ok, "detail": probe.detail}
            for name, probe in background_probes.items()
        },
        "projects": projects,
    }
    return JSONResponse(status_code=200 if all_ok else 503, content=body)


@app.get("/health", response_model=LivenessResponse)
@app.get("/health/live", response_model=LivenessResponse)
async def liveness() -> dict[str, str]:
    """Cheap public process liveness with no deployment diagnostics."""

    return {"status": "ok"}


@app.get(
    "/health/ready",
    response_model=HealthResponse,
    responses={
        401: {"model": ErrorEnvelope},
        403: {"model": ErrorEnvelope},
        503: {"model": HealthResponse},
        500: {"model": ErrorEnvelope},
    },
)
async def readiness(request: Request) -> JSONResponse:
    """Detailed dependency/capacity diagnostics for monitoring credentials."""

    caller = authorize_request(request, AuthScope.health_detail)
    return await health(allowed_projects=caller.projects)


async def _refresh_metrics() -> None:
    global _metrics_refreshed_at
    settings = get_settings()
    now = time.monotonic()
    if now - _metrics_refreshed_at < settings.metrics_refresh_interval_seconds:
        return
    async with _metrics_refresh_lock:
        now = time.monotonic()
        if now - _metrics_refreshed_at < settings.metrics_refresh_interval_seconds:
            return
        started = time.perf_counter()
        pool = get_worker_pool()
        timeout = settings.health_probe_timeout_seconds
        ACTIVE_WORK.labels("jobs").set(pool.active_tasks)
        ACTIVE_WORK.labels("browsers").set(pool.active_browsers)
        WORK_CAPACITY.labels("jobs").set(pool.max_concurrent)
        WORK_CAPACITY.labels("browsers").set(pool.max_browsers)
        try:
            queue_snapshot = await asyncio.wait_for(
                pool.queue_operational_snapshot(), timeout=timeout
            )
        except Exception:
            METRICS_REFRESH_FAILURES.labels("queue").inc()
        else:
            for priority, (depth, age_seconds) in queue_snapshot.items():
                QUEUE_DEPTH.labels(priority).set(depth)
                QUEUE_OLDEST_AGE.labels(priority).set(age_seconds)

        outbox = getattr(app.state, "webhook_outbox_store", None)
        if outbox is None:
            METRICS_REFRESH_FAILURES.labels("webhook").inc()
        else:
            try:
                summary = await asyncio.wait_for(outbox.summarize(), timeout=timeout)
            except Exception:
                METRICS_REFRESH_FAILURES.labels("webhook").inc()
            else:
                WEBHOOK_BACKLOG.labels("pending").set(summary.pending)
                WEBHOOK_BACKLOG.labels("delivered").set(summary.delivered)
                WEBHOOK_BACKLOG.labels("failed").set(summary.failed)
                WEBHOOK_OLDEST_AGE.set(summary.oldest_pending_age_seconds or 0.0)

        try:
            DISK_FREE_BYTES.set(shutil.disk_usage(settings.storage_results_dir).free)
        except OSError:
            METRICS_REFRESH_FAILURES.labels("result_storage").inc()
        for name, probe in _background_probes().items():
            BACKGROUND_TASK.labels(name).set(1 if probe.ok else 0)
        _metrics_refreshed_at = now
        METRICS_REFRESH_DURATION.observe(time.perf_counter() - started)


@app.get(
    "/metrics",
    response_class=Response,
    responses=ERROR_RESPONSES,
)
async def metrics(request: Request) -> Response:
    """Authenticated Prometheus text exposition with cached durable gauges."""

    authorize_request(request, AuthScope.health_detail)
    await _refresh_metrics()
    return Response(
        render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
