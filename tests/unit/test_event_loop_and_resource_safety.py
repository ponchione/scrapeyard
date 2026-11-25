from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from httpx import ASGITransport, AsyncClient
from scrapling import PlayWrightFetcher

import scrapeyard.main as main_module
from scrapeyard.api.dependencies import (
    get_job_store,
    get_result_store,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.api.scrape_submission import wait_for_persisted_job
from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.settings import get_settings
from scrapeyard.config.schema import (
    FailStrategy,
    FetcherType,
    PaginationConfig,
    RetryConfig,
    TargetConfig,
)
from scrapeyard.engine import browser_session, pagination, scraper
from scrapeyard.engine.browser_pool import BrowserPool, activate_browser_pool
from scrapeyard.engine.browser_session import BrowserSession
from scrapeyard.engine.rate_limiter import LocalDomainRateLimiter
from scrapeyard.engine.scrape_models import FetchOutcome, TargetResult
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.cancellation import (
    QueueCancellationOutcome,
    QueueDeliveryState,
    RunCancellationResult,
)
from scrapeyard.queue.worker import scrape_task
from scrapeyard.runtime import metrics as metrics_module
from scrapeyard.runtime.health import ProbeResult
from scrapeyard.runtime.metrics import ACTIVE_WORK, LAST_SUCCESS, OUTPUT_RECORDS, active_target
from scrapeyard.storage import job_rows, job_store as job_store_module, secret_envelope
from scrapeyard.storage.database import init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.result_store import LocalResultStore
from scrapeyard.storage.secret_envelope import EncryptionKeyring, migrate_persisted_secrets
from scrapeyard.storage.types import RunOwnershipError, SaveResultMeta
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookDeliveryCreate,
    WebhookOutboxSummary,
)
from scrapeyard.webhook.dispatcher import (
    HttpWebhookDispatcher,
    WebhookDispatchReason,
    WebhookDispatchResult,
    WebhookDispatchStatus,
)
from tests.unit.worker_helpers import make_job, make_settings_mock, make_target

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _encoded_key(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii")


def _target(
    url: str = "https://example.test/products",
    fetcher: FetcherType = FetcherType.dynamic,
    pagination_cfg: PaginationConfig | None = None,
) -> TargetConfig:
    return TargetConfig(
        url=url,
        fetcher=fetcher,
        selectors={"title": "h1"},
        pagination=pagination_cfg,
    )


def _scheduled_yaml(name: str = "daily") -> str:
    return (
        "project: acme\n"
        f"name: {name}\n"
        "schedule:\n"
        '  cron: "0 9 * * *"\n'
        "target:\n"
        "  url: https://example.com/items\n"
        "  fetcher: basic\n"
        "  selectors:\n"
        "    title: h1\n"
    )


async def test_scrape_target_propagates_run_ownership_error(monkeypatch, tmp_path):
    async def lost_ownership(checkpoint: str) -> None:
        raise RunOwnershipError(checkpoint, "job-1", "run-1")

    monkeypatch.setattr(scraper, "fetch_basic_response", AsyncMock())
    target = _target(fetcher=FetcherType.basic)

    with pytest.raises(RunOwnershipError):
        await scraper.scrape_target(
            target,
            adaptive=False,
            retry=RetryConfig(max_attempts=1),
            adaptive_dir=str(tmp_path / "adaptive"),
            cancellation_guard=lost_ownership,
        )


async def test_scrape_target_releases_shared_session_when_prepare_context_fails(
    monkeypatch, tmp_path
):
    released: list[BrowserSession] = []
    pool = BrowserPool(1)
    original_release = pool.release

    async def track_release(session: BrowserSession) -> None:
        released.append(session)
        await original_release(session)

    monkeypatch.setattr(pool, "release", track_release)
    monkeypatch.setattr(
        scraper,
        "_prepare_scrape_context",
        MagicMock(side_effect=RuntimeError("setup boom")),
    )

    with activate_browser_pool(pool), pytest.raises(RuntimeError, match="setup boom"):
        await scraper.scrape_target(
            _target(fetcher=FetcherType.dynamic),
            adaptive=False,
            retry=RetryConfig(max_attempts=1),
            adaptive_dir=str(tmp_path / "adaptive"),
        )

    assert len(released) == 1
    assert len(pool._idle) == 1


async def test_scrape_target_does_not_create_adaptive_dir_when_adaptive_false(
    monkeypatch, tmp_path
):
    adaptive_dir = tmp_path / "should-not-exist"
    mock_response = MagicMock(status=200)
    mock_response.css.return_value = []
    monkeypatch.setattr(
        scraper,
        "fetch_basic_response",
        AsyncMock(return_value=mock_response),
    )

    await scraper.scrape_target(
        _target(fetcher=FetcherType.basic),
        adaptive=False,
        retry=RetryConfig(max_attempts=1),
        adaptive_dir=str(adaptive_dir),
    )

    assert not adaptive_dir.exists()


async def test_browser_pool_acquire_removes_disconnected_session_when_aclose_raises(
    monkeypatch,
):
    async def failing_aclose(self: BrowserSession) -> None:
        raise RuntimeError("driver crashed")

    monkeypatch.setattr(BrowserSession, "aclose", failing_aclose)
    pool = BrowserPool(1)
    session = await pool.acquire(_target(), None, PlayWrightFetcher)
    await pool.release(session)
    assert pool.sessions == 1

    session._context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False))
    with pytest.raises(RuntimeError, match="driver crashed"):
        await pool.acquire(_target(), None, PlayWrightFetcher)

    assert pool.sessions == 0


