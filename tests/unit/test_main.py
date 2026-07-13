"""Tests for main.py health cache, lifespan orchestration, and health status."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

import scrapeyard.main as main_module
from scrapeyard.queue.reconciliation import (
    QueuedReconciliationError,
    QueuedReconciliationSummary,
)
from scrapeyard.runtime.instance_guard import SingleInstanceError
from scrapeyard.api.middleware import (
    APIKeyAuthMiddleware,
    APIVersionHeaderMiddleware,
    MetricsMiddleware,
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
)
from scrapeyard.storage.types import RunRecovery
from scrapeyard.webhook.dispatcher import HttpWebhookDispatcher


@pytest.mark.asyncio
async def test_health_cache_project_summary_classifies_statuses():
    fake_store = MagicMock(summary_by_project=AsyncMock(return_value=[
        ("healthy-project", "complete", 2),
        ("degraded-project", "running", 1),
        ("degraded-project", "partial", 1),
        ("failing-project", "failed", 1),
    ]))
    cache = main_module.HealthCache(lambda: fake_store, cache_ttl_seconds=60)

    summary = await cache.project_summary()

    assert summary["healthy-project"]["status"] == "healthy"
    assert summary["degraded-project"]["status"] == "degraded"
    assert summary["failing-project"]["status"] == "failing"
    assert summary["degraded-project"]["job_count"] == 2


@pytest.mark.asyncio
async def test_health_cache_uses_cached_summary_until_ttl_expires():
    fake_store = MagicMock(summary_by_project=AsyncMock(return_value=[("proj", "complete", 1)]))
    cache = main_module.HealthCache(lambda: fake_store, cache_ttl_seconds=60)

    first = await cache.project_summary()
    second = await cache.project_summary()

    assert first == second
    fake_store.summary_by_project.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_cache_returns_empty_summary_when_store_unavailable():
    cache = main_module.HealthCache(
        lambda: (_ for _ in ()).throw(ValueError("not ready")),
        cache_ttl_seconds=60,
    )

    summary = await cache.project_summary()

    assert summary == {}


@pytest.mark.asyncio
async def test_lifespan_initializes_and_shuts_down_dependencies(monkeypatch, tmp_path):
    app = FastAPI()
    settings = SimpleNamespace(
        log_dir="/tmp/logs",
        log_level="DEBUG",
        db_dir=str(tmp_path / "db"),
        redis_dsn="redis://redis:6379/0",
        queue_name="scrapeyard-test",
        storage_results_dir=str(tmp_path / "results"),
        adaptive_dir=str(tmp_path / "adaptive"),
        browser_debug_enabled=False,
        workers_shutdown_grace_seconds=7,
        workers_queued_claim_timeout_seconds=300,
        workers_running_heartbeat_timeout_seconds=600,
        storage_cleanup_interval_seconds=21600,
    )
    now = datetime(2026, 4, 24, 12, 0, tzinfo=timezone.utc)
    job_store = SimpleNamespace(
        recover_stale_running_jobs=AsyncMock(
            return_value=[
                RunRecovery(
                    job_id="recovered-job",
                    run_id="recovered-run",
                    action="failed_stale_heartbeat",
                    last_heartbeat_at=now - timedelta(seconds=601),
                )
            ]
        )
    )
    pool = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), redis=object())
    scheduler = SimpleNamespace(start=AsyncMock(), shutdown=MagicMock())
    webhook_dispatcher = SimpleNamespace(startup=AsyncMock())
    class _CleanupTask:
        def __init__(self) -> None:
            self.cancel = MagicMock()
            self.awaited = False

        def __await__(self):
            async def _wait():
                self.awaited = True
                raise asyncio.CancelledError
            return _wait().__await__()

    cleanup_task = _CleanupTask()

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "utc_now", lambda: now)
    monkeypatch.setattr(main_module, "get_job_store", lambda: job_store)
    monkeypatch.setattr(main_module, "setup_logging", MagicMock())
    monkeypatch.setattr(main_module, "init_db", AsyncMock())
    monkeypatch.setattr(main_module, "migrate_persisted_secrets", AsyncMock())
    monkeypatch.setattr(
        main_module,
        "build_runtime_services",
        lambda: main_module.RuntimeServices(
            result_store="result-store",
            webhook_outbox_store="webhook-outbox-store",
            webhook_dispatcher=webhook_dispatcher,
            worker_pool=pool,
            scheduler=scheduler,
        ),
    )
    monkeypatch.setattr(main_module, "init_rate_limiter", MagicMock())
    terminal_reconciliation = AsyncMock()
    monkeypatch.setattr(
        main_module,
        "reconcile_terminal_webhook_intents",
        terminal_reconciliation,
    )
    monkeypatch.setattr(
        main_module,
        "reconcile_stale_queued_jobs",
        AsyncMock(return_value=QueuedReconciliationSummary()),
    )
    monkeypatch.setattr(
        main_module,
        "start_cleanup_loop",
        lambda _result_store, _outbox_store, *, interval_hours, job_store: cleanup_task,
    )
    monkeypatch.setattr(main_module, "close_webhook_dispatcher", AsyncMock())
    monkeypatch.setattr(main_module, "close_db", AsyncMock())

    async with main_module.lifespan(app):
        assert not hasattr(app.state, "job_store")
        assert not hasattr(app.state, "error_store")
        assert not hasattr(app.state, "result_store")
        assert app.state.webhook_dispatcher is webhook_dispatcher
        assert app.state.webhook_outbox_store == "webhook-outbox-store"
        assert app.state.worker_pool is pool
        assert app.state.scheduler is scheduler
        assert app.state.cleanup_task is cleanup_task
        assert (tmp_path / "results").is_dir()
        assert (tmp_path / "adaptive").is_dir()
        assert not (tmp_path / "browser-debug").exists()

    main_module.setup_logging.assert_called_once_with("/tmp/logs", "DEBUG")
    main_module.init_db.assert_awaited_once_with(str(tmp_path / "db"))
    main_module.migrate_persisted_secrets.assert_awaited_once_with()
    job_store.recover_stale_running_jobs.assert_awaited_once_with(
        now - timedelta(seconds=600),
        now,
    )
    webhook_dispatcher.startup.assert_awaited_once()
    terminal_reconciliation.assert_awaited_once_with(
        job_store=job_store,
        result_store="result-store",
    )
    pool.start.assert_awaited_once()
    main_module.init_rate_limiter.assert_called_once_with(redis=pool.redis)
    main_module.reconcile_stale_queued_jobs.assert_awaited_once_with(
        job_store=job_store,
        worker_pool=pool,
        queued_claim_timeout_seconds=300,
    )
    scheduler.start.assert_awaited_once()
    cleanup_task.cancel.assert_called_once()
    scheduler.shutdown.assert_called_once()
    pool.stop.assert_awaited_once()
    worker_timeout = pool.stop.await_args.kwargs["timeout"]
    webhook_timeout = main_module.close_webhook_dispatcher.await_args.kwargs["timeout"]
    assert 0 <= webhook_timeout <= worker_timeout <= 7
    main_module.close_db.assert_awaited_once()
    assert app.state.instance_lock is None

    restarted_lock = main_module.SingleInstanceLock(
        main_module.instance_lock_path(settings.db_dir),
        main_module.instance_identity(
            db_dir=settings.db_dir,
            queue_name=settings.queue_name,
            redis_dsn=settings.redis_dsn,
        ),
    )
    restarted_lock.acquire()
    restarted_lock.release()


@pytest.mark.asyncio
async def test_shutdown_attempts_every_phase_after_independent_failures(monkeypatch):
    app = FastAPI()
    cleanup_task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    scheduler = SimpleNamespace(shutdown=MagicMock(side_effect=RuntimeError("scheduler")))
    worker = SimpleNamespace(stop=AsyncMock(side_effect=RuntimeError("worker")))
    app.state.cleanup_task = cleanup_task
    app.state.scheduler = scheduler
    app.state.worker_pool = worker
    close_webhook = AsyncMock(side_effect=RuntimeError("webhook"))
    close_database = AsyncMock(side_effect=RuntimeError("database"))
    monkeypatch.setattr(main_module, "close_webhook_dispatcher", close_webhook)
    monkeypatch.setattr(main_module, "close_db", close_database)

    with pytest.raises(
        RuntimeError,
        match="scheduler, worker, webhook, database",
    ):
        await main_module._shutdown_runtime_services(app, shutdown_grace_seconds=1)

    assert cleanup_task.cancelled()
    scheduler.shutdown.assert_called_once_with()
    worker.stop.assert_awaited_once()
    close_webhook.assert_awaited_once()
    close_database.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_passes_remaining_shared_budget_to_later_phases(monkeypatch):
    app = FastAPI()
    cleanup_task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    observed: dict[str, float] = {}

    async def stop_worker(*, timeout: float) -> None:
        observed["worker"] = timeout
        await asyncio.sleep(0.02)

    async def close_webhook(*, timeout: float) -> None:
        observed["webhook"] = timeout

    app.state.cleanup_task = cleanup_task
    app.state.worker_pool = SimpleNamespace(stop=stop_worker)
    monkeypatch.setattr(main_module, "close_webhook_dispatcher", close_webhook)
    monkeypatch.setattr(main_module, "close_db", AsyncMock())

    await main_module._shutdown_runtime_services(app, shutdown_grace_seconds=0.2)

    assert 0 < observed["webhook"] < observed["worker"] <= 0.2


@pytest.mark.asyncio
async def test_shutdown_webhook_backlog_drains_before_database_close(monkeypatch):
    app = FastAPI()
    app.state.worker_pool = SimpleNamespace(stop=AsyncMock())
    summary = SimpleNamespace(
        pending=0,
        delivered=0,
        failed=0,
        oldest_pending_age_seconds=None,
        pending_attempts_min=None,
        pending_attempts_max=None,
        pending_attempts_total=0,
    )
    outbox = SimpleNamespace(
        summarize=AsyncMock(return_value=summary),
        list_exhausted_pending=AsyncMock(return_value=[]),
        list_due_pending=AsyncMock(return_value=[]),
        next_pending_due_at=AsyncMock(return_value=None),
        oldest_pending_created_at=AsyncMock(return_value=None),
    )
    dispatcher = HttpWebhookDispatcher(
        outbox_store=outbox,
        dispatch_concurrency=1,
        dispatch_batch_size=3,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    processed: list[str] = []

    async def process_delivery(delivery) -> None:
        processed.append(delivery.delivery_id)
        if len(processed) == 1:
            first_started.set()
            await release_first.wait()

    dispatcher._process_delivery = process_delivery
    await dispatcher.startup()
    assert dispatcher._queue is not None
    for index in range(3):
        delivery = SimpleNamespace(delivery_id=f"queued-{index}")
        dispatcher._queue.put_nowait(delivery)
        dispatcher._scheduled_ids.add(delivery.delivery_id)
    await asyncio.wait_for(first_started.wait(), timeout=1)

    async def close_webhook(*, timeout: float) -> None:
        await dispatcher.shutdown(timeout=timeout)

    database_closed = asyncio.Event()

    async def close_database() -> None:
        database_closed.set()

    monkeypatch.setattr(main_module, "close_webhook_dispatcher", close_webhook)
    monkeypatch.setattr(main_module, "close_db", close_database)

    shutdown = asyncio.create_task(
        main_module._shutdown_runtime_services(app, shutdown_grace_seconds=1)
    )
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.wait_for(shutdown, timeout=1)

    assert processed == ["queued-0", "queued-1", "queued-2"]
    assert database_closed.is_set()


@pytest.mark.asyncio
async def test_shutdown_zero_grace_still_cancels_and_closes_everything(monkeypatch):
    app = FastAPI()
    cleanup_task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    worker = SimpleNamespace(stop=AsyncMock())
    close_webhook = AsyncMock()
    close_database = AsyncMock()
    app.state.cleanup_task = cleanup_task
    app.state.worker_pool = worker
    monkeypatch.setattr(main_module, "close_webhook_dispatcher", close_webhook)
    monkeypatch.setattr(main_module, "close_db", close_database)

    await main_module._shutdown_runtime_services(app, shutdown_grace_seconds=0)

    assert cleanup_task.cancelled()
    worker.stop.assert_awaited_once_with(timeout=0.0)
    close_webhook.assert_awaited_once_with(timeout=0.0)
    close_database.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_deadline_bounds_cancellation_resistant_database(monkeypatch):
    app = FastAPI()
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def close_database() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    app.state.worker_pool = SimpleNamespace(stop=AsyncMock())
    monkeypatch.setattr(main_module, "close_webhook_dispatcher", AsyncMock())
    monkeypatch.setattr(main_module, "close_db", close_database)
    loop = asyncio.get_running_loop()
    before = loop.time()

    with pytest.raises(RuntimeError, match="database"):
        await main_module._shutdown_runtime_services(
            app,
            shutdown_grace_seconds=0.005,
        )

    assert loop.time() - before < 0.1
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
    release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_lifespan_rejects_second_instance_before_database_start(monkeypatch, tmp_path):
    app = FastAPI()
    settings = SimpleNamespace(
        log_dir=str(tmp_path / "logs"),
        log_level="INFO",
        db_dir=str(tmp_path / "db"),
        redis_dsn="redis://redis:6379/0",
        queue_name="shared-queue",
        storage_results_dir=str(tmp_path / "results"),
        adaptive_dir=str(tmp_path / "adaptive"),
        workers_shutdown_grace_seconds=7,
    )
    held_lock = main_module.SingleInstanceLock(
        main_module.instance_lock_path(settings.db_dir),
        main_module.instance_identity(
            db_dir=settings.db_dir,
            queue_name=settings.queue_name,
            redis_dsn=settings.redis_dsn,
        ),
    )
    held_lock.acquire()
    init_db = AsyncMock()
    shutdown = AsyncMock()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "setup_logging", MagicMock())
    monkeypatch.setattr(main_module, "init_db", init_db)
    monkeypatch.setattr(main_module, "_shutdown_runtime_services", shutdown)

    try:
        with pytest.raises(SingleInstanceError, match="Another Scrapeyard"):
            async with main_module.lifespan(app):
                pytest.fail("contending lifespan must never begin serving")
    finally:
        held_lock.release()

    init_db.assert_not_awaited()
    shutdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_connects_redis_then_reconciles_before_scheduler(monkeypatch):
    app = FastAPI()
    events: list[str] = []
    job_store = object()

    async def _start_pool() -> None:
        events.append("redis_connected")

    async def _reconcile_terminal(**kwargs):
        assert kwargs == {
            "job_store": job_store,
            "result_store": "result-store",
        }
        events.append("terminal_intents_reconciled")

    async def _replay_webhooks() -> None:
        events.append("webhook_outbox_replayed")

    async def _reconcile(**kwargs):
        assert kwargs == {
            "job_store": job_store,
            "worker_pool": pool,
            "queued_claim_timeout_seconds": 300,
        }
        events.append("queued_reconciled")
        return QueuedReconciliationSummary()

    async def _start_scheduler() -> None:
        events.append("scheduler_started")

    pool = SimpleNamespace(start=_start_pool, redis=object())
    scheduler = SimpleNamespace(start=_start_scheduler)
    services = main_module.RuntimeServices(
        result_store="result-store",
        webhook_outbox_store="webhook-outbox-store",
        webhook_dispatcher=SimpleNamespace(startup=_replay_webhooks),
        worker_pool=pool,
        scheduler=scheduler,
    )
    monkeypatch.setattr(main_module, "build_runtime_services", lambda: services)
    monkeypatch.setattr(main_module, "get_job_store", lambda: job_store)
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(
            workers_queued_claim_timeout_seconds=300,
            storage_cleanup_interval_seconds=21600,
        ),
    )
    monkeypatch.setattr(main_module, "init_rate_limiter", MagicMock())
    monkeypatch.setattr(
        main_module,
        "reconcile_terminal_webhook_intents",
        _reconcile_terminal,
    )
    monkeypatch.setattr(main_module, "reconcile_stale_queued_jobs", _reconcile)
    monkeypatch.setattr(main_module, "start_cleanup_loop", MagicMock())

    await main_module._startup_runtime_services(app)

    assert events == [
        "terminal_intents_reconciled",
        "webhook_outbox_replayed",
        "redis_connected",
        "queued_reconciled",
        "scheduler_started",
    ]


@pytest.mark.asyncio
async def test_redis_inspection_failure_prevents_scheduler_start(monkeypatch):
    app = FastAPI()
    pool = SimpleNamespace(start=AsyncMock(), redis=object())
    scheduler = SimpleNamespace(start=AsyncMock())
    services = main_module.RuntimeServices(
        result_store="result-store",
        webhook_outbox_store="webhook-outbox-store",
        webhook_dispatcher=SimpleNamespace(startup=AsyncMock()),
        worker_pool=pool,
        scheduler=scheduler,
    )
    monkeypatch.setattr(main_module, "build_runtime_services", lambda: services)
    monkeypatch.setattr(main_module, "get_job_store", lambda: object())
    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: SimpleNamespace(workers_queued_claim_timeout_seconds=300),
    )
    monkeypatch.setattr(main_module, "init_rate_limiter", MagicMock())
    monkeypatch.setattr(
        main_module,
        "reconcile_terminal_webhook_intents",
        AsyncMock(),
    )
    monkeypatch.setattr(
        main_module,
        "reconcile_stale_queued_jobs",
        AsyncMock(side_effect=QueuedReconciliationError("inspection unavailable")),
    )
    cleanup = MagicMock()
    monkeypatch.setattr(main_module, "start_cleanup_loop", cleanup)

    with pytest.raises(QueuedReconciliationError, match="inspection unavailable"):
        await main_module._startup_runtime_services(app)

    pool.start.assert_awaited_once()
    scheduler.start.assert_not_awaited()
    cleanup.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_shutdown_runs_when_serving_raises(monkeypatch, tmp_path):
    app = FastAPI()
    settings = SimpleNamespace(
        log_dir="/tmp/logs",
        log_level="DEBUG",
        db_dir=str(tmp_path / "db"),
        redis_dsn="redis://redis:6379/0",
        queue_name="scrapeyard-test",
        storage_results_dir="/tmp/results",
        adaptive_dir="/tmp/adaptive",
        workers_shutdown_grace_seconds=7,
    )
    shutdown = AsyncMock()

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "setup_logging", MagicMock())
    monkeypatch.setattr(main_module, "init_db", AsyncMock())
    monkeypatch.setattr(main_module, "migrate_persisted_secrets", AsyncMock())
    monkeypatch.setattr(main_module, "_ensure_runtime_directories", MagicMock())
    monkeypatch.setattr(main_module, "_recover_stale_running_jobs", AsyncMock())
    monkeypatch.setattr(main_module, "_startup_runtime_services", AsyncMock())
    monkeypatch.setattr(main_module, "_shutdown_runtime_services", shutdown)

    with pytest.raises(RuntimeError, match="boom"):
        async with main_module.lifespan(app):
            raise RuntimeError("boom")

    shutdown.assert_awaited_once_with(app, shutdown_grace_seconds=7)


@pytest.mark.asyncio
async def test_health_returns_degraded_when_pool_is_saturated(monkeypatch):
    import json

    from scrapeyard.runtime.health import ProbeResult

    pool = SimpleNamespace(
        max_concurrent=2,
        active_tasks=2,
        max_browsers=1,
        active_browsers=1,
        queue_depths=AsyncMock(
            return_value={"high": 0, "normal": 0, "low": 0}
        ),
    )
    monkeypatch.setattr(main_module, "get_worker_pool", lambda: pool)
    monkeypatch.setattr(main_module._health, "project_summary", AsyncMock(return_value={"proj": {"status": "healthy"}}))
    monkeypatch.setattr(main_module._health, "start_time", 1.0)

    async def _ok_async(*_a, **_kw):
        return ProbeResult(True)

    def _ok_sync(*_a, **_kw):
        return ProbeResult(True)

    monkeypatch.setattr(main_module, "probe_redis", _ok_async)
    monkeypatch.setattr(main_module, "probe_sqlite", _ok_async)
    monkeypatch.setattr(main_module, "probe_disk", _ok_sync)
    monkeypatch.setattr(main_module, "probe_result_storage", _ok_sync)
    monkeypatch.setattr(
        main_module,
        "_background_probes",
        lambda: {
            name: ProbeResult(True)
            for name in ("worker", "scheduler", "cleanup", "webhook")
        },
    )
    import scrapeyard.runtime.health as runtime_health
    monkeypatch.setattr(runtime_health.time, "monotonic", lambda: 13.3)

    response = await main_module.health()

    assert response.status_code == 200
    payload = json.loads(response.body.decode())
    assert payload["status"] == "degraded"
    assert payload["workers"]["active_tasks"] == 2
    assert payload["projects"] == {}


@pytest.mark.asyncio
async def test_health_filters_project_summary_for_scoped_monitor(monkeypatch):
    import json

    from scrapeyard.runtime.health import ProbeResult

    settings = SimpleNamespace(
        health_include_projects=True,
        health_probe_timeout_seconds=0.5,
        storage_results_dir="/tmp/results",
        health_disk_free_min_mb=1,
    )
    pool = SimpleNamespace(
        max_concurrent=2,
        active_tasks=0,
        max_browsers=1,
        active_browsers=0,
        queue_depths=AsyncMock(return_value={"high": 0, "normal": 0, "low": 0}),
    )
    summaries = {
        "alpha": {"job_count": 1, "status": "healthy", "status_counts": {}},
        "beta": {"job_count": 2, "status": "failing", "status_counts": {}},
    }

    async def _ok_async(*_args, **_kwargs):
        return ProbeResult(True)

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "get_worker_pool", lambda: pool)
    monkeypatch.setattr(
        main_module._health,
        "project_summary",
        AsyncMock(return_value=summaries),
    )
    monkeypatch.setattr(main_module, "probe_redis", _ok_async)
    monkeypatch.setattr(main_module, "probe_sqlite", _ok_async)
    monkeypatch.setattr(main_module, "probe_result_storage", lambda *_args: ProbeResult(True))
    monkeypatch.setattr(main_module, "probe_disk", lambda *_args: ProbeResult(True))
    monkeypatch.setattr(
        main_module,
        "_background_probes",
        lambda: {
            name: ProbeResult(True)
            for name in ("worker", "scheduler", "cleanup", "webhook")
        },
    )

    response = await main_module.health(allowed_projects=frozenset({"alpha"}))

    assert response.status_code == 200
    assert json.loads(response.body)["projects"] == {"alpha": summaries["alpha"]}


@pytest.mark.asyncio
async def test_readiness_forwards_authenticated_project_scope(monkeypatch):
    request = MagicMock()
    caller = SimpleNamespace(projects=frozenset({"alpha"}))
    response = MagicMock()
    authorize = MagicMock(return_value=caller)
    health = AsyncMock(return_value=response)
    monkeypatch.setattr(main_module, "authorize_request", authorize)
    monkeypatch.setattr(main_module, "health", health)

    assert await main_module.readiness(request) is response

    authorize.assert_called_once_with(request, main_module.AuthScope.health_detail)
    health.assert_awaited_once_with(allowed_projects=caller.projects)


def test_app_middleware_stack_versions_all_size_rate_and_auth_responses():
    middleware_classes = [middleware.cls for middleware in main_module.app.user_middleware]

    assert middleware_classes[:5] == [
        MetricsMiddleware,
        APIVersionHeaderMiddleware,
        RequestSizeLimitMiddleware,
        RateLimitMiddleware,
        APIKeyAuthMiddleware,
    ]
