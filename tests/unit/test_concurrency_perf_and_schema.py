"""Reproduce-first regression tests for WO-S2 concurrency, perf, and schema fixes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import scrapeyard.api.middleware as middleware_module
import scrapeyard.engine.detection as detection_module
import scrapeyard.engine.selectors as selectors_module
import scrapeyard.engine.url_guard as url_guard_module
import scrapeyard.queue.worker_output as worker_output_module
import scrapeyard.storage.webhook_outbox as outbox_module
from scrapeyard.api.middleware import RateLimitMiddleware
from scrapeyard.api.response_models import ResultResponse
from scrapeyard.api.serializers import serialize_result_response
from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.config.schema import (
    GroupBy,
    MapDetectionConfig,
    ScrapeConfig,
    StockDetectionConfig,
    StockPatternConfig,
)
from scrapeyard.engine.asset_cache import AssetCache
from scrapeyard.engine.domain_guard import LocalDomainGuard
from scrapeyard.engine.resilience import CircuitBreaker, CircuitState
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import (
    DatabaseManager,
    close_db,
    db_transaction,
    get_db,
    init_db,
)
from scrapeyard.storage.job_queries import build_list_jobs_with_stats_query
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.result_store import LocalResultStore
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookDeliveryCreate,
)
from scrapeyard.webhook.dispatcher import HttpWebhookDispatcher
from tests.factories import seed_webhook_delivery


def _make_job(
    job_id: str = "job-1",
    *,
    project: str = "acme",
    name: str = "prices",
    current_run_id: str | None = "run-1",
) -> Job:
    return Job(
        job_id=job_id,
        project=project,
        name=name,
        status=JobStatus.queued,
        config_yaml=f"project: {project}\nname: {name}\ntarget:\n  url: https://example.com\n  selectors:\n    title: h1\n",
        current_run_id=current_run_id,
        current_trigger="adhoc",
    )


async def test_concurrent_reads_do_not_block_behind_slow_writer_transaction(
    tmp_path: Any,
) -> None:
    await init_db(str(tmp_path / "db"))
    try:
        store = SQLiteJobStore()
        outbox = SQLiteWebhookOutboxStore()
        job = _make_job()
        await store.save_job(job)
        now = datetime.now(timezone.utc)
        config_hash = hashlib.sha256(job.config_yaml.encode("utf-8")).hexdigest()
        assert await store.claim_run("run-1", "job-1", "adhoc", config_hash, now)

        writer_locked = asyncio.Event()
        release_writer = asyncio.Event()

        async def slow_writer() -> None:
            async with get_db("jobs.db") as db, db_transaction(db, immediate=True):
                await db.execute(
                    "UPDATE jobs SET updated_at = ? WHERE job_id = ?",
                    (now.isoformat(), "job-1"),
                )
                writer_locked.set()
                await release_writer.wait()

        writer_task = asyncio.create_task(slow_writer())
        try:
            await asyncio.wait_for(writer_locked.wait(), timeout=2.0)

            metadata, active, run_state, jobs_with_stats, summary = await asyncio.wait_for(
                asyncio.gather(
                    store.get_job_metadata("job-1"),
                    store.run_is_active("job-1", "run-1"),
                    store.get_job_run_state("job-1", "run-1"),
                    store.list_jobs_with_stats("acme"),
                    outbox.summarize(now=now),
                ),
                timeout=1.0,
            )

            assert metadata.job_id == "job-1"
            assert active is True
            assert run_state is not None and run_state["run_id"] == "run-1"
            assert len(jobs_with_stats) == 1
            assert summary.pending == 0

            async with get_db("jobs.db", readonly=True) as ro_db:
                with pytest.raises(sqlite3.OperationalError):
                    await ro_db.execute("UPDATE jobs SET status = 'failed' WHERE job_id = 'job-1'")
        finally:
            release_writer.set()
            await writer_task
    finally:
        await close_db()


@pytest.mark.parametrize("readonly", [False, True])
async def test_database_manager_close_waits_safely_for_active_borrower(
    tmp_path: Any,
    readonly: bool,
) -> None:
    manager = DatabaseManager()
    await manager.init(str(tmp_path / "db"))

    entered = asyncio.Event()
    release = asyncio.Event()

    async def borrower() -> int:
        async with manager.get("jobs.db", readonly=readonly) as db:
            first = await (await db.execute("SELECT 1")).fetchone()
            assert first is not None and first[0] == 1
            entered.set()
            await release.wait()
            second = await (await db.execute("SELECT 2")).fetchone()
            assert second is not None
            return int(second[0])

    borrow_task = asyncio.create_task(borrower())
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    close_task = asyncio.create_task(manager.close())
    await asyncio.sleep(0.02)
    assert not close_task.done(), "close() must not close a connection while a borrower is mid-query"

    release.set()
    assert await asyncio.wait_for(borrow_task, timeout=2.0) == 2
    await asyncio.wait_for(close_task, timeout=2.0)


async def test_completing_one_of_50_due_webhooks_does_not_redecrypt_other_49(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db(str(tmp_path / "db"))
    try:
        outbox = SQLiteWebhookOutboxStore()
        now = datetime.now(timezone.utc)
        for index in range(50):
            delivery_id = f"del-{index:03d}"
            await seed_webhook_delivery(
                WebhookDeliveryCreate(
                    delivery_id=delivery_id,
                    job_id=f"job-{index:03d}",
                    run_id=f"run-{index:03d}",
                    event="job.complete",
                    url="https://hooks.example.com/hook",
                    headers={"Authorization": "Bearer secret"},
                    timeout_seconds=5.0,
                    payload={"delivery_id": delivery_id, "job_id": f"job-{index:03d}"},
                    next_attempt_at=now - timedelta(seconds=100 - index),
                ),
                now=now - timedelta(seconds=100 - index),
            )

        revealed_purposes: list[str] = []
        real_reveal = outbox_module.reveal_text

        def counting_reveal(value: str, *, purpose: str) -> str:
            revealed_purposes.append(purpose)
            return real_reveal(value, purpose=purpose)

        monkeypatch.setattr(outbox_module, "reveal_text", counting_reveal)

        first_done = asyncio.Event()
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        sent_ids: list[str] = []

        async def fake_send(_config: Any, payload: dict[str, Any]) -> Any:
            from scrapeyard.webhook.dispatcher import (
                WebhookDispatchResult,
                WebhookDispatchStatus,
            )

            delivery_id = str(payload["delivery_id"])
            sent_ids.append(delivery_id)
            if len(sent_ids) == 1:
                first_done.set()
                return WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)
            second_started.set()
            await release_second.wait()
            return WebhookDispatchResult(WebhookDispatchStatus.delivered, 1)

        dispatcher = HttpWebhookDispatcher(
            outbox_store=outbox,
            dispatch_concurrency=1,
            dispatch_batch_size=100,
        )
        dispatcher.send_once = fake_send  # type: ignore[method-assign]

        await dispatcher.startup()
        try:
            await asyncio.wait_for(first_done.wait(), timeout=2.0)
            await asyncio.wait_for(second_started.wait(), timeout=2.0)
            await dispatcher.notify()
            await asyncio.sleep(0.05)

            decrypted_ids = {purpose.split(":", 1)[1] for purpose in revealed_purposes}
            assert decrypted_ids <= set(sent_ids), (
                f"Expected only claimed deliveries {sent_ids} to be decrypted, "
                f"but decrypted {len(decrypted_ids)} deliveries"
            )
            assert len(decrypted_ids) <= 2
        finally:
            release_second.set()
            await dispatcher.shutdown(timeout=2.0)
    finally:
        await close_db()


async def test_rate_limit_middleware_prunes_expired_keys_at_max_keys_and_admits_new_caller() -> None:
    now = [100.0]

    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(
        RateLimitMiddleware,
        requests=10,
        window_seconds=10.0,
        clock=lambda: now[0],
        max_keys=2,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Fill bucket 1 at t=100 (_last_prune_at becomes 100.0).
        transport.client = ("10.0.0.1", 1234)
        assert (await client.get("/limited")).status_code == 200
        # Fill bucket 2 at t=102.
        now[0] = 102.0
        transport.client = ("10.0.0.2", 1234)
        assert (await client.get("/limited")).status_code == 200
        # At t=110, periodic prune runs (_last_prune_at=110.0), expiring 10.0.0.1; add 10.0.0.3.
        now[0] = 110.0
        transport.client = ("10.0.0.3", 1234)
        assert (await client.get("/limited")).status_code == 200
        # Now at t=113 (< 110 + 10 = 120 so periodic prune will not run), 10.0.0.2@102 is expired.
        now[0] = 113.0
        transport.client = ("10.0.0.4", 1234)
        response = await client.get("/limited")

    assert response.status_code == 200


async def test_rate_limit_middleware_precomputes_api_key_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    middleware = RateLimitMiddleware(
        app,
        requests=1,
        window_seconds=10.0,
        api_keys={"precomputed-secret"},
        clock=lambda: now[0],
    )

    def forbidden_sha256(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("RateLimitMiddleware must not hash API keys per request")

    monkeypatch.setattr(middleware_module.hashlib, "sha256", forbidden_sha256)

    transport = ASGITransport(app=middleware)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/limited", headers={"X-API-Key": "precomputed-secret"})
        second = await client.get("/limited", headers={"X-API-Key": "precomputed-secret"})

    assert first.status_code == 200
    assert second.status_code == 429


async def test_asset_cache_rejects_oversized_headers_without_evicting_valid_entries() -> None:
    def make_req(url: str) -> Any:
        return SimpleNamespace(url=url, method="GET", resource_type="script")

    def make_resp(url: str, body: bytes, headers: dict[str, str] | None = None) -> Any:
        all_hdrs = {"cache-control": "public, max-age=600", **(headers or {})}

        async def all_headers() -> dict[str, str]:
            return all_hdrs

        async def read_body() -> bytes:
            return body

        return SimpleNamespace(
            status=200,
            request=make_req(url),
            all_headers=all_headers,
            body=read_body,
        )

    cache = AssetCache(max_bytes=10_000, max_entry_bytes=8 * 1024 * 1024)
    small_url = "https://www.example.test/small.js"
    huge_url = "https://www.example.test/huge.js"

    await cache.store("example.test", make_resp(small_url, b"console.log(1);"))
    assert cache.lookup("example.test", make_req(small_url)) is not None
    before_size = cache.size

    await cache.store("example.test", make_resp(huge_url, b"", headers={"x-huge": "h" * 20_000}))

    assert cache.lookup("example.test", make_req(small_url)) is not None
    assert cache.lookup("example.test", make_req(huge_url)) is None
    assert cache.size == before_size

    wide_url = "https://www.example.test/" + ("\U0001f600" * 500)
    wide_cache = AssetCache(max_bytes=1_000_000)
    await wide_cache.store("example.test", make_resp(wide_url, b""))
    wide_asset = wide_cache.lookup("example.test", make_req(wide_url))
    assert wide_asset is not None
    assert wide_asset.size >= sys.getsizeof(wide_url)


async def test_local_domain_guard_prunes_stale_days_and_bounds_domain_count() -> None:
    now = [1_000.0]
    day = ["20260101"]
    guard = LocalDomainGuard(clock=lambda: now[0], day=lambda: day[0], max_domains=5)

    for index in range(12):
        await guard.consume_page(f"host-{index}.example", 10)
        assert len(guard._pages) <= 5
        await guard.start_cooldown(f"host-{index}.example", 60)
        assert len(guard._cooldowns) <= 5

    day[0] = "20260102"
    await guard.consume_page("fresh.example", 10)
    assert all(recorded_day == "20260102" for _host, recorded_day in guard._pages)
    assert list(guard._pages) == [("fresh.example", "20260102")]


def test_circuit_breaker_does_not_scan_all_domains_on_every_check() -> None:
    class CountingDict(dict[str, Any]):
        def __init__(self) -> None:
            super().__init__()
            self.items_calls = 0

        def items(self) -> Any:
            self.items_calls += 1
            return super().items()

    clock = [100.0]
    cb = CircuitBreaker(
        max_consecutive_failures=3,
        cooldown_seconds=60,
        max_domains=1000,
        inactive_ttl_seconds=3600.0,
        clock=lambda: clock[0],
    )
    for index in range(200):
        cb.record_failure(f"host-{index}.example")

    tracked = CountingDict()
    tracked.update(cb._circuits)
    cb._circuits = tracked

    for index in range(50):
        clock[0] = 100.0 + index * 0.1
        assert cb.check("clean.example") is None

    assert tracked.items_calls == 0

    clock[0] = 100.0 + 3700.0
    assert cb.check("clean.example") is None
    assert tracked.items_calls == 1
    assert len(cb._circuits) == 0
    assert cb.state("host-0.example") is CircuitState.closed


@pytest.mark.parametrize("group_by", [GroupBy.target, GroupBy.merge])
def test_actual_worker_payload_validates_against_result_response_strict_schema(
    group_by: GroupBy,
) -> None:
    from scrapeyard.api import result_responses as rr_module

    assert hasattr(rr_module, "EncodedResultResponse")
    assert not hasattr(rr_module, "ResultResponse")

    secret = "SecretValue/42"
    config = ScrapeConfig.model_validate(
        {
            "project": "acme",
            "name": "catalog",
            "output": {"group_by": group_by.value},
            "execution": {"page_cache": "replay"},
            "target": {
                "url": "https://shop.example/items",
                "selectors": {"sku": ".sku"},
            },
        }
    )
    config._resolved_secret_values = (secret,)
    result = TargetResult(
        url="https://shop.example/items",
        status="success",
        data=[{"sku": f"token-{secret}"}],
        pages_scraped=2,
        pagination_stop_reason="exhausted",
        recorded_at="2026-11-25T00:00:00+00:00",
    )
    budget = RunBudget(
        max_duration_seconds=60,
        max_fetched_bytes=1_000_000,
        max_extracted_records=100,
        max_serialized_result_bytes=1_000_000,
        max_browser_debug_bytes=1_000,
    )

    worker_payload = worker_output_module.materialize_output_artifact(
        config,
        [result],
        "job-1",
        JobStatus.complete,
        [],
        publish_results=True,
        budget=budget,
    )
    worker_payload["run_budget"] = budget.snapshot()
    worker_payload["page_cache"] = "replay"
    worker_payload["run_id"] = "run-1"
    worker_payload["config_hash"] = "a" * 64

    validated_worker = ResultResponse.model_validate(worker_payload)
    assert validated_worker.targets[0].pagination is not None
    assert validated_worker.targets[0].pagination.stop_reason == "exhausted"
    assert validated_worker.targets[0].pagination.exhausted is True
    assert validated_worker.result_redaction is not None
    assert validated_worker.result_redaction.deployment_secret_matches == 1

    api_payload = serialize_result_response(
        "job-1",
        run_id="run-1",
        status="complete",
        artifact=worker_payload,
    )
    validated_api = ResultResponse.model_validate(api_payload)
    assert validated_api.result_redaction is not None
    assert validated_api.result_redaction.deployment_secret_matches == 1
    assert validated_api.targets[0].pagination is not None
    assert validated_api.targets[0].pagination.exhausted is True


def test_detection_extracts_element_text_once_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_extract = detection_module.element_text_content

    def counting_extract(element: object) -> str:
        nonlocal calls
        calls += 1
        return real_extract(element)

    monkeypatch.setattr(detection_module, "element_text_content", counting_extract)

    el = MagicMock()
    el.text = "Add to Cart to See Price - In Stock"
    el.css.return_value = []
    item: dict[str, Any] = {"name": "Widget", "price": None}
    map_cfg = MapDetectionConfig(text_patterns=["add to cart to see price"])
    stock_cfg = StockDetectionConfig(in_stock=StockPatternConfig(text_patterns=["in stock"]))

    detection_module.enrich_item_detection(item, el, map_cfg, stock_cfg)

    assert item["pricing_visibility"] == "map"
    assert item["display_price_text"] == "Add to Cart to See Price"
    assert item["stock_status"] == "in_stock"
    assert calls == 1


def test_redact_sensitive_mapping_deep_nesting_and_single_secret_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested: dict[str, Any] = {"secret": "super-secret-value"}
    for _ in range(1500):
        nested = {"child": nested}

    redacted_deep = url_guard_module.redact_sensitive_mapping(nested)
    assert "super-secret-value" not in json.dumps(redacted_deep)

    redact_calls = 0
    real_redact = url_guard_module._redact_variants

    def counting_redact(text: str, variants: tuple[str, ...]) -> tuple[str, int]:
        nonlocal redact_calls
        redact_calls += 1
        return real_redact(text, variants)

    monkeypatch.setattr(url_guard_module, "_redact_variants", counting_redact)

    token = url_guard_module.activate_deployment_secret_redaction(("top-secret-99",))
    try:
        out = url_guard_module.redact_sensitive_mapping(
            {"url": "https://user:pass@example.com/top-secret-99?k=v"},
            secret_values=("top-secret-99",),
        )
        assert out == {"url": "https://example.com/<redacted>?k=<redacted>"}
        # At most 1 pass for the key ("url") and 1 pass for the value string — never 3x for the value.
        assert redact_calls <= 2

        redact_calls = 0
        out_text = url_guard_module.redact_userinfo_in_text(
            "hit https://user:pass@example.com/top-secret-99?k=v"
        )
        assert out_text == "hit https://example.com/<redacted>?k=<redacted>"
        assert redact_calls == 1
    finally:
        url_guard_module.reset_deployment_secret_redaction(token)


async def test_worker_skips_redundant_full_graph_compact_json_size_and_store_enforces_budget(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_db(str(tmp_path / "db"))
    try:
        job_store = SQLiteJobStore()
        job = _make_job()
        await job_store.save_job(job)

        full_graph_calls = 0
        real_size = worker_output_module.compact_json_size

        def spy_compact_json_size(data: Any) -> int:
            nonlocal full_graph_calls
            if isinstance(data, dict) and "results" in data and "targets" in data:
                full_graph_calls += 1
            return real_size(data)

        config = ScrapeConfig.model_validate(
            {
                "project": "acme",
                "name": "prices",
                "target": {"url": "https://shop.example/items", "selectors": {"sku": ".sku"}},
            }
        )
        records = [{"sku": f"item-{i}", "payload": "x" * 100} for i in range(30)]
        result = TargetResult(url="https://shop.example/items", status="success", data=records)
        budget = RunBudget(
            max_duration_seconds=60,
            max_fetched_bytes=1_000_000,
            max_extracted_records=100,
            max_serialized_result_bytes=100_000,
            max_browser_debug_bytes=1_000,
        )

        payload = worker_output_module.materialize_output_artifact(
            config,
            [result],
            "job-1",
            JobStatus.complete,
            [],
            publish_results=True,
            budget=budget,
            size_fn=spy_compact_json_size,
        )
        assert full_graph_calls == 0

        tight_budget = RunBudget(
            max_duration_seconds=60,
            max_fetched_bytes=1_000_000,
            max_extracted_records=100,
            max_serialized_result_bytes=500,
            max_browser_debug_bytes=1_000,
        )
        result_store = LocalResultStore(
            results_dir=str(tmp_path / "results"),
            job_lookup=lambda _job_id: asyncio.sleep(0, result=("acme", "prices")),
            max_serialized_result_bytes=100_000,
        )
        with pytest.raises(BudgetExceeded) as exc_info:
            await result_store.save_result(
                "job-1",
                payload,
                run_id="run-tight",
                budget=tight_budget,
            )
        assert exc_info.value.limit_name is BudgetLimitName.serialized_result_bytes
    finally:
        await close_db()


def test_selectors_checks_element_value_size_once_without_transforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    real_check = selectors_module.checked_selector_value_size

    def counting_check(value: str, *, limit: int | None = None) -> int:
        nonlocal calls
        calls += 1
        return real_check(value, limit=limit)

    monkeypatch.setattr(selectors_module, "checked_selector_value_size", counting_check)

    page = SimpleNamespace(css=lambda _q: [SimpleNamespace(text="hello")])
    result = selectors_module.extract_selectors_strict(page, {"title": "h1"})
    assert result == {"title": "hello"}
    assert calls == 1


async def test_migration_026_drops_idx_jobs_project_and_uses_unique_index(
    tmp_path: Any,
) -> None:
    db_dir = str(tmp_path / "db")
    await init_db(db_dir)
    try:
        async with get_db("jobs.db") as db:
            migration_row = await (
                await db.execute(
                    "SELECT filename FROM schema_migrations WHERE migration_id = '026'"
                )
            ).fetchone()
            assert migration_row is not None
            assert migration_row[0] == "026_drop_redundant_jobs_project_index.sql"

            index_row = await (
                await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'idx_jobs_project'"
                )
            ).fetchone()
            assert index_row is None

            sql, params = build_list_jobs_with_stats_query("acme", limit=10, offset=0)
            plan_rows = await (await db.execute(f"EXPLAIN QUERY PLAN {sql}", params)).fetchall()
            plan = " ".join(str(row[3]) for row in plan_rows)
            assert "sqlite_autoindex_jobs_2 (project=?)" in plan
            assert "SCAN jobs" not in plan
            index_cols = [
                str(row[2])
                for row in await (
                    await db.execute("PRAGMA index_info('sqlite_autoindex_jobs_2')")
                ).fetchall()
            ]
            assert index_cols == ["project", "name"]
    finally:
        await close_db()