async def test_browser_pool_release_discards_disconnected_session(monkeypatch):
    closed: list[BrowserSession] = []

    async def track_aclose(self: BrowserSession) -> None:
        closed.append(self)

    monkeypatch.setattr(BrowserSession, "aclose", track_aclose)
    pool = BrowserPool(1)
    session = await pool.acquire(_target(), None, PlayWrightFetcher)
    session.routed_requests = 1
    session._context = SimpleNamespace(browser=SimpleNamespace(is_connected=lambda: False))

    await pool.release(session)

    assert closed == [session]
    assert pool.sessions == 0
    assert len(pool._idle) == 0


async def test_browser_session_fetch_settles_budget_when_cancelled_in_finally(monkeypatch):
    browser = MagicMock()
    browser.is_connected.return_value = True
    page = MagicMock(frames=[])
    page.is_closed.return_value = False
    page.close = AsyncMock()
    context = MagicMock(browser=browser, pages=[page])
    context.route = AsyncMock()
    context.unroute = AsyncMock()
    context.set_offline = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    session = BrowserSession(PlayWrightFetcher)
    session._context = context
    budget = RunBudget(
        max_duration_seconds=10,
        max_requests=100,
        max_fetched_bytes=1000,
        max_extracted_records=100,
        max_serialized_result_bytes=1000,
        max_browser_debug_bytes=1000,
    )

    in_release = asyncio.Event()
    allow_release = asyncio.Event()
    original_settle = budget.settle_requests
    settle_calls = 0

    async def slow_settle(reserved: int, used: int) -> None:
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            in_release.set()
            await allow_release.wait()
        await original_settle(reserved, used)

    monkeypatch.setattr(budget, "settle_requests", slow_settle)

    async def guard(route):
        await route.continue_()

    class _Req:
        def __init__(self, url: str) -> None:
            self.url = url
            self.redirected_from = None
            self.resource_type = "script"
            self.method = "GET"

    req1 = _Req("https://fixture.example/1")
    req2 = _Req("https://fixture.example/2")

    async def navigate(*_args, **_kwargs):
        handlers = {call.args[0]: call.args[1] for call in context.on.call_args_list}
        route_cb = context.route.call_args.args[1]
        await route_cb(MagicMock(request=req1, continue_=AsyncMock()))
        await route_cb(MagicMock(request=req2, continue_=AsyncMock()))
        handlers["requestfailed"](req1)
        raise RuntimeError("stop after navigation")

    page.goto = AsyncMock(side_effect=navigate)

    task = asyncio.create_task(
        session.fetch("https://fixture.example/", {}, guard, budget=budget)
    )
    await asyncio.wait_for(in_release.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    allow_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert budget.requests == 2


async def test_browser_session_serializes_concurrent_route_error_page_closes(monkeypatch):
    browser = MagicMock()
    browser.is_connected.return_value = True
    page = MagicMock(frames=[])
    page.is_closed.return_value = False
    page.close = AsyncMock()
    context = MagicMock(browser=browser, pages=[page])
    context.route = AsyncMock()
    context.unroute = AsyncMock()
    context.set_offline = AsyncMock()
    context.new_page = AsyncMock(return_value=page)

    active_closes = 0
    max_concurrent_closes = 0
    total_closes = 0
    first_close_started = asyncio.Event()
    allow_close_finish = asyncio.Event()

    async def instrumented_close_page(_page, _browser) -> None:
        nonlocal active_closes, max_concurrent_closes, total_closes
        active_closes += 1
        total_closes += 1
        max_concurrent_closes = max(max_concurrent_closes, active_closes)
        first_close_started.set()
        await allow_close_finish.wait()
        active_closes -= 1

    monkeypatch.setattr(browser_session, "_close_page", instrumented_close_page)

    session = BrowserSession(PlayWrightFetcher)
    session._context = context

    async def failing_guard(_route):
        raise RuntimeError("route exploded")

    async def navigate(*_args, **_kwargs):
        route_cb = context.route.call_args.args[1]
        r1 = MagicMock(request=SimpleNamespace(url="https://fixture.example/a"), abort=AsyncMock())
        r2 = MagicMock(request=SimpleNamespace(url="https://fixture.example/b"), abort=AsyncMock())
        t1 = asyncio.create_task(route_cb(r1))
        t2 = asyncio.create_task(route_cb(r2))
        await asyncio.wait_for(first_close_started.wait(), timeout=2)
        await asyncio.sleep(0)
        allow_close_finish.set()
        await asyncio.gather(t1, t2)
        raise RuntimeError("navigation aborted")

    page.goto = AsyncMock(side_effect=navigate)

    with pytest.raises(RuntimeError, match="route exploded"):
        await session.fetch("https://fixture.example/", {}, failing_guard)

    assert max_concurrent_closes == 1
    assert total_closes == 1


def test_encryption_keyring_from_settings_caches_and_invalidates(monkeypatch):
    monkeypatch.setenv("SCRAPEYARD_ENCRYPTION_KEYS", json.dumps({"k1": _encoded_key(b"a")}))
    monkeypatch.setenv("SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID", "k1")
    get_settings.cache_clear()

    loads_calls = 0
    real_loads = json.loads

    def counting_loads(text: str, *args, **kwargs):
        nonlocal loads_calls
        loads_calls += 1
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(secret_envelope.json, "loads", counting_loads)

    first = EncryptionKeyring.from_settings(required=True)
    second = EncryptionKeyring.from_settings(required=True)
    assert first is second
    assert loads_calls == 1

    monkeypatch.setenv(
        "SCRAPEYARD_ENCRYPTION_KEYS",
        json.dumps({"k1": _encoded_key(b"a"), "k2": _encoded_key(b"b")}),
    )
    monkeypatch.setenv("SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID", "k2")
    get_settings.cache_clear()

    third = EncryptionKeyring.from_settings(required=True)
    assert third is not None and third.active_key_id == "k2"
    assert loads_calls == 2


async def test_migrate_persisted_secrets_skips_aesgcm_decrypt_for_active_key_rows(
    tmp_path, monkeypatch
):
    await init_db(str(tmp_path / "db"))
    jobs = SQLiteJobStore()
    outbox = SQLiteWebhookOutboxStore()

    cfg = _scheduled_yaml("active-key")
    job = Job(
        job_id="job-active",
        project="acme",
        name="active-key",
        config_yaml=cfg,
        current_run_id="run-1",
        updated_at=NOW,
    )
    await jobs.save_job(job)
    assert await jobs.claim_run(
        "run-1",
        "job-active",
        "adhoc",
        hashlib.sha256(cfg.encode("utf-8")).hexdigest(),
        NOW,
    )
    await outbox.enqueue_delivery(
        WebhookDeliveryCreate(
            delivery_id="del-1",
            job_id="job-active",
            run_id="run-1",
            event="job.complete",
            url="https://hooks.example.com/cb",
            headers={"Authorization": "Bearer secret"},
            timeout_seconds=5,
            payload={"delivery_id": "del-1", "ok": True},
            next_attempt_at=NOW,
        ),
        now=NOW,
    )
    await outbox.mark_retryable_failure(
        "del-1",
        attempted_at=NOW,
        next_attempt_at=NOW,
        last_error="transient timeout",
    )

    decrypt_calls = 0
    real_decrypt = AESGCM.decrypt

    def counting_decrypt(self, nonce, data, associated_data):
        nonlocal decrypt_calls
        decrypt_calls += 1
        return real_decrypt(self, nonce, data, associated_data)

    monkeypatch.setattr(AESGCM, "decrypt", counting_decrypt)

    await migrate_persisted_secrets()

    assert decrypt_calls == 0


async def test_api_routes_minimize_config_yaml_decryption(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "db"))
    job_store = SQLiteJobStore()
    result_store = LocalResultStore(
        str(tmp_path / "results"),
        lambda jid: job_store.get_job_metadata(jid).then() if False else _lookup_meta(job_store, jid),
    )

    async def _lookup_meta(store: SQLiteJobStore, jid: str) -> tuple[str, str]:
        if hasattr(store, "get_job_metadata"):
            meta = await store.get_job_metadata(jid)
            return meta.project, meta.name
        loaded = await store.get_job(jid)
        return loaded.project, loaded.name

    cfg = _scheduled_yaml("route-decrypt-test")
    job = Job(
        job_id="job-routes-1",
        project="acme",
        name="route-decrypt-test",
        status=JobStatus.complete,
        config_yaml=cfg,
        schedule_cron="0 9 * * *",
        schedule_timezone="UTC",
        schedule_enabled=True,
        current_run_id="run-done",
        updated_at=NOW,
    )
    await job_store.save_job(job)
    await result_store.save_result(
        "job-routes-1",
        {"job_id": "job-routes-1", "run_id": "run-done", "project": "acme", "results": [], "targets": []},
        run_id="run-done",
        status="complete",
        record_count=0,
    )

    worker_pool = AsyncMock()
    worker_pool.enqueue = AsyncMock()
    worker_pool.cancel_run = AsyncMock(
        return_value=RunCancellationResult(
            run_id="run-done",
            outcome=QueueCancellationOutcome.missing,
        )
    )
    worker_pool.inspect_delivery = AsyncMock(return_value=QueueDeliveryState.missing)
    scheduler = main_module.build_runtime_services().scheduler
    monkeypatch.setattr(scheduler, "_job_store", job_store)
    monkeypatch.setattr(scheduler, "_pool", worker_pool)
    monkeypatch.setattr(scheduler, "_result_store", result_store)

    reveal_calls = 0
    real_reveal = secret_envelope.reveal_text

    def counting_reveal(value: str, *, purpose: str) -> str:
        nonlocal reveal_calls
        reveal_calls += 1
        return real_reveal(value, purpose=purpose)

    monkeypatch.setattr(secret_envelope, "reveal_text", counting_reveal)
    monkeypatch.setattr(job_rows, "reveal_text", counting_reveal)
    monkeypatch.setattr(job_store_module, "reveal_text", counting_reveal)

    main_module.app.dependency_overrides[get_job_store] = lambda: job_store
    main_module.app.dependency_overrides[get_result_store] = lambda: result_store
    main_module.app.dependency_overrides[get_scheduler] = lambda: scheduler
    main_module.app.dependency_overrides[get_worker_pool] = lambda: worker_pool
    try:
        transport = ASGITransport(app=main_module.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            reveal_calls = 0
            res = await client.get("/results/job-routes-1")
            assert res.status_code == 200
            assert reveal_calls == 0

            reveal_calls = 0
            res = await client.post("/jobs/job-routes-1/pause")
            assert res.status_code == 200
            assert reveal_calls == 0

            reveal_calls = 0
            res = await client.post("/jobs/job-routes-1/resume")
            assert res.status_code == 200
            assert reveal_calls == 0

            reveal_calls = 0
            res = await client.post("/jobs/job-routes-1/trigger")
            assert res.status_code == 202
            assert reveal_calls == 1

            reveal_calls = 0
            res = await client.post("/jobs/job-routes-1/cancel")
            assert res.status_code == 204
            assert reveal_calls == 0

            reveal_calls = 0
            res = await client.delete("/jobs/job-routes-1?delete_results=true")
            assert res.status_code == 204
            assert reveal_calls == 0
    finally:
        main_module.app.dependency_overrides.clear()


async def test_get_job_run_state_does_not_redecrypt_cached_runs_on_subsequent_polls(
    tmp_path, monkeypatch
):
    await init_db(str(tmp_path / "db"))
    store = SQLiteJobStore()
    cfg = _scheduled_yaml("poll-cached")
    await store.save_job(
        Job(
            job_id="job-poll",
            project="acme",
            name="poll-cached",
            status=JobStatus.queued,
            config_yaml=cfg,
            current_run_id="run-poll-1",
            updated_at=NOW,
        )
    )

    reveal_calls = 0
    real_reveal = secret_envelope.reveal_text

    def counting_reveal(value: str, *, purpose: str) -> str:
        nonlocal reveal_calls
        reveal_calls += 1
        return real_reveal(value, purpose=purpose)

    monkeypatch.setattr(job_store_module, "reveal_text", counting_reveal)

    first = await store.get_job_run_state("job-poll", "run-poll-1")
    assert first is not None and first["name"] == "poll-cached"
    assert reveal_calls == 1

    second = await store.get_job_run_state("job-poll", "run-poll-1")
    third = await store.get_job_run_state("job-poll", "run-poll-1")
    assert second == first
    assert third == first
    assert reveal_calls == 1


async def test_update_scheduled_job_avoids_redecrypting_written_row(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "db"))
    store = SQLiteJobStore()
    await store.save_job(
        Job(
            job_id="job-upd",
            project="acme",
            name="before",
            config_yaml=_scheduled_yaml("before"),
            schedule_cron="0 9 * * *",
            schedule_timezone="UTC",
            schedule_enabled=True,
        )
    )

    reveal_calls = 0
    real_reveal = secret_envelope.reveal_text

    def counting_reveal(value: str, *, purpose: str) -> str:
        nonlocal reveal_calls
        reveal_calls += 1
        return real_reveal(value, purpose=purpose)

    monkeypatch.setattr(job_rows, "reveal_text", counting_reveal)

    outcome = await store.update_scheduled_job(
        "job-upd",
        project="acme",
        name="after",
        config_yaml=_scheduled_yaml("after"),
        schedule_cron="30 10 * * *",
        schedule_timezone="America/New_York",
        schedule_enabled=False,
        updated_at=NOW,
    )

    assert reveal_calls == 1
    assert outcome.current is not None
    assert outcome.current.name == "after"
    assert outcome.current.schedule_cron == "30 10 * * *"
    assert outcome.current.schedule_timezone == "America/New_York"
    assert outcome.current.schedule_enabled is False


