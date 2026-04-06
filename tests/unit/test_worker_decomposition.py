from __future__ import annotations

from unittest.mock import MagicMock

from scrapeyard.engine.scraper import TargetResult
from scrapeyard.queue.worker import _collect_result_payload, _resolve_target_runtime_context


def test_collect_result_payload_flattens_data_and_errors_in_order():
    results = [
        TargetResult(
            url="https://a.example",
            status="success",
            data=[{"sku": "a1"}, {"sku": "a2"}],
            errors=["warn-a"],
        ),
        TargetResult(
            url="https://b.example",
            status="failed",
            data=[],
            errors=["err-b1", "err-b2"],
        ),
    ]

    flat_data, all_errors = _collect_result_payload(results)

    assert flat_data == [{"sku": "a1"}, {"sku": "a2"}]
    assert all_errors == ["warn-a", "err-b1", "err-b2"]


def test_resolve_target_runtime_context_uses_explicit_adaptive_override_and_proxy():
    target_cfg = MagicMock(url="https://shop.example/products", proxy=None)
    config = MagicMock()
    config.adaptive = True
    config.schedule = None
    config.proxy = None
    settings = MagicMock(proxy_url="http://service-proxy:8080")

    context = _resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir="/tmp/artifacts",
    )

    assert context.domain == "shop.example"
    assert context.adaptive is True
    assert context.proxy_url == "http://service-proxy:8080"
    assert context.artifacts_dir == "/tmp/artifacts/shop.example"


def test_resolve_target_runtime_context_enables_adaptive_for_scheduled_jobs_when_unspecified():
    target_cfg = MagicMock(url="https://shop.example/products", proxy=None)
    config = MagicMock()
    config.adaptive = None
    config.schedule = MagicMock()
    config.proxy = None
    settings = MagicMock(proxy_url="")

    context = _resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir=None,
    )

    assert context.domain == "shop.example"
    assert context.adaptive is True
    assert context.proxy_url is None
    assert context.artifacts_dir is None
