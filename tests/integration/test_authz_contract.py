from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport, AsyncClient

from scrapeyard.api.auth import AuthScope, authorize_request, parse_api_credentials
from scrapeyard.api.dependencies import get_job_store, get_result_store
from scrapeyard.api.middleware import APIKeyAuthMiddleware
from scrapeyard.api.routes import router
from scrapeyard.common.settings import get_settings
from scrapeyard.engine.scraper import TargetResult
from scrapeyard.main import http_exception_handler, request_validation_exception_handler
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.storage.database import get_db

SECRETS = {
    "reader": "reader-secret-0000000000",
    "submitter": "submit-secret-0000000000",
    "scheduler": "schedule-secret-000000000",
    "deleter": "delete-secret-0000000000",
    "monitor": "monitor-secret-000000000",
    "multi-scheduler": "multi-schedule-secret-00000",
    "beta-reader": "beta-reader-secret-000000",
    "global-reader": "global-reader-secret-0000",
    "transport-admin": "transport-secret-00000000",
}


def _credentials():
    return parse_api_credentials(
        json.dumps(
            {
                "reader": {
                    "secret": SECRETS["reader"],
                    "scopes": ["read"],
                    "projects": ["alpha"],
                },
                "submitter": {
                    "secret": SECRETS["submitter"],
                    "scopes": ["submit"],
                    "projects": ["alpha"],
                },
                "scheduler": {
                    "secret": SECRETS["scheduler"],
                    "scopes": ["schedule-admin"],
                    "projects": ["alpha"],
                },
                "deleter": {
                    "secret": SECRETS["deleter"],
                    "scopes": ["delete"],
                    "projects": ["alpha"],
                },
                "monitor": {
                    "secret": SECRETS["monitor"],
                    "scopes": ["health-detail"],
                },
                "multi-scheduler": {
                    "secret": SECRETS["multi-scheduler"],
                    "scopes": ["schedule-admin"],
                    "projects": ["alpha", "beta"],
                },
                "beta-reader": {
                    "secret": SECRETS["beta-reader"],
                    "scopes": ["read"],
                    "projects": ["beta"],
                },
                "global-reader": {
                    "secret": SECRETS["global-reader"],
                    "scopes": ["read"],
                },
                "transport-admin": {
                    "secret": SECRETS["transport-admin"],
                    "scopes": ["schedule-admin", "transport-admin"],
                    "projects": ["alpha"],
                },
            }
        )
    )