async def test_wait_for_persisted_job_does_not_decrypt_config(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "db"))
    store = SQLiteJobStore()
    await store.save_job(
        Job(
            job_id="job-wait",
            project="acme",
            name="wait-me",
            status=JobStatus.complete,
            config_yaml=_scheduled_yaml("wait-me"),
            current_run_id="run-w",
        )
    )

    reveal_calls = 0
    real_reveal = secret_envelope.reveal_text

    def counting_reveal(value: str, *, purpose: str) -> str:
        nonlocal reveal_calls
        reveal_calls += 1
        return real_reveal(value, purpose=purpose)

    monkeypatch.setattr(job_rows, "reveal_text", counting_reveal)

    done = await wait_for_persisted_job(
        "job-wait",
        job_store=store,
        timeout_seconds=1.0,
        poll_delay_seconds=0.05,
    )

    assert done is True
    assert reveal_calls == 0


async def test_webhook_mark_last_success_only_advances_on_delivered(tmp_path):
    await init_db(str(tmp_path / "db"))
    outbox = SQLiteWebhookOutboxStore()
    LAST_SUCCESS.labels("webhook").set(0.0)

    for delivery_id in ("d-retry", "d-perm", "d-ok"):
        await outbox.enqueue_delivery(
            WebhookDeliveryCreate(
                delivery_id=delivery_id,
                job_id="job-1",
                run_id=f"run-{delivery_id}",
                event="job.complete",
                url="https://hooks.example.com/cb",
                headers={},
                timeout_seconds=5,
                payload={"delivery_id": delivery_id},
                next_attempt_at=NOW,
            ),
            now=NOW,
        )

    dispatcher = HttpWebhookDispatcher(outbox_store=outbox)

    dispatcher.send_once = AsyncMock(
        return_value=WebhookDispatchResult(
            WebhookDispatchStatus.retryable_failed,
            1,
            "502",
            WebhookDispatchReason.retryable_http_response,
        )
    )
    delivery = await outbox.get_delivery("d-retry")
    assert delivery is not None
    await dispatcher._process_delivery(delivery)
    assert LAST_SUCCESS.labels("webhook")._value.get() == 0.0

    dispatcher.send_once = AsyncMock(
        return_value=WebhookDispatchResult(
            WebhookDispatchStatus.permanent_failed,
            1,
            "400",
            WebhookDispatchReason.permanent_http_response,
        )
    )
    delivery = await outbox.get_delivery("d-perm")
    assert delivery is not None
    await dispatcher._process_delivery(delivery)
    assert LAST_SUCCESS.labels("webhook")._value.get() == 0.0

    dispatcher.send_once = AsyncMock(
        return_value=WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)
    )
    delivery = await outbox.get_delivery("d-ok")
    assert delivery is not None
    await dispatcher._process_delivery(delivery)
    assert LAST_SUCCESS.labels("webhook")._value.get() > 0.0


