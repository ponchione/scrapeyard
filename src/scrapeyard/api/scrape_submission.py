"""Service helpers for ad-hoc scrape submission policy."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.paths import MAX_PATH_PART_BYTES, safe_path_part
from scrapeyard.common.qualification import qualification_checkpoint
from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import ExecutionMode, FetcherType
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.delivery import queue_delivery_metadata
from scrapeyard.queue.pool import QueueJobHandle, WorkerPool
from scrapeyard.storage.job_store import DuplicateJobError
from scrapeyard.storage.protocols import JobStore, ResultStore
from scrapeyard.storage.types import IdempotentJobAction

_ADHOC_JOB_SAVE_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class ScrapeSubmission:
    job_id: str
    run_id: str
    status: str
    completed: bool
    results: Any | None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class IdempotencyContext:
    """Opaque caller/key digests used to scope durable deduplication."""

    caller_scope: str
    key_digest: str


class IdempotencyConflictError(RuntimeError):
    """Raised when one caller reuses a live key for different YAML."""


class ResultArtifactUnavailableError(RuntimeError):
    """Raised when a terminal synchronous run has no readable result artifact."""


async def submit_scrape_job(
    *,
    config_yaml: str,
    config: Any,
    job_store: JobStore,
    result_store: ResultStore,
    worker_pool: WorkerPool,
    sync_timeout_seconds: int,
    sync_poll_delay_seconds: float,
    idempotency: IdempotencyContext | None = None,
    idempotency_retention_hours: int = 24,
) -> ScrapeSubmission:
    wait_for_completion = should_wait_for_completion(config)
    job, should_enqueue, replayed = await _save_adhoc_job(
        config_yaml=config_yaml,
        config=config,
        job_store=job_store,
        idempotency=idempotency,
        response_mode="sync" if wait_for_completion else "async",
        idempotency_retention_hours=idempotency_retention_hours,
    )

    queued_job: QueueJobHandle | None = None
    if should_enqueue:
        delivery = queue_delivery_metadata(config)
        try:
            queued_job = await worker_pool.enqueue(
                job.job_id,
                config_yaml,
                delivery.priority,
                needs_browser=delivery.needs_browser,
                run_id=job.current_run_id,
                trigger="adhoc",
            )
            qualification_checkpoint("after_enqueue_before_claim")
        except Exception:
            with suppress(Exception):
                if job.current_run_id is not None:
                    await job_store.rollback_queued_submission(
                        job.job_id,
                        job.current_run_id,
                    )
            raise

    if not wait_for_completion:
        return ScrapeSubmission(
            job_id=job.job_id,
            run_id=_require_run_id(job),
            status=job.status.value,
            completed=False,
            results=None,
            replayed=replayed,
        )

    if queued_job is not None:
        completed = await wait_for_queued_job(
            queued_job,
            timeout_seconds=sync_timeout_seconds,
            poll_delay_seconds=sync_poll_delay_seconds,
        )
    else:
        completed = await wait_for_persisted_job(
            job.job_id,
            job_store=job_store,
            timeout_seconds=sync_timeout_seconds,
            poll_delay_seconds=sync_poll_delay_seconds,
        )
    if not completed:
        updated_job = await job_store.get_job(job.job_id)
        return ScrapeSubmission(
            job_id=job.job_id,
            run_id=_require_run_id(updated_job),
            status=updated_job.status.value,
            completed=False,
            results=None,
            replayed=replayed,
        )

    updated_job = await job_store.get_job(job.job_id)
    try:
        payload = await result_store.get_result(
            job.job_id,
            run_id=updated_job.current_run_id,
        )
    except (KeyError, FileNotFoundError) as exc:
        raise ResultArtifactUnavailableError from exc
    return ScrapeSubmission(
        job_id=job.job_id,
        run_id=_require_run_id(updated_job),
        status=updated_job.status.value,
        completed=True,
        results=payload.data,
        replayed=replayed,
    )


async def _save_adhoc_job(
    *,
    config_yaml: str,
    config: Any,
    job_store: JobStore,
    idempotency: IdempotencyContext | None,
    response_mode: str,
    idempotency_retention_hours: int,
) -> tuple[Job, bool, bool]:
    last_duplicate: DuplicateJobError | None = None
    for _ in range(_ADHOC_JOB_SAVE_ATTEMPTS):
        job = Job(
            job_id=str(uuid.uuid4()),
            project=config.project,
            name=_adhoc_job_name(config.name),
            config_yaml=config_yaml,
            updated_at=utc_now(),
            current_run_id=generate_run_id(),
            current_trigger="adhoc",
        )
        try:
            if idempotency is None:
                await job_store.save_job(job)
                return job, True, False
            outcome = await job_store.create_idempotent_job(
                job,
                caller_scope=idempotency.caller_scope,
                key_digest=idempotency.key_digest,
                request_hash=hashlib.sha256(config_yaml.encode("utf-8")).hexdigest(),
                response_mode=response_mode,
                expires_at=job.created_at + timedelta(hours=idempotency_retention_hours),
            )
        except DuplicateJobError as exc:
            last_duplicate = exc
            continue
        if outcome.action is IdempotentJobAction.conflict:
            raise IdempotencyConflictError
        if outcome.action is IdempotentJobAction.matched:
            return outcome.job, False, True
        return outcome.job, True, False
    if last_duplicate is not None:
        raise last_duplicate
    raise RuntimeError("Unable to save ad-hoc job")


def _require_run_id(job: Job) -> str:
    if job.current_run_id is None:
        raise RuntimeError("Ad-hoc submission is missing its persisted run ID")
    return job.current_run_id


def _adhoc_job_name(config_name: str) -> str:
    """Return a unique ad-hoc job name that remains safe as one path segment."""
    config_name = safe_path_part(config_name, label="config name")
    suffix = f"-{uuid.uuid4().hex[:8]}"
    max_base_bytes = MAX_PATH_PART_BYTES - len(suffix.encode("utf-8"))
    base = config_name.encode("utf-8")[:max_base_bytes].decode("utf-8", errors="ignore")
    return safe_path_part(f"{base}{suffix}", label="ad-hoc job name")


def should_wait_for_completion(config: Any) -> bool:
    """Determine if a scrape should wait on queued completion."""
    if config.execution.mode == ExecutionMode.sync:
        return True
    if config.execution.mode == ExecutionMode.async_:
        return False
    targets = config.resolved_targets()
    if len(targets) != 1:
        return False
    target = targets[0]
    if target.pagination is not None:
        return False
    return bool(target.fetcher == FetcherType.basic)


async def wait_for_queued_job(
    queued_job: QueueJobHandle,
    *,
    timeout_seconds: int,
    poll_delay_seconds: float,
) -> bool:
    result_task = asyncio.ensure_future(
        queued_job.result(timeout=timeout_seconds, poll_delay=poll_delay_seconds)
    )
    try:
        await asyncio.shield(result_task)
    except asyncio.TimeoutError:
        return False
    except asyncio.CancelledError:
        if result_task.done():
            # arq reports an aborted job using CancelledError. That is a
            # terminal queue outcome, not cancellation of this HTTP request.
            return False
        result_task.cancel()
        with suppress(asyncio.CancelledError):
            await result_task
        raise
    return True


async def wait_for_persisted_job(
    job_id: str,
    *,
    job_store: JobStore,
    timeout_seconds: float,
    poll_delay_seconds: float,
) -> bool:
    """Wait for a duplicate sync request without acquiring another queue handle."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        job = await job_store.get_job(job_id)
        if job.status in {JobStatus.complete, JobStatus.partial, JobStatus.failed}:
            return True
        if job.status in {JobStatus.cancelled, JobStatus.deleting}:
            return False
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll_delay_seconds, remaining))
