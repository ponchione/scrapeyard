"""Service helpers for ad-hoc scrape submission policy."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.paths import MAX_PATH_PART_BYTES, safe_path_part
from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import ExecutionMode, FetcherType
from scrapeyard.models.job import Job
from scrapeyard.queue.pool import QueueJobHandle, WorkerPool
from scrapeyard.storage.protocols import JobStore, ResultStore


@dataclass(frozen=True, slots=True)
class ScrapeSubmission:
    job_id: str
    status: str
    completed: bool
    results: Any | None


async def submit_scrape_job(
    *,
    config_yaml: str,
    config: Any,
    job_store: JobStore,
    result_store: ResultStore,
    worker_pool: WorkerPool,
    sync_timeout_seconds: int,
    sync_poll_delay_seconds: float,
) -> ScrapeSubmission:
    job_name = _adhoc_job_name(config.name)
    job = Job(
        job_id=str(uuid.uuid4()),
        project=config.project,
        name=job_name,
        config_yaml=config_yaml,
        updated_at=utc_now(),
        current_run_id=generate_run_id(),
    )
    await job_store.save_job(job)

    has_browser_target = any(target.fetcher != FetcherType.basic for target in config.resolved_targets())
    try:
        queued_job = await worker_pool.enqueue(
            job.job_id,
            config_yaml,
            config.execution.priority.value,
            needs_browser=has_browser_target,
            run_id=job.current_run_id,
            trigger="adhoc",
        )
    except Exception:
        with suppress(Exception):
            await job_store.delete_job(job.job_id)
        raise

    if not should_wait_for_completion(config):
        return ScrapeSubmission(job_id=job.job_id, status="queued", completed=False, results=None)

    completed = await wait_for_queued_job(
        queued_job,
        timeout_seconds=sync_timeout_seconds,
        poll_delay_seconds=sync_poll_delay_seconds,
    )
    if not completed:
        return ScrapeSubmission(job_id=job.job_id, status="queued", completed=False, results=None)

    try:
        payload = await result_store.get_result(job.job_id)
    except KeyError:
        payload = None
    updated_job = await job_store.get_job(job.job_id)
    return ScrapeSubmission(
        job_id=job.job_id,
        status=updated_job.status.value,
        completed=True,
        results=payload.data if payload else None,
    )


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
    try:
        await queued_job.result(timeout=timeout_seconds, poll_delay=poll_delay_seconds)
    except asyncio.TimeoutError:
        return False
    return True