async def test_webhook_acquire_client_closes_evicted_client_when_factory_fails():
    first_client = MagicMock(spec=httpx.AsyncClient)
    first_client.aclose = AsyncMock()
    calls = 0

    def flaky_factory() -> httpx.AsyncClient:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first_client
        raise RuntimeError("factory boom")

    dispatcher = HttpWebhookDispatcher(
        client_factory=flaky_factory,
        client_cache_max_size=1,
    )
    client = await dispatcher._acquire_client("host-a.example")
    assert client is first_client
    await dispatcher._release_client("host-a.example", client)

    with pytest.raises(RuntimeError, match="factory boom"):
        await dispatcher._acquire_client("host-b.example")

    first_client.aclose.assert_awaited_once()
    await dispatcher.shutdown()


async def test_output_records_not_incremented_for_discarded_all_or_nothing_run(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-aon",
        file_path="/tmp/results/run-aon",
        record_count=0,
        serialized_bytes=128,
    )
    before = OUTPUT_RECORDS._value.get()

    with (
        patch("scrapeyard.queue.worker.load_config") as mock_load,
        patch("scrapeyard.queue.worker.scrape_target") as mock_scrape,
        patch("scrapeyard.queue.worker.get_settings", return_value=make_settings_mock()),
    ):
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [
            make_target("http://a.com"),
            make_target("http://b.com"),
        ]
        cfg.execution.concurrency = 1
        cfg.execution.deadline_at = None
        cfg.execution.delay_between = 0
        cfg.execution.post_target_delay = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.all_or_nothing
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.proxy = None
        cfg.webhook = None
        mock_scrape.side_effect = [
            TargetResult(url="http://a.com", status="success", data=[{"title": "A"}, {"title": "B"}]),
            TargetResult(url="http://b.com", status="failed", errors=["timeout"]),
        ]

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert OUTPUT_RECORDS._value.get() == before


