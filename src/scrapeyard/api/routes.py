"""API routes for scrape, jobs, results, and errors (spec section 4.1)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.api.query_parsing import parse_error_filters
from scrapeyard.api.response_utils import json_error, json_response, no_content_response
from scrapeyard.api.scrape_submission import submit_scrape_job
from scrapeyard.api.serializers import (
    serialize_error_record,
    serialize_job_created,
    serialize_job_detail,
    serialize_job_summary,
    serialize_results_payload,
    serialize_scrape_queued,
    serialize_scrape_result,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.config.loader import load_config
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.scheduler.cron import SchedulerService
from scrapeyard.storage.job_store import DuplicateJobError
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore

router = APIRouter()
logger = logging.getLogger(__name__)


def _is_yaml_request(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/x-yaml"


async def _read_valid_yaml_config(request: Request) -> tuple[str, Any] | Response:
    if not _is_yaml_request(request):
        return json_error(415, "Content-Type must be application/x-yaml")
    body = await request.body()
    config_yaml = body.decode("utf-8")
    try:
        config = load_config(config_yaml)
    except Exception as exc:
        return json_error(422, f"Invalid config: {exc}")
    return config_yaml, config


def _resolve_admin_read_limit(limit: int | None) -> int:
    settings = get_settings()
    resolved_limit = limit or settings.admin_read_default_limit
    if resolved_limit > settings.admin_read_max_limit:
        raise ValueError(
            f"Invalid 'limit': {resolved_limit}. "
            f"Maximum is {settings.admin_read_max_limit}."
        )
    return resolved_limit


def _set_pagination_headers(
    response: Response,
    *,
    limit: int,
    offset: int,
    item_count: int,
    has_more: bool,
) -> None:
    response.headers["X-Scrapeyard-Limit"] = str(limit)
    response.headers["X-Scrapeyard-Offset"] = str(offset)
    response.headers["X-Scrapeyard-Item-Count"] = str(item_count)
    response.headers["X-Scrapeyard-Has-More"] = "true" if has_more else "false"
    if has_more:
        response.headers["X-Scrapeyard-Next-Offset"] = str(offset + item_count)


@router.post("/scrape")
async def scrape(
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
    worker_pool=Depends(get_worker_pool),
) -> Response:
    """Submit an ad-hoc scrape request."""
    parsed = await _read_valid_yaml_config(request)
    if isinstance(parsed, Response):
        return parsed
    config_yaml, config = parsed

    settings = get_settings()
    try:
        submission = await submit_scrape_job(
            config_yaml=config_yaml,
            config=config,
            job_store=job_store,
            result_store=result_store,
            worker_pool=worker_pool,
            sync_timeout_seconds=settings.sync_timeout_seconds,
            sync_poll_delay_seconds=settings.sync_poll_delay_seconds,
        )
    except MemoryError:
        logger.warning("Rejecting async scrape due to pool memory pressure")
        return json_error(503, "Server at capacity — try again later")

    if not submission.completed:
        return json_response(
            202,
            serialize_scrape_queued(
                submission.job_id,
                status=submission.status,
                poll_url=f"/results/{submission.job_id}",
            ),
        )

    return json_response(
        200,
        serialize_scrape_result(
            submission.job_id,
            status=submission.status,
            results=submission.results,
        ),
    )


@router.post("/jobs", status_code=201, response_model=None)
async def create_job(
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Any:
    """Create a scheduled job. Requires a schedule block in the config."""
    parsed = await _read_valid_yaml_config(request)
    if isinstance(parsed, Response):
        return parsed
    config_yaml, config = parsed

    if config.schedule is None:
        return json_error(400, "A 'schedule' block is required for POST /jobs")

    job = Job(
        job_id=str(uuid.uuid4()),
        project=config.project,
        name=config.name,
        config_yaml=config_yaml,
        schedule_cron=config.schedule.cron,
        schedule_enabled=config.schedule.enabled,
    )
    try:
        await job_store.save_job(job)
    except DuplicateJobError as exc:
        return json_error(409, f"Job name {exc.name!r} already exists in project {exc.project!r}")

    scheduler.register_job(job.job_id, config.schedule.cron, enabled=config.schedule.enabled)
    return serialize_job_created(job)


@router.get("/jobs", response_model=None)
async def list_jobs(
    response: Response,
    project: str | None = Query(None),
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    job_store: JobStore = Depends(get_job_store),
) -> Any:
    """List jobs, optionally filtered by project."""
    try:
        resolved_limit = _resolve_admin_read_limit(limit)
    except ValueError as exc:
        return json_error(400, str(exc))

    rows = await job_store.list_jobs_with_stats(project, limit=resolved_limit + 1, offset=offset)
    has_more = len(rows) > resolved_limit
    rows = rows[:resolved_limit]
    _set_pagination_headers(
        response,
        limit=resolved_limit,
        offset=offset,
        item_count=len(rows),
        has_more=has_more,
    )
    return [
        serialize_job_summary(job, run_count=run_count, last_run_at=last_run_at)
        for job, run_count, last_run_at in rows
    ]


@router.get("/jobs/{job_id}", response_model=None)
async def get_job(
    job_id: str,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Any:
    """Get a single job by ID."""
    try:
        job = await job_store.get_job(job_id)
    except KeyError:
        return json_error(404, f"Job {job_id!r} not found")

    runs = await job_store.get_job_runs(job_id, limit=10)
    run_count, last_run_at = await job_store.get_job_run_stats(job_id)
    next_run_at = scheduler.get_next_run_time(job_id)
    return serialize_job_detail(
        job,
        runs=runs,
        run_count=run_count,
        last_run_at=last_run_at,
        next_run_at=next_run_at,
    )


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    delete_results: bool = Query(False),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
    error_store: ErrorStore = Depends(get_error_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Response:
    """Delete a job by ID."""
    try:
        await job_store.get_job(job_id)
    except KeyError:
        return json_error(404, f"Job {job_id!r} not found")

    scheduler.remove_job(job_id)
    if delete_results:
        await result_store.delete_results(job_id)
    await error_store.delete_errors_for_job(job_id)
    await job_store.delete_job(job_id)
    return no_content_response()


@router.get("/results/{job_id}", response_model=None)
async def get_results(
    job_id: str,
    latest: bool = Query(True),
    run_id: str | None = Query(None),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
) -> Any:
    """Get results for a job."""
    try:
        job = await job_store.get_job(job_id)
    except KeyError:
        return json_error(404, f"Job {job_id!r} not found")

    if job.status in (JobStatus.queued, JobStatus.running):
        return json_response(
            202,
            serialize_scrape_queued(job_id, status=job.status.value, poll_url=f"/jobs/{job_id}"),
        )

    if run_id is None and not latest:
        return json_error(400, "Provide run_id when latest=false")

    try:
        payload = await result_store.get_result(job_id, run_id=run_id)
    except KeyError:
        return json_error(404, f"No results found for job {job_id!r}")

    return serialize_results_payload(
        job_id,
        run_id=payload.run_id,
        status=job.status.value,
        results=payload.data,
    )


@router.get("/errors", response_model=None)
async def get_errors(
    response: Response,
    project: str | None = Query(None),
    job_id: str | None = Query(None),
    since: str | None = Query(None),
    error_type: str | None = Query(None),
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    error_store: ErrorStore = Depends(get_error_store),
) -> Any:
    """Query error records with optional filters."""
    try:
        resolved_limit = _resolve_admin_read_limit(limit)
    except ValueError as exc:
        return json_error(400, str(exc))
    filters = parse_error_filters(
        project=project,
        job_id=job_id,
        since=since,
        error_type=error_type,
    )
    if isinstance(filters, Response):
        return filters
    errors = await error_store.query_errors(filters, limit=resolved_limit + 1, offset=offset)
    has_more = len(errors) > resolved_limit
    errors = errors[:resolved_limit]
    _set_pagination_headers(
        response,
        limit=resolved_limit,
        offset=offset,
        item_count=len(errors),
        has_more=has_more,
    )
    return [serialize_error_record(error) for error in errors]
