"""API routes for scrape, jobs, results, and errors (spec section 4.1)."""

from __future__ import annotations

import asyncio
import hashlib
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
from scrapeyard.api.response_models import (
    APICompatibility,
    ERROR_RESPONSES,
    PAGINATION_HEADERS,
    ErrorRecordResponse,
    JobCreatedResponse,
    JobDetailResponse,
    JobSummaryResponse,
    LegacyResultResponse,
    ManualTriggerResponse,
    QueuedSubmissionResponse,
    ResultResponse,
    ScheduleStateResponse,
)
from scrapeyard.api.auth import AuthScope, authorize_request
from scrapeyard.api.scrape_submission import (
    IdempotencyConflictError,
    IdempotencyContext,
    ResultArtifactUnavailableError,
    submit_scrape_job,
)
from scrapeyard.api.job_lifecycle import (
    JobLifecycleRequestError,
    cancel_current_job,
    delete_reserved_job,
)
from scrapeyard.api.serializers import (
    serialize_error_record,
    serialize_job_created,
    serialize_job_detail,
    serialize_job_summary,
    serialize_results_payload,
    serialize_schedule_state,
    serialize_scrape_queued,
    serialize_scrape_result,
)
from scrapeyard.common.settings import get_settings
from scrapeyard.common.time import utc_now
from scrapeyard.common.yaml import MAX_YAML_NESTING
from scrapeyard.config.loader import load_config, load_config_project
from scrapeyard.config.schema import ScrapeConfig
from scrapeyard.engine.url_guard import redact_userinfo_in_text
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.pool import WorkerPool
from scrapeyard.scheduler.cron import (
    ManualTriggerConflictError,
    ScheduledJobLifecycleError,
    SchedulerService,
)
from scrapeyard.storage.job_store import DuplicateJobError
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.storage.types import (
    ScheduledJobMutationAction,
    ScheduledJobMutationOutcome,
)

router = APIRouter(responses=ERROR_RESPONSES)
logger = logging.getLogger(__name__)


def _register_schedule_snapshot(scheduler: SchedulerService, job: Job) -> None:
    if job.schedule_cron is None:
        raise RuntimeError("Scheduled job snapshot is missing its cron expression")
    scheduler.register_job(
        job.job_id,
        job.schedule_cron,
        timezone_name=job.schedule_timezone,
        enabled=job.schedule_enabled,
    )


async def _apply_scheduler_mutation(
    outcome: ScheduledJobMutationOutcome,
    *,
    job_store: JobStore,
    scheduler: SchedulerService,
) -> Job:
    if outcome.action is ScheduledJobMutationAction.missing:
        raise_json_error(404, "Scheduled job not found")
    if outcome.action is ScheduledJobMutationAction.not_scheduled:
        raise_json_error(409, "Job is not a scheduled job")
    if outcome.action is ScheduledJobMutationAction.project_conflict:
        raise_json_error(409, "A scheduled job's project cannot be changed")
    if outcome.action is ScheduledJobMutationAction.active_conflict:
        raise_json_error(409, "Scheduled config cannot change while a run is queued or active")
    if outcome.action is ScheduledJobMutationAction.lifecycle_conflict:
        raise_json_error(409, "Cancelled or deleting jobs cannot change schedule state")
    previous = outcome.previous
    current = outcome.current
    if previous is None or current is None:
        raise RuntimeError("Scheduled job mutation returned no state snapshot")
    try:
        _register_schedule_snapshot(scheduler, current)
    except Exception as exc:
        restored = await job_store.restore_scheduled_job(current, previous)
        if not restored:
            logger.critical(
                "Schedule compensation lost database ownership job_id=%s "
                "error_type=%s recovery_action=operator_reconcile",
                current.job_id,
                type(exc).__name__,
            )
            raise_json_error(503, "Schedule update failed and requires operator reconciliation")
        try:
            _register_schedule_snapshot(scheduler, previous)
        except Exception as restore_exc:
            logger.critical(
                "Schedule compensation failed to restore local scheduler job_id=%s "
                "error_type=%s recovery_action=restart_from_database",
                current.job_id,
                type(restore_exc).__name__,
            )
            raise_json_error(503, "Schedule update failed; restart scheduler from database state")
        raise_json_error(503, "Schedule update failed; persisted state was restored")
    return current