def test_active_target_uses_gauge_inc_dec_without_shadow_counter():
    assert not hasattr(metrics_module, "_active_targets")
    ACTIVE_WORK.labels("targets").set(3)
    with active_target():
        assert ACTIVE_WORK.labels("targets")._value.get() == 4.0
    assert ACTIVE_WORK.labels("targets")._value.get() == 3.0
    ACTIVE_WORK.labels("targets").set(0)


async def test_pagination_records_pre_redirect_next_url_and_offloads_fingerprint(monkeypatch):
    monkeypatch.setattr(pagination, "_pagination_url_is_safe", AsyncMock(return_value=True))
    fetched_urls: list[str] = []

    class _El:
        def __init__(self, href: str) -> None:
            self.attrib = {"href": href}

    page1 = SimpleNamespace(links=[_El("https://shop.example/page-2-alias")])
    page2 = SimpleNamespace(links=[_El("https://shop.example/page-2-alias")])

    monkeypatch.setattr(
        pagination,
        "select_elements_strict",
        lambda pg, _sel, **_kw: pg.links,
    )

    async def fake_fetch(*args, **_kwargs):
        url = args[2]
        fetched_urls.append(url)
        return FetchOutcome(
            page=page2,
            debug={"final_url": "https://shop.example/canonical/2"},
        )

    target = _target(
        url="https://shop.example/canonical/1",
        fetcher=FetcherType.basic,
        pagination_cfg=PaginationConfig(next="a.next", max_pages=5),
    )
    result = TargetResult(
        url=target.url,
        data=[{"title": "1"}],
        pages_scraped=1,
        debug={"final_url": "https://shop.example/canonical/1"},
    )

    await pagination.paginate_target(
        page=page1,
        target=target,
        result=result,
        fetch_target_page=fake_fetch,
        extract_page_data=lambda _pg, _t: [{"title": "2"}],
        retry_handler=object(),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status=set(),
        adaptive_dir="",
        proxy_url=None,
        artifacts_dir=None,
    )

    assert fetched_urls == ["https://shop.example/page-2-alias"]
    assert result.pagination_stop_reason == "repeated_url"


