"""API routes for scrape, jobs, results, and errors (spec section 4.1)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import ValidationError
from yaml import YAMLError

from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.api.query_parsing import parse_error_filters
from scrapeyard.api.response_utils import (
    apply_paginated_list_response,
    json_response,
    no_content_response,
    raise_json_error,
)
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
from scrapeyard.config.schema import ScrapeConfig
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.pool import WorkerPool
from scrapeyard.scheduler.cron import SchedulerService
from scrapeyard.storage.job_store import DuplicateJobError
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore

router = APIRouter()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedYamlConfig:
    config_yaml: str
    config: ScrapeConfig


def _is_yaml_request(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/x-yaml"


async def _read_valid_yaml_config(request: Request) -> ParsedYamlConfig:
    if not _is_yaml_request(request):
        raise_json_error(415, "Content-Type must be application/x-yaml")
    try:
        body = await request.body()
        config_yaml = body.decode("utf-8")
        config = await asyncio.to_thread(load_config, config_yaml)
    except (UnicodeDecodeError, ValidationError, TypeError, ValueError, YAMLError) as exc:
        raise_json_error(422, f"Invalid config: {exc}")
    return ParsedYamlConfig(config_yaml, config)


def _resolve_admin_read_limit(limit: int | None) -> int:
    settings = get_settings()
    resolved_limit = limit or settings.admin_read_default_limit
    if resolved_limit > settings.admin_read_max_limit:
        raise_json_error(
            400,
            f"Invalid 'limit': {resolved_limit}. "
            f"Maximum is {settings.admin_read_max_limit}.",
        )
    return resolved_limit


async def _get_job_or_404(job_store: JobStore, job_id: str) -> Job:
    try:
        return await job_store.get_job(job_id)
    except KeyError:
        raise_json_error(404, f"Job {job_id!r} not found")


def _queued_scrape_response(job_id: str, *, status: str, poll_url: str) -> Response:
    return json_response(
        202,
        serialize_scrape_queued(job_id, status=status, poll_url=poll_url),
    )


@router.post("/scrape")
async def scrape(
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
    worker_pool: WorkerPool = Depends(get_worker_pool),
) -> Response:
    """Submit an ad-hoc scrape request."""
    parsed = await _read_valid_yaml_config(request)
    config_yaml = parsed.config_yaml
    config = parsed.config

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
        raise_json_error(503, "Server at capacity — try again later")

    if not submission.completed:
        return _queued_scrape_response(
            submission.job_id,
            status=submission.status,
            poll_url=f"/results/{submission.job_id}",
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
    config_yaml = parsed.config_yaml
    config = parsed.config

    if config.schedule is None:
        raise_json_error(400, "A 'schedule' block is required for POST /jobs")

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
        raise_json_error(409, f"Job name {exc.name!r} already exists in project {exc.project!r}")

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
    resolved_limit = _resolve_admin_read_limit(limit)

    rows = await job_store.list_jobs_with_stats(project, limit=resolved_limit + 1, offset=offset)
    rows = apply_paginated_list_response(response, rows=rows, limit=resolved_limit, offset=offset)
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
    job = await _get_job_or_404(job_store, job_id)

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
    await _get_job_or_404(job_store, job_id)

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
    job = await _get_job_or_404(job_store, job_id)

    if run_id is None:
        if not latest:
            raise_json_error(400, "Provide run_id when latest=false")
        if job.status in (JobStatus.queued, JobStatus.running):
            return _queued_scrape_response(job_id, status=job.status.value, poll_url=f"/jobs/{job_id}")

    try:
        payload = await result_store.get_result(job_id, run_id=run_id)
    except KeyError:
        raise_json_error(404, f"No results found for job {job_id!r}")

    return serialize_results_payload(
        job_id,
        run_id=payload.run_id,
        status=payload.status,
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
    resolved_limit = _resolve_admin_read_limit(limit)
    filters = parse_error_filters(
        project=project,
        job_id=job_id,
        since=since,
        error_type=error_type,
    )
    errors = await error_store.query_errors(filters, limit=resolved_limit + 1, offset=offset)
    errors = apply_paginated_list_response(response, rows=errors, limit=resolved_limit, offset=offset)
    return [serialize_error_record(error) for error in errors]
