from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from scrapeyard.api.dependencies import get_job_store, get_result_store, get_worker_pool
from scrapeyard.common.time import utc_now
from scrapeyard.engine.scraper import TargetResult
from tests.integration.conftest import poll_until_ready


def _yaml(*, mode: str = "async", group_by: str = "target") -> str:
    return f"""
project: api-contract
name: response-contract
execution:
  mode: {mode}
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
output:
  group_by: {group_by}
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


async def _success(*_args, **_kwargs):
    return TargetResult(
        url="https://example.com",
        status="success",
        data=[{"title": "Contract"}],
        pages_scraped=1,
    )


def _atomic_yaml(*, group_by: str) -> str:
    return f"""
project: api-contract
name: atomic-response-contract
execution:
  mode: async
  concurrency: 2
  delay_between: 0
  domain_rate_limit: 0
  fail_strategy: all_or_nothing
output:
  group_by: {group_by}
targets:
  - url: https://example.com/ok
    selectors:
      title: h1
  - url: https://example.com/fail
    selectors:
      title: h1
"""


async def _mixed_atomic(target, *_args, **_kwargs):
    if target.url.endswith("/ok"):
        return TargetResult(
            url=target.url,
            status="success",
            data=[{"title": "must-not-publish"}],
            pages_scraped=1,
        )
    return TargetResult(
        url=target.url,
        status="failed",
        data=[],
        errors=["selector miss"],
        pages_scraped=1,
    )


def _assert_error_envelope(payload: dict, status_code: int) -> None:
    assert set(payload) == {"error", "code", "status_code", "details"}
    assert payload["status_code"] == status_code
    assert isinstance(payload["error"], str)
    assert isinstance(payload["code"], str)
    assert isinstance(payload["details"], list)


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", ["target", "merge"])
async def test_v1_result_has_one_record_location_for_each_grouping_mode(
    client,
    monkeypatch,
    group_by,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    submitted = await client.post(
        "/scrape",
        content=_yaml(group_by=group_by),
        headers={"content-type": "application/x-yaml"},
    )
    result = await poll_until_ready(
        lambda: client.get(f"/results/{submitted.json()['job_id']}"),
        lambda response: response.status_code == 200,
        failure_message="Timed out waiting for contract result",
    )
    payload = result.json()

    assert result.headers["X-Scrapeyard-API-Version"] == "1"
    assert set(payload) == {
        "job_id",
        "run_id",
        "status",
        "completed_at",
        "errors",
        "targets",
        "budget_error",
        "results",
    }
    assert payload["targets"][0]["status"] == "success"
    assert "job_id" not in payload["results"] if isinstance(payload["results"], dict) else True
    if group_by == "target":
        assert payload["results"]["example.com"]["data"] == [{"title": "Contract"}]
    else:
        assert payload["results"] == [{"title": "Contract", "_source": "example.com"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("group_by", "empty_results"),
    [("target", {}), ("merge", [])],
)
async def test_all_or_nothing_failure_has_zero_accepted_records_end_to_end(
    client,
    monkeypatch,
    group_by,
    empty_results,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _mixed_atomic)
    submitted = await client.post(
        "/scrape",
        content=_atomic_yaml(group_by=group_by),
        headers={"content-type": "application/x-yaml"},
    )
    result = await poll_until_ready(
        lambda: client.get(f"/results/{submitted.json()['job_id']}"),
        lambda response: response.status_code == 200,
        failure_message="Timed out waiting for atomic failure result",
    )
    payload = result.json()
    job = (await client.get(f"/jobs/{submitted.json()['job_id']}")).json()

    assert payload["status"] == "failed"
    assert payload["results"] == empty_results
    successful_target = next(
        target for target in payload["targets"] if target["status"] == "success"
    )
    assert successful_target["count"] == 0
    assert successful_target["observed_count"] == 1
    assert job["runs"][0]["record_count"] == 0


@pytest.mark.asyncio
async def test_sync_and_polling_use_the_same_v1_result_contract(client, monkeypatch):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    submitted = await client.post(
        "/scrape",
        content=_yaml(mode="sync"),
        headers={"content-type": "application/x-yaml"},
    )
    assert submitted.status_code == 200
    polled = await client.get(f"/results/{submitted.json()['job_id']}")
    assert polled.status_code == 200
    assert polled.json() == submitted.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("group_by", ["target", "merge"])
async def test_sync_and_polling_share_missing_terminal_artifact_semantics(
    client,
    monkeypatch,
    group_by,
):
    job_store = get_job_store()
    captured: dict[str, str] = {}

    class _FailedWithoutArtifact:
        async def result(self, timeout=None, *, poll_delay=0.5):
            del timeout, poll_delay
            await job_store.fail_owned_run(
                captured["job_id"],
                captured["run_id"],
                utc_now(),
                error_count=1,
            )

    async def _enqueue(job_id, _config_yaml, _priority, **kwargs):
        captured.update(job_id=job_id, run_id=kwargs["run_id"])
        return _FailedWithoutArtifact()

    monkeypatch.setattr(get_worker_pool(), "enqueue", _enqueue)
    submitted = await client.post(
        "/scrape",
        content=_yaml(mode="sync", group_by=group_by),
        headers={"content-type": "application/x-yaml"},
    )

    assert submitted.status_code == 404
    assert submitted.json()["error"] == "Completed result artifact is no longer available"
    polled = await client.get(f"/results/{captured['job_id']}")
    assert polled.status_code == 404
    assert polled.json()["code"] == submitted.json()["code"] == "not_found"


@pytest.mark.asyncio
async def test_legacy_v0_compatibility_supports_representative_eyebox_reader(
    client,
    monkeypatch,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    submitted = await client.post(
        "/scrape?compatibility=legacy-v0",
        content=_yaml(mode="sync"),
        headers={"content-type": "application/x-yaml"},
    )
    assert submitted.status_code == 200
    payload = submitted.json()

    # Representative Eyebox v0 access pattern: API envelope -> stored output
    # document -> grouping map -> data records.
    stored_output = payload["results"]
    records = stored_output["results"]["example.com"]["data"]
    assert stored_output["job_id"] == payload["job_id"]
    assert stored_output["status"] == payload["status"]
    assert records == [{"title": "Contract"}]

    polled = await client.get(
        f"/results/{payload['job_id']}?compatibility=legacy-v0"
    )
    assert polled.json() == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_payload", [b"\xff", b'{"broken":'])
async def test_corrupt_result_artifacts_use_sanitized_unavailable_response(
    client,
    monkeypatch,
    corrupt_payload,
):
    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    submitted = await client.post(
        "/scrape",
        content=_yaml(),
        headers={"content-type": "application/x-yaml"},
    )
    job_id = submitted.json()["job_id"]
    result = await poll_until_ready(
        lambda: client.get(f"/results/{job_id}"),
        lambda response: response.status_code == 200,
        failure_message="Timed out waiting for result to corrupt",
    )
    metadata = await get_result_store().get_result_metadata(
        job_id,
        result.json()["run_id"],
    )
    assert metadata is not None
    (Path(metadata.file_path) / "results.json").write_bytes(corrupt_payload)

    response = await client.get(f"/results/{job_id}")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "broken" not in response.text
    assert metadata.file_path not in response.text


@pytest.mark.asyncio
async def test_custom_and_framework_validation_share_one_error_envelope(client):
    custom = await client.get("/results/missing?latest=false")
    framework = await client.get("/jobs?offset=-1")

    assert custom.status_code == 400
    assert framework.status_code == 422
    _assert_error_envelope(custom.json(), 400)
    _assert_error_envelope(framework.json(), 422)
    assert framework.json()["code"] == "validation_error"
    assert framework.json()["details"][0]["location"][-1] == "offset"
    assert custom.headers["X-Scrapeyard-API-Version"] == "1"
    assert framework.headers["X-Scrapeyard-API-Version"] == "1"


@pytest.mark.asyncio
async def test_yaml_and_query_validation_use_equivalent_422_envelopes(client):
    yaml_validation = await client.post(
        "/scrape",
        content="name: missing-project\ntarget:\n  url: https://example.com\n  selectors:\n    t: h1",
        headers={"content-type": "application/x-yaml"},
    )
    query_validation = await client.get("/jobs?limit=0")

    assert yaml_validation.status_code == query_validation.status_code == 422
    _assert_error_envelope(yaml_validation.json(), 422)
    _assert_error_envelope(query_validation.json(), 422)
    assert yaml_validation.json()["code"] == "validation_error"
    assert query_validation.json()["code"] == "validation_error"


@pytest.mark.asyncio
async def test_framework_404_and_405_use_the_public_error_envelope(client):
    missing = await client.get("/route-that-does-not-exist")
    wrong_method = await client.patch("/jobs")

    assert missing.status_code == 404
    assert wrong_method.status_code == 405
    _assert_error_envelope(missing.json(), 404)
    _assert_error_envelope(wrong_method.json(), 405)
    assert missing.json()["code"] == "not_found"
    assert wrong_method.json()["code"] == "method_not_allowed"
    assert wrong_method.headers["allow"]


@pytest.mark.asyncio
async def test_unhandled_errors_use_safe_versioned_json_envelope(test_app):
    async def _boom() -> None:
        raise RuntimeError("database password must never escape")

    existing_routes = list(test_app.router.routes)
    try:
        test_app.add_api_route("/_test/unhandled", _boom)
        transport = ASGITransport(app=test_app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/_test/unhandled")
    finally:
        test_app.router.routes[:] = existing_routes

    assert response.status_code == 500
    assert response.headers["X-Scrapeyard-API-Version"] == "1"
    assert sum(
        name.lower() == b"x-scrapeyard-api-version"
        for name, _value in response.headers.raw
    ) == 1
    assert response.headers["content-type"].startswith("application/json")
    _assert_error_envelope(response.json(), 500)
    assert response.json()["code"] == "internal_server_error"
    assert response.json()["error"] == "Internal server error"
    assert "password" not in response.text