async def test_page_param_pagination_offloads_fingerprint_via_run_cpu_work(monkeypatch):
    monkeypatch.setattr(pagination, "_pagination_url_is_safe", AsyncMock(return_value=True))
    cpu_funcs: list[object] = []
    real_run_cpu = pagination._run_cpu_work

    async def spy_run_cpu(fn, *args, **kwargs):
        cpu_funcs.append(fn)
        return await real_run_cpu(fn, *args, **kwargs)

    monkeypatch.setattr(pagination, "_run_cpu_work", spy_run_cpu)

    async def fake_fetch(*args, **_kwargs):
        url = args[2]
        return FetchOutcome(page=object(), debug={"final_url": url})

    target = _target(
        url="https://shop.example/items?page=1",
        fetcher=FetcherType.basic,
        pagination_cfg=PaginationConfig(page_param="page", max_pages=2),
    )
    result = TargetResult(
        url=target.url,
        data=[{"title": "item-1"}],
        pages_scraped=1,
        debug={"final_url": target.url},
    )

    await pagination.paginate_target(
        page=object(),
        target=target,
        result=result,
        fetch_target_page=fake_fetch,
        extract_page_data=lambda _pg, _t: [{"title": "item-2"}],
        retry_handler=object(),
        fetcher_cls=object(),
        adaptive=False,
        retryable_status=set(),
        adaptive_dir="",
        proxy_url=None,
        artifacts_dir=None,
    )

    assert pagination.page_data_fingerprint in cpu_funcs


