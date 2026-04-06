"""API routes for scrape, jobs, results, and errors (spec section 4.1)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from scrapeyard.api.dependencies import (
    get_error_store,
    get_job_store,
    get_result_store,
    get_scheduler,
    get_worker_pool,
)
from scrapeyard.config.loader import load_config
from scrapeyard.config.schema import ExecutionMode, FetcherType
from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.settings import get_settings
from scrapeyard.models.job import ErrorFilters, ErrorType, Job, JobStatus
from scrapeyard.queue.pool import QueueJobHandle, WorkerPool
from scrapeyard.scheduler.cron import SchedulerService
from scrapeyard.storage.job_store import DuplicateJobError
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore

router = APIRouter()
logger = logging.getLogger(__name__)


def _is_yaml_request(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/x-yaml"


def _should_wait_for_completion(config: Any) -> bool:
    """Determine if a scrape should wait on queued completion.

    Heuristic: sync if single target AND no pagination AND fetcher is basic.
    Overridable via execution.mode.
    """
    if config.execution.mode == ExecutionMode.sync:
        return True
    if config.execution.mode == ExecutionMode.async_:
        return False
    # auto mode
    targets = config.resolved_targets()
    if len(targets) != 1:
        return False
    target = targets[0]
    if target.pagination is not None:
        return False
    return bool(target.fetcher == FetcherType.basic)


def _queued_response(job_id: str) -> Response:
    return Response(
        content=_json_encode({
            "job_id": job_id,
            "status": "queued",
            "poll_url": f"/results/{job_id}",
        }),
        status_code=202,
        media_type="application/json",
    )


async def _wait_for_queued_job(
    queued_job: QueueJobHandle,
    *,
    timeout_seconds: int,
    poll_delay_seconds: float,
) -> bool:
    try:
        await queued_job.result(timeout=timeout_seconds, poll_delay=poll_delay_seconds)
    except asyncio.TimeoutError:
        return False
    return True


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
    worker_pool: WorkerPool = Depends(get_worker_pool),
) -> Response:
    """Submit an ad-hoc scrape request."""
    if not _is_yaml_request(request):
        return _error_response(415, "Content-Type must be application/x-yaml")

    body = await request.body()
    config_yaml = body.decode("utf-8")
    try:
        config = load_config(config_yaml)
    except Exception as exc:
        return _error_response(422, f"Invalid config: {exc}")

    job = Job(
        job_id=str(uuid.uuid4()),
        project=config.project,
        name=f"{config.name}-{uuid.uuid4().hex[:8]}",
        config_yaml=config_yaml,
        updated_at=datetime.now(timezone.utc),
        current_run_id=generate_run_id(),
    )
    await job_store.save_job(job)

    has_browser_target = any(
        t.fetcher != FetcherType.basic for t in config.resolved_targets()
    )
    try:
        queued_job = await worker_pool.enqueue(
            job.job_id,
            config_yaml,
            config.execution.priority.value,
            needs_browser=has_browser_target,
            run_id=job.current_run_id,
            trigger="adhoc",
        )
    except MemoryError:
        logger.warning("Rejecting async scrape job %s due to pool memory pressure", job.job_id)
        await job_store.delete_job(job.job_id)
        return _error_response(503, "Server at capacity — try again later")

    if not _should_wait_for_completion(config):
        return _queued_response(job.job_id)

    settings = get_settings()
    completed = await _wait_for_queued_job(
        queued_job,
        timeout_seconds=settings.sync_timeout_seconds,
        poll_delay_seconds=settings.sync_poll_delay_seconds,
    )
    if not completed:
        return _queued_response(job.job_id)

    try:
        payload = await result_store.get_result(job.job_id)
    except KeyError:
        payload = None
    updated_job = await job_store.get_job(job.job_id)
    return Response(
        content=_json_encode({
            "job_id": job.job_id,
            "status": updated_job.status.value,
            "results": payload.data if payload else None,
        }),
        status_code=200,
        media_type="application/json",
    )


@router.post("/jobs", status_code=201, response_model=None)
async def create_job(
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Any:
    """Create a scheduled job. Requires a schedule block in the config."""
    if not _is_yaml_request(request):
        return _error_response(415, "Content-Type must be application/x-yaml")

    body = await request.body()
    config_yaml = body.decode("utf-8")
    try:
        config = load_config(config_yaml)
    except Exception as exc:
        return _error_response(422, f"Invalid config: {exc}")

    if config.schedule is None:
        return _error_response(400, "A 'schedule' block is required for POST /jobs")

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
        return _error_response(
            409, f"Job name {exc.name!r} already exists in project {exc.project!r}"
        )
    scheduler.register_job(job.job_id, config.schedule.cron, enabled=config.schedule.enabled)
    return {
        "job_id": job.job_id,
        "project": config.project,
        "name": config.name,
        "schedule": config.schedule.cron,
    }


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
        return _error_response(400, str(exc))

    rows = await job_store.list_jobs_with_stats(
        project,
        limit=resolved_limit + 1,
        offset=offset,
    )
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
        {
            "job_id": job.job_id,
            "project": job.project,
            "name": job.name,
            "status": job.status.value,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "schedule_cron": job.schedule_cron,
            "schedule_enabled": job.schedule_enabled,
            "run_count": run_count,
            "last_run_at": last_run_at.isoformat() if last_run_at else None,
        }
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
        return _error_response(404, f"Job {job_id!r} not found")

    runs = await job_store.get_job_runs(job_id, limit=10)
    run_count, last_run_at = await job_store.get_job_run_stats(job_id)
    next_run_at = scheduler.get_next_run_time(job_id)

    return {
        "job_id": job.job_id,
        "project": job.project,
        "name": job.name,
        "status": job.status.value,
        "config_yaml": job.config_yaml,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "schedule_cron": job.schedule_cron,
        "schedule_enabled": job.schedule_enabled,
        "next_run_at": next_run_at.isoformat() if next_run_at else None,
        "run_count": run_count,
        "last_run_at": last_run_at.isoformat() if last_run_at else None,
        "runs": [
            {
                "run_id": r.run_id,
                "status": r.status.value,
                "trigger": r.trigger,
                "config_hash": r.config_hash,
                "started_at": r.started_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "record_count": r.record_count,
                "error_count": r.error_count,
            }
            for r in runs
        ],
    }


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
        return _error_response(404, f"Job {job_id!r} not found")

    scheduler.remove_job(job_id)
    if delete_results:
        await result_store.delete_results(job_id)
    await error_store.delete_errors_for_job(job_id)
    await job_store.delete_job(job_id)
    return Response(status_code=204)


@router.get("/results/{job_id}", response_model=None)
async def get_results(
    job_id: str,
    latest: bool = Query(True),
    run_id: str | None = Query(None),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
) -> Any:
    """Get results for a job."""
    # Check job exists.
    try:
        job = await job_store.get_job(job_id)
    except KeyError:
        return _error_response(404, f"Job {job_id!r} not found")

    # If job is still running, return 202.
    if job.status in (JobStatus.queued, JobStatus.running):
        return Response(
            content=_json_encode({
                "job_id": job_id,
                "status": job.status.value,
                "poll_url": f"/jobs/{job_id}",
            }),
            status_code=202,
            media_type="application/json",
        )

    if run_id is None and not latest:
        return _error_response(400, "Provide run_id when latest=false")

    try:
        payload = await result_store.get_result(job_id, run_id=run_id)
    except KeyError:
        return _error_response(404, f"No results found for job {job_id!r}")

    return {
        "job_id": job_id,
        "run_id": payload.run_id,
        "status": job.status.value,
        "results": payload.data,
    }


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
        return _error_response(400, str(exc))
    try:
        since_dt = datetime.fromisoformat(since) if since else None
    except ValueError:
        return _error_response(400, f"Invalid 'since' format: {since!r}")
    try:
        error_type_enum = ErrorType(error_type) if error_type else None
    except ValueError:
        return _error_response(400, f"Invalid 'error_type': {error_type!r}")

    filters = ErrorFilters(
        project=project,
        job_id=job_id,
        since=since_dt,
        error_type=error_type_enum,
    )
    errors = await error_store.query_errors(
        filters,
        limit=resolved_limit + 1,
        offset=offset,
    )
    has_more = len(errors) > resolved_limit
    errors = errors[:resolved_limit]
    _set_pagination_headers(
        response,
        limit=resolved_limit,
        offset=offset,
        item_count=len(errors),
        has_more=has_more,
    )
    return [
        {
            "job_id": e.job_id,
            "run_id": e.run_id,
            "project": e.project,
            "target_url": e.target_url,
            "attempt": e.attempt,
            "timestamp": e.timestamp.isoformat(),
            "error_type": e.error_type.value,
            "http_status": e.http_status,
            "fetcher_used": e.fetcher_used,
            "error_message": e.error_message,
            "selectors_matched": e.selectors_matched,
            "action_taken": e.action_taken.value,
            "resolved": e.resolved,
        }
        for e in errors
    ]


def _json_encode(data: Any) -> bytes:
    return json.dumps(data).encode("utf-8")


def _error_response(status_code: int, message: str) -> Response:
    """Return a JSON error response with a consistent structure."""
    return Response(
        content=_json_encode({"error": message}),
        status_code=status_code,
        media_type="application/json",
    )
