from __future__ import annotations

from unittest.mock import MagicMock

from scrapeyard.config.schema import GroupBy
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.models.job import JobStatus
from scrapeyard.queue.target_execution import resolve_target_runtime_context
from scrapeyard.queue.worker import _collect_result_payload, _format_output


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

    context = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir="/tmp/artifacts",
    )

    assert context.domain == "shop.example"
    assert context.adaptive is True
    assert context.proxy_url == "http://service-proxy:8080"
    assert context.artifacts_dir == "/tmp/artifacts/shop.example"


def test_resolve_target_runtime_context_strips_userinfo_from_domain():
    target_cfg = MagicMock(url="https://user:pass@shop.example:8443/products", proxy=None)
    config = MagicMock(adaptive=False, schedule=None, proxy=None)
    settings = MagicMock(proxy_url="")

    context = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir="/tmp/artifacts",
    )

    assert context.domain == "shop.example:8443"
    assert context.artifacts_dir == "/tmp/artifacts/shop.example:8443"


def test_resolve_target_runtime_context_enables_adaptive_for_scheduled_jobs_when_unspecified():
    target_cfg = MagicMock(url="https://shop.example/products", proxy=None)
    config = MagicMock()
    config.adaptive = None
    config.schedule = MagicMock()
    config.proxy = None
    settings = MagicMock(proxy_url="")

    context = resolve_target_runtime_context(
        target_cfg=target_cfg,
        config=config,
        settings=settings,
        run_artifacts_dir=None,
    )

    assert context.domain == "shop.example"
    assert context.adaptive is True
    assert context.proxy_url is None
    assert context.artifacts_dir is None


def test_format_output_merges_results_with_source_domains_and_target_metadata():
    config = MagicMock(project="test", name="job")
    config.output.group_by = GroupBy.merge
    results = [
        TargetResult(url="https://a.example/products", status="success", data=[{"sku": "a1"}], errors=[], pages_scraped=1),
        TargetResult(url="https://b.example/products", status="failed", data=["raw-item"], errors=["boom"], pages_scraped=1),
    ]

    payload = _format_output(
        config,
        results,
        [{"sku": "a1"}, "raw-item"],
        "job-1",
        JobStatus.partial,
        ["boom"],
    )

    assert payload["status"] == "partial"
    assert payload["targets"] == [
        {
            "url": "https://a.example/products",
            "status": "success",
            "count": 1,
            "pages_scraped": 1,
            "error_type": None,
            "error_detail": None,
            "errors": [],
            "debug": None,
        },
        {
            "url": "https://b.example/products",
            "status": "failed",
            "count": 1,
            "pages_scraped": 1,
            "error_type": None,
            "error_detail": None,
            "errors": ["boom"],
            "debug": None,
        },
    ]
    assert payload["results"] == [{"sku": "a1", "_source": "a.example"}, "raw-item"]
    assert results[0].data == [{"sku": "a1"}]


def test_format_output_groups_results_by_domain_without_mutating_group_items():
    config = MagicMock(project="test", name="job")
    config.output.group_by = "target"
    first_item = {"sku": "a1"}
    second_item = {"sku": "b1"}
    results = [
        TargetResult(url="https://a.example/products", status="success", data=[first_item], errors=[]),
        TargetResult(url="https://b.example/products", status="failed", data=[second_item], errors=["boom"]),
    ]

    payload = _format_output(
        config,
        results,
        [first_item, second_item],
        "job-1",
        JobStatus.partial,
        ["boom"],
    )

    assert payload["results"] == {
        "a.example": {
            "status": "success",
            "count": 1,
            "data": [first_item],
            "debug": None,
            "error_type": None,
            "error_detail": None,
        },
        "b.example": {
            "status": "failed",
            "count": 1,
            "data": [second_item],
            "debug": None,
            "error_type": None,
            "error_detail": None,
        },
    }
    assert first_item == {"sku": "a1"}
    assert second_item == {"sku": "b1"}


def test_format_output_keeps_same_domain_targets_separate():
    config = MagicMock(project="test", name="job")
    config.output.group_by = "target"
    results = [
        TargetResult(url="https://shop.example/products/a", status="success", data=[{"sku": "a"}]),
        TargetResult(url="https://shop.example/products/b", status="success", data=[{"sku": "b"}]),
    ]

    payload = _format_output(
        config,
        results,
        [{"sku": "a"}, {"sku": "b"}],
        "job-1",
        JobStatus.complete,
        [],
    )

    assert payload["results"]["shop.example"]["data"] == [{"sku": "a"}]
    assert payload["results"]["shop.example#2"]["data"] == [{"sku": "b"}]


def test_format_output_redacts_url_userinfo_from_metadata_and_debug():
    config = MagicMock(project="test", name="job")
    config.output.group_by = GroupBy.merge
    result = TargetResult(
        url="https://user:pass@example.com:8443/products",
        status="failed",
        data=[{"sku": "a1"}],
        errors=["failed at https://user:pass@example.com/private"],
        error_detail="redirected to https://user:pass@example.com/private",
        debug={
            "final_url": "https://user:pass@example.com/private",
            "headers": {"Authorization": "Bearer secret"},
        },
    )

    payload = _format_output(
        config,
        [result],
        [{"sku": "a1"}],
        "job-1",
        JobStatus.failed,
        result.errors,
    )

    assert payload["targets"][0]["url"] == "https://example.com:8443/products"
    assert payload["targets"][0]["error_detail"] == "redirected to https://example.com/private"
    assert payload["targets"][0]["errors"] == ["failed at https://example.com/private"]
    assert payload["targets"][0]["debug"]["final_url"] == "https://example.com/private"
    assert payload["targets"][0]["debug"]["headers"]["Authorization"] == "<redacted>"
    assert payload["results"] == [{"sku": "a1", "_source": "example.com:8443"}]