async def test_refresh_metrics_offloads_disk_and_memory_off_event_loop(monkeypatch, tmp_path):
    loop_thread = threading.current_thread()
    observed_threads: dict[str, threading.Thread] = {}

    def spy_disk_usage(path: str):
        observed_threads["disk"] = threading.current_thread()
        return SimpleNamespace(free=123456)

    def spy_memory_headroom(limit: int):
        observed_threads["memory"] = threading.current_thread()
        return 512.0

    pool = SimpleNamespace(
        active_tasks=0,
        active_browsers=0,
        max_concurrent=2,
        max_browsers=1,
        queue_operational_snapshot=AsyncMock(return_value={"high": (0, 0.0)}),
    )
    outbox = SimpleNamespace(
        summarize=AsyncMock(return_value=WebhookOutboxSummary(0, 0, 0, None, 0, 0, 0))
    )
    settings = SimpleNamespace(
        metrics_refresh_interval_seconds=30.0,
        health_probe_timeout_seconds=2.0,
        storage_results_dir=str(tmp_path),
        workers_memory_limit_mb=1024,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.shutil, "disk_usage", spy_disk_usage)
    monkeypatch.setattr(main_module, "memory_headroom_mb", spy_memory_headroom)
    monkeypatch.setattr(main_module, "get_worker_pool", lambda: pool)
    monkeypatch.setattr(main_module.app.state, "webhook_outbox_store", outbox, raising=False)
    monkeypatch.setattr(
        main_module,
        "_background_probes",
        lambda: {"worker": ProbeResult(True)},
    )
    monkeypatch.setattr(main_module, "_metrics_refreshed_at", 0.0)

    await main_module._refresh_metrics()

    assert observed_threads["disk"] is not loop_thread
    assert observed_threads["memory"] is not loop_thread