@pytest.fixture()
async def authorized_client(test_app) -> AsyncIterator[AsyncClient]:
    del test_app
    auth_app = FastAPI()
    auth_app.include_router(router)
    auth_app.add_exception_handler(HTTPException, http_exception_handler)
    auth_app.add_exception_handler(
        RequestValidationError,
        request_validation_exception_handler,
    )
    auth_app.add_middleware(APIKeyAuthMiddleware, credentials=_credentials())
    transport = ASGITransport(app=auth_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _headers(role: str) -> dict[str, str]:
    return {"X-API-Key": SECRETS[role]}


def _scrape_yaml(project: str) -> str:
    return f"""
project: {project}
name: authz-scrape
execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
"""


def _schedule_yaml(project: str = "alpha") -> str:
    return f"""
project: {project}
name: authz-schedule
schedule:
  cron: "0 9 * * *"
target:
  url: https://example.com
  selectors:
    title: h1
"""


def _transport_schedule_yaml(override: str) -> str:
    return f"""
project: alpha
name: transport-override
schedule:
  cron: "0 9 * * *"
{override}
"""


@pytest.mark.asyncio
async def test_read_only_cannot_submit_or_delete(authorized_client):
    store = get_job_store()
    await store.save_job(
        Job(
            job_id="read-only-delete",
            project="alpha",
            name="terminal",
            config_yaml="project: alpha",
            status=JobStatus.failed,
        )
    )

    submit = await authorized_client.post(
        "/scrape",
        content=_scrape_yaml("alpha"),
        headers={**_headers("reader"), "content-type": "application/x-yaml"},
    )
    delete = await authorized_client.delete(
        "/jobs/read-only-delete",
        headers=_headers("reader"),
    )

    assert submit.status_code == 403
    assert delete.status_code == 403
    assert submit.json()["code"] == delete.json()["code"] == "forbidden"


@pytest.mark.asyncio
async def test_submit_scope_and_project_boundary_are_both_enforced(
    authorized_client,
    monkeypatch,
):
    async def _success(*_args, **_kwargs):
        return TargetResult(
            url="https://example.com",
            status="success",
            data=[],
            pages_scraped=1,
        )

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", _success)
    allowed = await authorized_client.post(
        "/scrape",
        content=_scrape_yaml("alpha"),
        headers={**_headers("submitter"), "content-type": "application/x-yaml"},
    )
    denied = await authorized_client.post(
        "/scrape",
        content=_scrape_yaml("beta"),
        headers={**_headers("submitter"), "content-type": "application/x-yaml"},
    )

    assert allowed.status_code == 202
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_missing_scope_is_rejected_before_secret_bearing_yaml_is_parsed(
    authorized_client,
):
    response = await authorized_client.post(
        "/scrape",
        content="""
project: alpha
name: must-not-resolve
target:
  url: https://example.com
  browser:
    extra_headers:
      Authorization: ${SCRAPEYARD_SECRET_DO_NOT_RESOLVE}
  selectors:
    title: h1
""",
        headers={**_headers("reader"), "content-type": "application/x-yaml"},
    )

    assert response.status_code == 403
    assert "required scope" in response.json()["error"]


@pytest.mark.asyncio
async def test_project_is_authorized_before_its_allowlisted_secret_is_resolved(
    authorized_client,
    monkeypatch,
):
    monkeypatch.setenv(
        "SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST",
        '{"beta":["SCRAPEYARD_SECRET_BETA_ONLY"]}',
    )
    get_settings.cache_clear()
    response = await authorized_client.post(
        "/scrape",
        content="""
project: beta
name: forbidden-project
target:
  url: https://example.com
  browser:
    extra_headers:
      Authorization: ${SCRAPEYARD_SECRET_BETA_ONLY}
  selectors:
    title: h1
""",
        headers={**_headers("submitter"), "content-type": "application/x-yaml"},
    )

    assert response.status_code == 403
    assert "not authorized for project 'beta'" in response.json()["error"]


@pytest.mark.asyncio
async def test_project_scoped_reader_cannot_enumerate_or_read_other_project(
    authorized_client,
):
    store = get_job_store()
    await store.save_job(
        Job(
            job_id="beta-job",
            project="beta",
            name="private",
            config_yaml="project: beta",
            status=JobStatus.failed,
        )
    )

    unfiltered = await authorized_client.get("/jobs", headers=_headers("reader"))
    other = await authorized_client.get("/jobs/beta-job", headers=_headers("reader"))
    absent = await authorized_client.get("/jobs/no-such-job", headers=_headers("reader"))
    filtered = await authorized_client.get(
        "/jobs?project=alpha",
        headers=_headers("reader"),
    )

    assert unfiltered.status_code == 403
    # Another project's job reads as an absent one: neither its existence nor
    # its project is confirmed (TD-115).
    assert other.status_code == absent.status_code == 404
    assert other.json()["error"] == absent.json()["error"].replace("no-such-job", "beta-job")
    assert "beta" not in other.json()["error"].replace("beta-job", "")
    assert filtered.status_code == 200


@pytest.mark.asyncio
async def test_scheduled_job_cannot_transfer_historical_results_between_projects(
    authorized_client,
):
    created = await authorized_client.post(
        "/jobs",
        content=_schedule_yaml("alpha"),
        headers={
            **_headers("multi-scheduler"),
            "content-type": "application/x-yaml",
        },
    )
    assert created.status_code == 201
    job_id = created.json()["job_id"]
    await get_result_store().save_result(
        job_id,
        [{"private": "alpha-result"}],
        run_id="alpha-history",
    )

    moved = await authorized_client.put(
        f"/jobs/{job_id}",
        content=_schedule_yaml("beta"),
        headers={
            **_headers("multi-scheduler"),
            "content-type": "application/x-yaml",
        },
    )

    assert moved.status_code == 409
    assert "project cannot be changed" in moved.json()["error"]
    assert (await get_job_store().get_job(job_id)).project == "alpha"
    denied = await authorized_client.get(
        f"/results/{job_id}",
        headers=_headers("beta-reader"),
    )
    assert denied.status_code == 404


@pytest.mark.asyncio
async def test_retained_results_authorize_from_metadata_for_latest_and_explicit_runs(
    authorized_client,
):
    job_id = "retained-alpha-job"
    await get_job_store().save_job(
        Job(
            job_id=job_id,
            project="alpha",
            name="retained-results",
            config_yaml=_scrape_yaml("alpha"),
            status=JobStatus.complete,
        )
    )
    result_store = get_result_store()
    await result_store.save_result(job_id, [{"version": "old"}], run_id="run-old")
    await result_store.save_result(job_id, [{"version": "new"}], run_id="run-new")
    async with get_db("jobs.db") as db:
        await db.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        await db.commit()

    paths = (
        f"/results/{job_id}",
        f"/results/{job_id}?latest=false&run_id=run-old",
    )
    alpha = [
        await authorized_client.get(path, headers=_headers("reader"))
        for path in paths
    ]
    beta = [
        await authorized_client.get(path, headers=_headers("beta-reader"))
        for path in paths
    ]
    global_reader = [
        await authorized_client.get(path, headers=_headers("global-reader"))
        for path in paths
    ]

    assert [response.status_code for response in alpha] == [200, 200]
    assert [response.status_code for response in global_reader] == [200, 200]
    assert [response.status_code for response in beta] == [404, 404]
    assert alpha[0].json()["run_id"] == "run-new"
    assert alpha[1].json()["run_id"] == "run-old"


@pytest.mark.asyncio
async def test_schedule_admin_and_delete_scopes_are_least_privilege(
    authorized_client,
):
    created = await authorized_client.post(
        "/jobs",
        content=_schedule_yaml(),
        headers={**_headers("scheduler"), "content-type": "application/x-yaml"},
    )
    reader_create = await authorized_client.post(
        "/jobs",
        content=_schedule_yaml(),
        headers={**_headers("reader"), "content-type": "application/x-yaml"},
    )
    assert created.status_code == 201
    assert reader_create.status_code == 403

    store = get_job_store()
    await store.save_job(
        Job(
            job_id="delete-me",
            project="alpha",
            name="delete-me",
            config_yaml="project: alpha",
            status=JobStatus.failed,
        )
    )
    deleted = await authorized_client.delete(
        "/jobs/delete-me",
        headers=_headers("deleter"),
    )
    assert deleted.status_code == 204


async def _save_active_job(job_id: str, *, project: str = "alpha", scheduled: bool = False) -> None:
    await get_job_store().save_job(
        Job(
            job_id=job_id,
            project=project,
            name=job_id,
            config_yaml=f"project: {project}",
            status=JobStatus.queued,
            current_run_id=f"{job_id}-run",
            current_trigger="scheduled" if scheduled else "adhoc",
            schedule_cron="0 9 * * *" if scheduled else None,
        )
    )


@pytest.mark.asyncio
async def test_submit_scope_cancels_only_an_exact_adhoc_run_in_its_project(authorized_client):
    for job_id, kwargs in (
        ("own", {}),
        ("unnamed-run", {}),
        ("other-project", {"project": "beta"}),
        ("scheduled", {"scheduled": True}),
    ):
        await _save_active_job(job_id, **kwargs)
    submitter = _headers("submitter")

    own = await authorized_client.post("/jobs/own/cancel?run_id=own-run", headers=submitter)
    stale = await authorized_client.post("/jobs/own/cancel?run_id=another-run", headers=submitter)
    current = await authorized_client.post("/jobs/unnamed-run/cancel", headers=submitter)
    other_project = await authorized_client.post(
        "/jobs/other-project/cancel?run_id=other-project-run", headers=submitter,
    )
    scheduled = await authorized_client.post(
        "/jobs/scheduled/cancel?run_id=scheduled-run", headers=submitter,
    )
    reader = await authorized_client.post(
        "/jobs/unnamed-run/cancel?run_id=unnamed-run-run", headers=_headers("reader"),
    )
    delete = await authorized_client.delete("/jobs/own", headers=submitter)

    assert own.status_code == 204
    assert (await get_job_store().get_job("own")).status is JobStatus.cancelled
    assert stale.status_code == 409
    assert other_project.status_code == 404
    for denied in (current, scheduled, reader, delete):
        assert denied.status_code == 403
        assert denied.json()["code"] == "forbidden"
    for job_id in ("unnamed-run", "other-project", "scheduled"):
        assert (await get_job_store().get_job(job_id)).status is JobStatus.queued
    assert "'delete'" in current.json()["error"]
    assert "'delete'" in scheduled.json()["error"]
    assert "'submit'" in reader.json()["error"]


@pytest.mark.asyncio
async def test_delete_scope_still_cancels_current_and_scheduled_jobs(authorized_client):
    await _save_active_job("current")
    await _save_active_job("scheduled", scheduled=True)
    deleter = _headers("deleter")

    current = await authorized_client.post("/jobs/current/cancel", headers=deleter)
    scheduled = await authorized_client.post(
        "/jobs/scheduled/cancel?run_id=scheduled-run", headers=deleter,
    )

    assert current.status_code == scheduled.status_code == 204
    assert (await get_job_store().get_job("scheduled")).status is JobStatus.cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        """proxy:\n  url: http://8.8.8.8:8080\ntarget:\n  url: https://example.com\n  selectors:\n    title: h1""",
        """target:\n  url: https://example.com\n  proxy:\n    url: http://8.8.8.8:8080\n  selectors:\n    title: h1""",
        """target:\n  url: https://example.com\n  fetcher: dynamic\n  browser:\n    cdp_url: ws://8.8.8.8:9222\n  selectors:\n    title: h1""",
    ],
)
async def test_untrusted_mode_requires_transport_admin_for_overrides(
    authorized_client,
    override,
):
    settings = get_settings()
    original_untrusted = settings.untrusted_submissions
    original_proxy = settings.proxy_url
    settings.untrusted_submissions = True
    settings.proxy_url = "http://8.8.8.8:8080"
    try:
        denied = await authorized_client.post(
            "/jobs",
            content=_transport_schedule_yaml(override),
            headers={
                **_headers("scheduler"),
                "content-type": "application/x-yaml",
            },
        )
        allowed = await authorized_client.post(
            "/jobs",
            content=_transport_schedule_yaml(override),
            headers={
                **_headers("transport-admin"),
                "content-type": "application/x-yaml",
            },
        )
    finally:
        settings.untrusted_submissions = original_untrusted
        settings.proxy_url = original_proxy

    assert denied.status_code == 403
    assert "transport-admin" in denied.json()["error"]
    assert allowed.status_code == 201


