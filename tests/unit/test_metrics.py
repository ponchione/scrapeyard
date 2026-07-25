from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import scrapeyard.main as main_module
from scrapeyard.api.middleware import MetricsMiddleware
from scrapeyard.runtime.health import ProbeResult
from scrapeyard.runtime.metrics import API_REQUESTS, render_metrics, set_cleanup_backlog
from scrapeyard.storage.types import CleanupBacklogSnapshot
from scrapeyard.storage.webhook_outbox import WebhookOutboxSummary


async def test_metrics_endpoint_exports_prometheus_text(monkeypatch):
    refresh = AsyncMock()
    monkeypatch.setattr(main_module, "_refresh_metrics", refresh)
    transport = ASGITransport(app=main_module.app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "scrapeyard_api_requests_total" in response.text
    refresh.assert_awaited_once()


async def test_durable_metric_refresh_is_cached_and_bounded(monkeypatch, tmp_path):
    pool = SimpleNamespace(
        active_tasks=2,
        active_browsers=1,
        max_concurrent=4,
        max_browsers=2,
        queue_operational_snapshot=AsyncMock(
            return_value={
                "high": (1, 3.0, 0.0),
                "normal": (2, 4.0, 1.5),
                "low": (0, 0.0, 0.0),
            }
        ),
    )
    outbox = SimpleNamespace(
        summarize=AsyncMock(
            return_value=WebhookOutboxSummary(3, 5, 1, 9.5, 0, 2, 4)
        )
    )
    settings = SimpleNamespace(
        metrics_refresh_interval_seconds=30.0,
        health_probe_timeout_seconds=0.5,
        storage_results_dir=str(tmp_path),
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module, "get_worker_pool", lambda: pool)
    monkeypatch.setattr(main_module.app.state, "webhook_outbox_store", outbox, raising=False)
    monkeypatch.setattr(
        main_module,
        "_background_probes",
        lambda: {
            name: ProbeResult(True)
            for name in ("worker", "scheduler", "cleanup", "webhook")
        },
    )
    monkeypatch.setattr(main_module, "_metrics_refreshed_at", 0.0)

    await main_module._refresh_metrics()
    await main_module._refresh_metrics()

    pool.queue_operational_snapshot.assert_awaited_once()
    outbox.summarize.assert_awaited_once()
    rendered = render_metrics().decode()
    assert 'scrapeyard_queue_depth{priority="normal"} 2.0' in rendered
    assert (
        'scrapeyard_queue_clock_rollback_offset_seconds{priority="normal"} 1.5'
        in rendered
    )
    assert 'scrapeyard_webhook_deliveries{status="pending"} 3.0' in rendered


async def test_api_metric_labels_remain_bounded_under_many_resource_ids():
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get("/items/{item_id}")
    async def _item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    transport = ASGITransport(app=app)
    started = time.perf_counter()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for index in range(250):
            response = await client.get(f"/items/private-resource-{index}")
            assert response.status_code == 200
    elapsed = time.perf_counter() - started

    samples = [
        sample
        for family in API_REQUESTS.collect()
        for sample in family.samples
        if sample.name == "scrapeyard_api_requests_total"
        and sample.labels.get("route") == "/items/{item_id}"
    ]
    assert len(samples) == 1
    assert samples[0].value >= 250
    assert "private-resource-249" not in render_metrics().decode()
    assert elapsed < 5.0


def test_cleanup_backlog_metrics_publish_only_fixed_categories():
    observed_at = datetime(2026, 7, 15, tzinfo=timezone.utc)
    set_cleanup_backlog(
        {
            "expired_results": CleanupBacklogSnapshot(
                12,
                observed_at - timedelta(hours=3),
            ),
            "unbounded-user-value": CleanupBacklogSnapshot(99, observed_at),
        },
        observed_at=observed_at,
    )

    rendered = render_metrics().decode()
    assert 'scrapeyard_cleanup_eligible_items{category="expired_results"} 12.0' in rendered
    assert (
        'scrapeyard_cleanup_oldest_eligible_age_seconds{category="expired_results"} '
        "10800.0"
    ) in rendered
    assert "unbounded-user-value" not in rendered