@dataclass(frozen=True)
class ParsedYamlConfig:
    config_yaml: str
    config: ScrapeConfig


def _is_yaml_request(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/x-yaml"


def _format_validation_error(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(part) for part in error.get("loc", ())) or "config"
        message = redact_userinfo_in_text(str(error.get("msg") or "Invalid value"))
        messages.append(f"{loc}: {message}")
    return "; ".join(messages) or "Validation failed"


def _format_yaml_error(exc: YAMLError) -> str:
    problem = getattr(exc, "problem", None)
    mark = getattr(exc, "problem_mark", None)
    if problem:
        location = ""
        if mark is not None:
            location = f" at line {mark.line + 1}, column {mark.column + 1}"
        return f"YAML parse error{location}: {redact_userinfo_in_text(str(problem))}"
    return redact_userinfo_in_text(str(exc)) or "YAML parse error"


def _format_config_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return _format_validation_error(exc)
    if isinstance(exc, YAMLError):
        return _format_yaml_error(exc)
    return redact_userinfo_in_text(str(exc)) or type(exc).__name__


async def _read_valid_yaml_config(
    request: Request,
    *,
    scope: AuthScope,
) -> ParsedYamlConfig:
    if not _is_yaml_request(request):
        raise_json_error(415, "Content-Type must be application/x-yaml")
    try:
        body = await request.body()
        config_yaml = body.decode("utf-8")
        project = await asyncio.to_thread(load_config_project, config_yaml)
        authorize_request(request, scope, project=project)
        config = await asyncio.to_thread(load_config, config_yaml)
    except RecursionError:
        raise_json_error(
            422,
            f"Invalid config: YAML nesting exceeds {MAX_YAML_NESTING} levels",
        )
    except (UnicodeDecodeError, ValidationError, TypeError, ValueError, YAMLError) as exc:
        raise_json_error(422, f"Invalid config: {_format_config_error(exc)}")
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


def _queued_scrape_response(
    job_id: str,
    *,
    run_id: str,
    status: str,
    poll_url: str,
    replayed: bool = False,
) -> Response:
    response = json_response(
        202,
        serialize_scrape_queued(
            job_id,
            run_id=run_id,
            status=status,
            poll_url=poll_url,
        ),
    )
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return response


def _idempotency_context(
    request: Request,
    *,
    max_bytes: int,
) -> IdempotencyContext | None:
    values = [
        value
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"idempotency-key"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise_json_error(400, "Provide exactly one Idempotency-Key header")
    raw_key = values[0]
    if not raw_key or len(raw_key) > max_bytes:
        raise_json_error(
            400,
            f"Idempotency-Key must contain 1 to {max_bytes} ASCII bytes",
        )
    if any(byte < 0x21 or byte > 0x7E for byte in raw_key):
        raise_json_error(400, "Idempotency-Key must contain visible ASCII characters")
    caller_scope = getattr(request.state, "caller_identity", "unauthenticated")
    return IdempotencyContext(
        caller_scope=caller_scope,
        key_digest=hashlib.sha256(raw_key).hexdigest(),
    )


@router.post(
    "/scrape",
    response_model=LegacyResultResponse | ResultResponse,
    responses={202: {"model": QueuedSubmissionResponse}},
)
async def scrape(
    request: Request,
    compatibility: APICompatibility = Query(APICompatibility.v1),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
    worker_pool: WorkerPool = Depends(get_worker_pool),
) -> Response:
    """Submit an ad-hoc scrape request."""
    authorize_request(request, AuthScope.submit)
    parsed = await _read_valid_yaml_config(request, scope=AuthScope.submit)
    config_yaml = parsed.config_yaml
    config = parsed.config

    settings = get_settings()
    idempotency = _idempotency_context(
        request,
        max_bytes=settings.idempotency_key_max_bytes,
    )
    try:
        submission = await submit_scrape_job(
            config_yaml=config_yaml,
            config=config,
            job_store=job_store,
            result_store=result_store,
            worker_pool=worker_pool,
            sync_timeout_seconds=settings.sync_timeout_seconds,
            sync_poll_delay_seconds=settings.sync_poll_delay_seconds,
            idempotency=idempotency,
            idempotency_retention_hours=settings.idempotency_retention_hours,
        )
    except IdempotencyConflictError:
        raise_json_error(409, "Idempotency-Key was already used with different request content")
    except ResultArtifactUnavailableError:
        raise_json_error(404, "Completed result artifact is no longer available")
    except MemoryError:
        logger.warning("Rejecting async scrape due to pool memory pressure")
        raise_json_error(503, "Server at capacity — try again later")

    if not submission.completed:
        return _queued_scrape_response(
            submission.job_id,
            run_id=submission.run_id,
            status=submission.status,
            poll_url=f"/results/{submission.job_id}",
            replayed=submission.replayed,
        )

    response = json_response(
        200,
        serialize_scrape_result(
            submission.job_id,
            run_id=submission.run_id,
            status=submission.status,
            results=submission.results,
            compatibility=compatibility,
        ),
    )
    if submission.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return response


@router.post("/jobs", status_code=201, response_model=JobCreatedResponse)
async def create_job(
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Any:
    """Create a scheduled job. Requires a schedule block in the config."""
    authorize_request(request, AuthScope.schedule_admin)
    parsed = await _read_valid_yaml_config(request, scope=AuthScope.schedule_admin)
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
        schedule_timezone=config.schedule.timezone,
        schedule_enabled=config.schedule.enabled,
    )
    try:
        await job_store.save_job(job)
    except DuplicateJobError as exc:
        raise_json_error(409, f"Job name {exc.name!r} already exists in project {exc.project!r}")

    try:
        _register_schedule_snapshot(scheduler, job)
    except Exception as exc:
        scheduler.remove_job(job.job_id)
        rolled_back = await job_store.rollback_scheduled_job_creation(job.job_id)
        if not rolled_back:
            logger.critical(
                "Scheduled creation compensation lost ownership job_id=%s "
                "error_type=%s recovery_action=operator_reconcile",
                job.job_id,
                type(exc).__name__,
            )
            raise_json_error(503, "Schedule registration failed and requires reconciliation")
        raise_json_error(503, "Schedule registration failed; job creation was rolled back")
    return serialize_job_created(job)


@router.put("/jobs/{job_id}", response_model=ScheduleStateResponse)
async def update_job(
    job_id: str,
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Any:
    """Replace a scheduled job's future-run config and schedule atomically."""

    authorize_request(request, AuthScope.schedule_admin)
    existing = await _get_job_or_404(job_store, job_id)
    authorize_request(request, AuthScope.schedule_admin, project=existing.project)
    parsed = await _read_valid_yaml_config(request, scope=AuthScope.schedule_admin)
    config = parsed.config
    if config.schedule is None:
        raise_json_error(400, "A 'schedule' block is required for scheduled job updates")
    try:
        outcome = await job_store.update_scheduled_job(
            job_id,
            project=config.project,
            name=config.name,
            config_yaml=parsed.config_yaml,
            schedule_cron=config.schedule.cron,
            schedule_timezone=config.schedule.timezone,
            schedule_enabled=config.schedule.enabled,
            updated_at=utc_now(),
        )
    except DuplicateJobError as exc:
        raise_json_error(409, f"Job name {exc.name!r} already exists in project {exc.project!r}")
    current = await _apply_scheduler_mutation(
        outcome,
        job_store=job_store,
        scheduler=scheduler,
    )
    return serialize_schedule_state(current)


async def _set_schedule_enabled(
    job_id: str,
    *,
    request: Request,
    enabled: bool,
    job_store: JobStore,
    scheduler: SchedulerService,
) -> dict[str, Any]:
    job = await _get_job_or_404(job_store, job_id)
    authorize_request(request, AuthScope.schedule_admin, project=job.project)
    outcome = await job_store.set_schedule_enabled(
        job_id,
        enabled=enabled,
        updated_at=utc_now(),
    )
    current = await _apply_scheduler_mutation(
        outcome,
        job_store=job_store,
        scheduler=scheduler,
    )
    return serialize_schedule_state(current)


@router.post("/jobs/{job_id}/pause", response_model=ScheduleStateResponse)
async def pause_job(
    job_id: str,
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> dict[str, Any]:
    """Pause future cron fires without affecting an accepted current run."""

    return await _set_schedule_enabled(
        job_id,
        request=request,
        enabled=False,
        job_store=job_store,
        scheduler=scheduler,
    )


@router.post("/jobs/{job_id}/resume", response_model=ScheduleStateResponse)
async def resume_job(
    job_id: str,
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> dict[str, Any]:
    """Resume future cron fires from persisted schedule state."""

    return await _set_schedule_enabled(
        job_id,
        request=request,
        enabled=True,
        job_store=job_store,
        scheduler=scheduler,
    )


@router.post(
    "/jobs/{job_id}/trigger",
    status_code=202,
    response_model=ManualTriggerResponse,
)
async def trigger_job(
    job_id: str,
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Any:
    """Queue an immediate manual run using the current persisted config version."""

    job = await _get_job_or_404(job_store, job_id)
    authorize_request(request, AuthScope.schedule_admin, project=job.project)
    try:
        run_id, config_hash = await scheduler.trigger_job_now(job_id)
    except KeyError:
        raise_json_error(404, f"Job {job_id!r} not found")
    except (ManualTriggerConflictError, ScheduledJobLifecycleError) as exc:
        raise_json_error(409, str(exc))
    except Exception:
        raise_json_error(503, "Manual trigger could not be enqueued")
    return {
        "job_id": job_id,
        "run_id": run_id,
        "status": "queued",
        "trigger": "manual",
        "config_hash": config_hash,
        "poll_url": f"/results/{job_id}",
    }


@router.get(
    "/jobs",
    response_model=list[JobSummaryResponse],
    responses={200: {"headers": PAGINATION_HEADERS}},
)
async def list_jobs(
    request: Request,
    response: Response,
    project: str | None = Query(None),
    limit: int | None = Query(None, ge=1),
    offset: int = Query(0, ge=0),
    job_store: JobStore = Depends(get_job_store),
) -> Any:
    """List jobs, optionally filtered by project."""
    caller = authorize_request(request, AuthScope.read, project=project)
    if caller.projects is not None and project is None:
        raise_json_error(403, "Project-scoped callers must provide a project filter")
    resolved_limit = _resolve_admin_read_limit(limit)

    rows = await job_store.list_jobs_with_stats(project, limit=resolved_limit + 1, offset=offset)
    rows = apply_paginated_list_response(response, rows=rows, limit=resolved_limit, offset=offset)
    return [
        serialize_job_summary(job, run_count=run_count, last_run_at=last_run_at)
        for job, run_count, last_run_at in rows
    ]


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: str,
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Any:
    """Get a single job by ID."""
    job = await _get_job_or_404(job_store, job_id)
    authorize_request(request, AuthScope.read, project=job.project)

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
    request: Request,
    delete_results: bool = Query(False),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
    error_store: ErrorStore = Depends(get_error_store),
    scheduler: SchedulerService = Depends(get_scheduler),
    worker_pool: WorkerPool = Depends(get_worker_pool),
) -> Response:
    """Reserve or resume safe deletion of a terminal/cancelled job."""
    authorize_request(request, AuthScope.delete)
    try:
        job = await job_store.get_job(job_id)
    except KeyError:
        job = None
    if job is not None:
        authorize_request(request, AuthScope.delete, project=job.project)
    try:
        await delete_reserved_job(
            job_id,
            delete_results=delete_results,
            job_store=job_store,
            result_store=result_store,
            error_store=error_store,
            worker_pool=worker_pool,
            scheduler=scheduler,
        )
    except JobLifecycleRequestError as exc:
        raise_json_error(exc.status_code, exc.message)
    return no_content_response()


@router.post("/jobs/{job_id}/cancel", status_code=204)
async def cancel_job(
    job_id: str,
    request: Request,
    job_store: JobStore = Depends(get_job_store),
    worker_pool: WorkerPool = Depends(get_worker_pool),
    scheduler: SchedulerService = Depends(get_scheduler),
) -> Response:
    """Cancel the current queued/running delivery and wait for quiescence."""
    job = await _get_job_or_404(job_store, job_id)
    authorize_request(request, AuthScope.delete, project=job.project)
    try:
        await cancel_current_job(
            job_id,
            job_store=job_store,
            worker_pool=worker_pool,
            scheduler=scheduler,
        )
    except JobLifecycleRequestError as exc:
        raise_json_error(exc.status_code, exc.message)
    return no_content_response()


@router.get(
    "/results/{job_id}",
    response_model=LegacyResultResponse | ResultResponse,
    responses={202: {"model": QueuedSubmissionResponse}},
)
async def get_results(
    job_id: str,
    request: Request,
    latest: bool = Query(True),
    run_id: str | None = Query(None),
    compatibility: APICompatibility = Query(APICompatibility.v1),
    job_store: JobStore = Depends(get_job_store),
    result_store: ResultStore = Depends(get_result_store),
) -> Any:
    """Get results for a job."""
    caller = authorize_request(request, AuthScope.read)
    if run_id is None and not latest:
        raise_json_error(400, "Provide run_id when latest=false")

    try:
        job = await job_store.get_job(job_id)
    except KeyError:
        job = None
    if job is not None:
        authorize_request(request, AuthScope.read, project=job.project)

    if (
        run_id is None
        and job is not None
        and job.current_run_id is not None
        and job.status in (JobStatus.queued, JobStatus.running)
    ):
        return _queued_scrape_response(
            job_id,
            run_id=job.current_run_id,
            status=job.status.value,
            poll_url=f"/jobs/{job_id}",
        )

    result_run_id = (
        run_id
        if run_id is not None
        else (None if job is None else job.current_run_id)
    )
    if job is None and caller.projects is not None:
        metadata = await result_store.get_result_metadata(job_id, result_run_id)
        if metadata is None or not caller.permits_project(metadata.project):
            # Retained-result ownership and absence intentionally share one
            # response so cross-project job/run IDs are not an enumeration oracle.
            raise_json_error(404, f"No results found for job {job_id!r}")
        result_run_id = metadata.run_id
    try:
        payload = await result_store.get_result(job_id, run_id=result_run_id)
    except (KeyError, FileNotFoundError):
        raise_json_error(404, f"No results found for job {job_id!r}")

    return serialize_results_payload(
        job_id,
        run_id=payload.run_id,
        status=payload.status,
        results=payload.data,
        compatibility=compatibility,
    )


@router.get(
    "/errors",
    response_model=list[ErrorRecordResponse],
    responses={200: {"headers": PAGINATION_HEADERS}},
)
async def get_errors(
    request: Request,
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
    caller = authorize_request(request, AuthScope.read, project=project)
    if caller.projects is not None and project is None:
        raise_json_error(403, "Project-scoped callers must provide a project filter")
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
