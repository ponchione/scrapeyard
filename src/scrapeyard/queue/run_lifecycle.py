"""Run lifecycle helpers for worker orchestration."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

from scrapeyard.common.paths import safe_join
from scrapeyard.common.time import utc_now
from scrapeyard.models.job import Job, JobStatus
from scrapeyard.queue.job_state import build_completed_job, build_failed_job, build_running_job
from scrapeyard.storage.protocols import ErrorStore, JobStore, ResultStore
from scrapeyard.webhook.dispatcher import WebhookDispatcher
from scrapeyard.webhook.payload import build_webhook_payload, should_fire

logger = logging.getLogger(__name__)


def build_run_paths(settings: Any, project: str, job_name: str, run_id: str | None) -> tuple[str, str | None]:
    adaptive_dir = str(safe_join(settings.adaptive_dir, project))
    run_artifacts_dir = None if run_id is None else str(
        safe_join(settings.storage_results_dir, project, job_name, run_id) / "artifacts"
    )
    return adaptive_dir, run_artifacts_dir


async def mark_job_running(job_store: JobStore, job: Job, started_at: datetime) -> Job:
    running_job = build_running_job(job, started_at=started_at)
    await job_store.update_job_status(running_job)
    return running_job


async def create_run_record(
    job_store: JobStore,
    *,
    run_id: str | None,
    job_id: str,
    trigger: str,
    config_yaml: str,
    started_at: datetime,
) -> None:
    if run_id is None:
        return
    config_hash = hashlib.sha256(config_yaml.encode()).hexdigest()
    await job_store.create_run(run_id, job_id, trigger, config_hash, started_at)


async def save_run_result(
    *,
    job_id: str,
    run_id: str | None,
    result_store: ResultStore,
    output_data: dict[str, Any],
    final_status: JobStatus,
    record_count: int,
) -> Any:
    return await result_store.save_result(
        job_id,
        output_data,
        run_id=run_id,
        status=final_status.value,
        record_count=record_count,
    )


async def update_job_completion(
    job_store: JobStore,
    job: Job,
    final_status: JobStatus,
    completed_at: datetime,
    run_id: str | None,
) -> None:
    completed_job = build_completed_job(
        job,
        final_status=final_status,
        completed_at=completed_at,
        run_id=run_id,
    )
    await job_store.update_job_status(completed_job)


async def finalize_run(
    run_id: str | None,
    final_status: JobStatus,
    record_count: int,
    job_store: JobStore,
    error_store: ErrorStore,
) -> None:
    """Finalize a run row with status, counts, and completed_at.

    Cross-DB safety: error_count comes from errors.db, finalize_run writes
    to jobs.db. If the write fails the run would be stuck in ``running``
    forever, so we catch the exception and fall back to ``fail_run``.
    """
    if run_id is None:
        return
    error_count = await error_store.count_errors_for_run(run_id)
    try:
        await job_store.finalize_run(run_id, final_status.value, record_count, error_count)
    except Exception:
        logger.critical(
            "Failed to finalize run %s (status=%s) — falling back to fail_run",
            run_id,
            final_status.value,
            exc_info=True,
        )
        try:
            await job_store.fail_run(run_id)
        except Exception:
            logger.critical(
                "fail_run fallback also failed for run %s — run stuck in 'running'",
                run_id,
                exc_info=True,
            )


async def dispatch_webhook(
    *,
    webhook_dispatcher: WebhookDispatcher | None,
    config: Any,
    job_id: str,
    final_status: JobStatus,
    save_meta: Any,
    all_errors: list[str],
    started_at: datetime,
    completed_at: datetime,
) -> None:
    """Submit webhook payload if configured and status matches trigger conditions."""
    if webhook_dispatcher is None or config.webhook is None:
        return
    if not should_fire(config.webhook, final_status):
        return
    payload = build_webhook_payload(
        job_id=job_id,
        project=config.project,
        name=config.name,
        status=final_status,
        run_id=save_meta.run_id if save_meta else None,
        result_path=save_meta.file_path if save_meta else None,
        result_count=save_meta.record_count if save_meta else 0,
        error_count=len(all_errors),
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
    )
    await webhook_dispatcher.submit(config.webhook, payload)


async def handle_crash(
    job_id: str,
    run_id: str | None,
    job_store: JobStore,
) -> None:
    """Best-effort crash recovery: mark job and run as failed."""
    try:
        job = await job_store.get_job(job_id)
        failed_job = build_failed_job(job, failed_at=utc_now())
        await job_store.update_job_status(failed_job)
    except Exception:
        logger.exception("Failed to mark job %s as failed", job_id)

    if run_id is not None:
        try:
            await job_store.fail_run(run_id)
        except Exception:
            logger.exception("Failed to finalize run %s", run_id)
