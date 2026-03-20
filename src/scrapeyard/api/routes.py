"""API routes for scrape, jobs, results, and errors (spec section 4.1)."""

from __future__ import annotations

import json
import logging
import uuid
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

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
    if target.fetcher != FetcherType.basic:
        return False
    return True


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
) -> bool:
    try:
        await queued_job.result(timeout=timeout_seconds, poll_delay=0.05)
    except asyncio.TimeoutError:
        return False
    return True


@router.post("/scrape")
async def scrape(
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
    worker_pool: WorkerPool = Depends(get_worker_pool),
) -> Response:
    """Submit an ad-hoc scrape request."""
    if not _is_yaml_request(request):
        return Response(
            content=_json_encode({"error": "Content-Type must be application/x-yaml"}),
            status_code=415,
            media_type="application/json",
        )

    body = await request.body()
    config_yaml = body.decode("utf-8")
    try:
        config = load_config(config_yaml)
    except Exception as exc:
        return Response(
            content=_json_encode({"error": f"Invalid config: {exc}"}),
            status_code=422,
            media_type="application/json",
        )

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
        )
    except MemoryError:
        logger.warning("Rejecting async scrape job %s due to pool memory pressure", job.job_id)
        await job_store.delete_job(job.job_id)
        return Response(
            content=_json_encode({"error": "Server at capacity — try again later"}),
            status_code=503,
            media_type="application/json",
        )

    if not _should_wait_for_completion(config):
        return _queued_response(job.job_id)

    settings = get_settings()
    completed = await _wait_for_queued_job(
        queued_job,
        timeout_seconds=settings.sync_timeout_seconds,
    )
    if not completed:
        return _queued_response(job.job_id)

    try:
        result = await result_store.get_result(job.job_id)
    except KeyError:
        result = None
    updated_job = await job_store.get_job(job.job_id)
    return Response(
        content=_json_encode({
            "job_id": job.job_id,
            "status": updated_job.status.value,
            "results": result,
        }),
        status_code=200,
        media_type="application/json",
    )


@router.post("/jobs", status_code=201, response_model=None)
async def create_job(
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
):
    """Create a scheduled job. Requires a schedule block in the config."""
    body = await request.body()
    config_yaml = body.decode("utf-8")
    try:
        config = load_config(config_yaml)
    except Exception as exc:
        return Response(
            content=_json_encode({"error": f"Invalid config: {exc}"}),
            status_code=422,
            media_type="application/json",
        )

    if config.schedule is None:
        return Response(
            content=_json_encode({"error": "A 'schedule' block is required for POST /jobs"}),
            status_code=400,
            media_type="application/json",
        )

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
        return Response(
            content=_json_encode({
                "error": (
                    f"Job name {exc.name!r} already exists in project {exc.project!r}"
                )
            }),
            status_code=409,
            media_type="application/json",
        )
    scheduler.register_job(job.job_id, config.schedule.cron, enabled=config.schedule.enabled)
    return {
        "job_id": job.job_id,
        "project": config.project,
        "name": config.name,
        "schedule": config.schedule.cron,
    }


@router.get("/jobs")
async def list_jobs(
    project: Optional[str] = Query(None),
    job_store: JobStore = Depends(get_job_store),
) -> list[dict[str, Any]]:
    """List jobs, optionally filtered by project."""
    jobs = await job_store.list_jobs(project)
    return [_job_to_dict(j) for j in jobs]


@router.get("/jobs/{job_id}", response_model=None)
async def get_job(
    job_id: str,
    job_store: JobStore = Depends(get_job_store),
):
    """Get a single job by ID."""
    try:
        job = await job_store.get_job(job_id)
    except KeyError:
        return Response(
            content=_json_encode({"error": f"Job {job_id!r} not found"}),
            status_code=404,
            media_type="application/json",
        )
    return _job_to_dict(job)


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: str,
    delete_results: bool = Query(False),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Response:
    """Delete a job by ID."""
    scheduler.remove_job(job_id)
    if delete_results:
        await result_store.delete_results(job_id)
    await job_store.delete_job(job_id)
    return Response(status_code=204)


@router.get("/results/{job_id}", response_model=None)
async def get_results(
    job_id: str,
    latest: bool = Query(True),
    run_id: Optional[str] = Query(None),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
):
    """Get results for a job."""
    # Check job exists.
    try:
        job = await job_store.get_job(job_id)
    except KeyError:
        return Response(
            content=_json_encode({"error": f"Job {job_id!r} not found"}),
            status_code=404,
            media_type="application/json",
        )

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
        return Response(
            content=_json_encode({"error": "Provide run_id when latest=false"}),
            status_code=400,
            media_type="application/json",
        )

    try:
        result = await result_store.get_result(job_id, run_id=run_id)
    except KeyError:
        return Response(
            content=_json_encode({"error": f"No results found for job {job_id!r}"}),
            status_code=404,
            media_type="application/json",
        )

    return {"job_id": job_id, "status": job.status.value, "results": result}


@router.get("/errors")
async def get_errors(
    project: Optional[str] = Query(None),
    job_id: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    error_type: Optional[str] = Query(None),
    error_store: ErrorStore = Depends(get_error_store),
):
    """Query error records with optional filters."""
    try:
        since_dt = datetime.fromisoformat(since) if since else None
    except ValueError:
        return Response(
            content=_json_encode({"error": f"Invalid 'since' format: {since!r}"}),
            status_code=400,
            media_type="application/json",
        )
    try:
        error_type_enum = ErrorType(error_type) if error_type else None
    except ValueError:
        return Response(
            content=_json_encode({"error": f"Invalid 'error_type': {error_type!r}"}),
            status_code=400,
            media_type="application/json",
        )

    filters = ErrorFilters(
        project=project,
        job_id=job_id,
        since=since_dt,
        error_type=error_type_enum,
    )
    errors = await error_store.query_errors(filters)
    return [
        {
            "job_id": e.job_id,
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


def _job_to_dict(job: Job) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "project": job.project,
        "name": job.name,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "schedule_cron": job.schedule_cron,
        "schedule_enabled": job.schedule_enabled,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "run_count": job.run_count,
    }


def _json_encode(data: Any) -> bytes:
    return json.dumps(data).encode("utf-8")