@pytest.mark.asyncio
async def test_public_liveness_is_minimal_and_detailed_health_requires_scope():
    app = FastAPI()

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        authorize_request(request, AuthScope.health_detail)
        return {"status": "ok", "dependencies": {"sqlite": {"ok": True}}}

    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_middleware(
        APIKeyAuthMiddleware,
        credentials=_credentials(),
        exempt_paths={"/health/live"},
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        live_response = await client.get("/health/live")
        reader_response = await client.get(
            "/health/ready",
            headers=_headers("reader"),
        )
        monitor_response = await client.get(
            "/health/ready",
            headers=_headers("monitor"),
        )

    assert live_response.json() == {"status": "ok"}
    assert "dependencies" not in live_response.json()
    assert reader_response.status_code == 403
    assert monitor_response.status_code == 200
    assert "dependencies" in monitor_response.json()


@pytest.mark.asyncio
async def test_audit_logs_name_caller_without_logging_secret(
    authorized_client,
    caplog,
):
    caplog.set_level(logging.INFO, logger="scrapeyard.api.middleware")
    response = await authorized_client.get(
        "/jobs?project=alpha",
        headers=_headers("reader"),
    )
    assert response.status_code == 200
    assert "caller_id=reader" in caplog.text
    assert "credential_name=reader" in caplog.text
    assert SECRETS["reader"] not in caplog.text


async def test_acceptance_lookup_keeps_caller_submit_scope_and_exact_run_keeps_read_scope(authorized_client, monkeypatch):
    import asyncio

    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("scrapeyard.queue.worker.scrape_target", blocked)
    headers = {**_headers("submitter"), "Idempotency-Key": "owned-receipt", "content-type": "application/x-yaml"}
    accepted = await authorized_client.post("/scrape", content=_scrape_yaml("alpha"), headers=headers)
    assert accepted.status_code == 202
    recovered = await authorized_client.get("/scrape/acceptance", headers=headers)
    assert recovered.status_code == 200
    assert recovered.json()["job_id"] == accepted.json()["job_id"]
    forbidden = await authorized_client.get("/scrape/acceptance", headers={**_headers("global-reader"), "Idempotency-Key": "owned-receipt"})
    assert forbidden.status_code == 403
    path = f"/jobs/{accepted.json()['job_id']}/runs/{accepted.json()['run_id']}"
    assert (await authorized_client.get(path, headers=_headers("reader"))).status_code == 200
    assert (await authorized_client.get(path, headers=_headers("beta-reader"))).status_code == 404
    assert (await authorized_client.get(path, headers=_headers("submitter"))).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "method", "path", "body"),
    [
        ("reader", "GET", "/jobs/{id}", None),
        ("reader", "GET", "/jobs/{id}/runs/{id}-run", None),
        ("reader", "GET", "/results/{id}", None),
        ("deleter", "POST", "/jobs/{id}/cancel", None),
        ("deleter", "DELETE", "/jobs/{id}", None),
        ("scheduler", "POST", "/jobs/{id}/pause", None),
        ("scheduler", "POST", "/jobs/{id}/resume", None),
        ("scheduler", "POST", "/jobs/{id}/trigger", None),
        ("scheduler", "PUT", "/jobs/{id}", _schedule_yaml("alpha")),
    ],
)
async def test_another_projects_job_answers_as_an_absent_one(
    authorized_client, role, method, path, body,
):
    await _save_active_job("beta-live", project="beta", scheduled=True)

    async def ask(job_id: str):
        return await authorized_client.request(
            method,
            path.format(id=job_id),
            content=body,
            headers={**_headers(role), "content-type": "application/x-yaml"},
        )

    other, absent = await ask("beta-live"), await ask("absent-job")

    # 404, or 204 for a delete: an absent job's deletion is already done.
    assert other.status_code == absent.status_code == (204 if method == "DELETE" else 404)
    if absent.content:
        assert other.json() == {
            **absent.json(),
            "error": absent.json()["error"].replace("absent-job", "beta-live"),
        }
    assert (await get_job_store().get_job("beta-live")).status is JobStatus.queued


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
async def test_the_schema_and_its_pages_are_not_served(path):
    from scrapeyard.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path, headers=_headers("monitor"))

    assert response.status_code in (401, 404)


@pytest.mark.asyncio
async def test_a_caller_that_may_cancel_nothing_learns_nothing_from_cancel(authorized_client):
    # An unrestricted credential with neither submit nor delete must not tell
    # an existing job of any project from an absent one.
    await _save_active_job("beta-live", project="beta")

    existing = await authorized_client.post("/jobs/beta-live/cancel", headers=_headers("monitor"))
    absent = await authorized_client.post("/jobs/absent-job/cancel", headers=_headers("monitor"))

    assert existing.status_code == absent.status_code == 403
    assert existing.json() == absent.json()
    assert (await get_job_store().get_job("beta-live")).status is JobStatus.queued