async def test_result_store_offloads_lstat_exists_unlink_rmdir(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "db"))
    loop_thread = threading.current_thread()
    off_loop_checks: list[bool] = []

    async def lookup(_job_id: str) -> tuple[str, str]:
        return "acme", "job"

    store = LocalResultStore(str(tmp_path / "results"), lookup)
    real_checked = store._checked_result_dir

    def spy_checked(file_path: str) -> Path:
        off_loop_checks.append(threading.current_thread() is not loop_thread)
        return real_checked(file_path)

    monkeypatch.setattr(store, "_checked_result_dir", spy_checked)

    await store.save_result("j-1", {"ok": True}, run_id="r-1", record_count=1)
    await store.get_result("j-1", run_id="r-1")
    await store.delete_result("j-1", "r-1")

    assert off_loop_checks
    assert all(off_loop_checks)


async def test_webhook_acquire_client_closes_evicted_client_when_uniqueness_fails():
    first_client = MagicMock(spec=httpx.AsyncClient)
    first_client.aclose = AsyncMock()
    second_client = MagicMock(spec=httpx.AsyncClient)
    second_client.aclose = AsyncMock()
    returns = iter([first_client, second_client, second_client])

    dispatcher = HttpWebhookDispatcher(
        client_factory=lambda: next(returns),
        client_cache_max_size=2,
    )
    c1 = await dispatcher._acquire_client("host-a.example")
    c2 = await dispatcher._acquire_client("host-b.example")
    await dispatcher._release_client("host-a.example", c1)

    with pytest.raises(RuntimeError, match="distinct client"):
        await dispatcher._acquire_client("host-c.example")

    first_client.aclose.assert_awaited_once()
    await dispatcher._release_client("host-b.example", c2)
    await dispatcher.shutdown()


async def test_output_records_increments_on_durable_persistence(mock_stores):
    job_store, result_store, error_store, circuit_breaker = mock_stores
    job_store.get_job = AsyncMock(return_value=make_job())
    result_store.save_result.return_value = SaveResultMeta(
        run_id="run-ok",
        file_path="/tmp/results/run-ok",
        record_count=2,
        serialized_bytes=128,
    )
    before = OUTPUT_RECORDS._value.get()

    with (
        patch("scrapeyard.queue.worker.load_config") as mock_load,
        patch(
            "scrapeyard.queue.worker.scrape_target",
            return_value=TargetResult(
                url="http://a.com",
                status="success",
                data=[{"title": "A"}, {"title": "B"}],
            ),
        ),
        patch("scrapeyard.queue.worker.get_settings", return_value=make_settings_mock()),
    ):
        cfg = mock_load.return_value
        cfg.project = "test"
        cfg.name = "test-job"
        cfg.resolved_targets.return_value = [make_target("http://a.com")]
        cfg.execution.concurrency = 1
        cfg.execution.deadline_at = None
        cfg.execution.delay_between = 0
        cfg.execution.post_target_delay = 0
        cfg.execution.domain_rate_limit = 0
        cfg.execution.fail_strategy = FailStrategy.partial
        cfg.adaptive = False
        cfg.schedule = None
        cfg.retry = MagicMock()
        cfg.validation = MagicMock(required_fields=[], min_results=0, on_empty="warn")
        cfg.output.group_by = "target"
        cfg.proxy = None
        cfg.webhook = None

        await scrape_task(
            "job-1",
            "yaml",
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            circuit_breaker=circuit_breaker,
            rate_limiter=LocalDomainRateLimiter(),
        )

    assert OUTPUT_RECORDS._value.get() == before + 2


async def test_readiness_probe_executor_shuts_down_and_recreates():
    executor = main_module._ensure_readiness_probe_executor()
    assert not executor._shutdown
    main_module._shutdown_readiness_probe_executor()
    assert executor._shutdown
    recreated = main_module._ensure_readiness_probe_executor()
    assert recreated is not executor
    assert not recreated._shutdown

