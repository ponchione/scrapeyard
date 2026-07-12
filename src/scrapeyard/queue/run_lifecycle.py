"""Run lifecycle helpers for worker orchestration."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from scrapeyard.common.budgets import RunBudget
from scrapeyard.common.paths import safe_join
from scrapeyard.common.settings import ServiceSettings
from scrapeyard.common.time import utc_now
from scrapeyard.config.schema import ScrapeConfig
from scrapeyard.models.job import JobStatus
from scrapeyard.storage.protocols import JobStore, ResultStore
from scrapeyard.storage.types import RunOwnershipError, SaveResultMeta
from scrapeyard.storage.webhook_outbox import WebhookDeliveryCreate
from scrapeyard.webhook.dispatcher import WebhookNotifier

logger = logging.getLogger(__name__)


def build_run_paths(
    settings: ServiceSettings,
    project: str,
    job_name: str,
    run_id: str | None,
) -> tuple[str, str | None]:
    adaptive_dir = str(safe_join(settings.adaptive_dir, project))
    browser_debug_enabled = bool(getattr(settings, "browser_debug_enabled", False))
    run_artifacts_dir = None if run_id is None or not browser_debug_enabled else str(
        safe_join(settings.storage_results_dir, project, job_name, run_id) / "artifacts"
    )
    return adaptive_dir, run_artifacts_dir


async def save_run_result(
    *,
    job_id: str,
    run_id: str | None,
    result_store: ResultStore,
    output_data: dict[str, Any],
    final_status: JobStatus,
    record_count: int,
    budget: RunBudget | None = None,
    max_serialized_bytes: int | None = None,
) -> SaveResultMeta:
    return await result_store.save_result(
        job_id,
        output_data,
        run_id=run_id,
        status=final_status.value,
        record_count=record_count,
        budget=budget,
        max_serialized_bytes=max_serialized_bytes,
    )


async def dispatch_webhook(
    *,
    webhook_dispatcher: WebhookNotifier | None,
    config: ScrapeConfig,
    delivery: WebhookDeliveryCreate | None,
) -> None:
    """Schedule an already-durable terminal delivery without affecting status."""
    if webhook_dispatcher is None or config.webhook is None or delivery is None:
        return
    try:
        await webhook_dispatcher.notify()
    except Exception as exc:
        logger.error(
            "Post-commit webhook scheduling failed "
            "job_id=%s run_id=%s event=%s delivery_id=%s "
            "terminal_status=%s recovery_action=leave_durable_intent_pending "
            "error_type=%s",
            delivery.job_id,
            delivery.run_id,
            delivery.event,
            delivery.delivery_id,
            delivery.payload.get("status"),
            type(exc).__name__,
        )


async def handle_crash(
    job_id: str,
    run_id: str | None,
    job_store: JobStore,
    *,
    failed_at: datetime | None = None,
    heartbeat_cutoff: datetime | None = None,
    error_count: int = 0,
    webhook_delivery: WebhookDeliveryCreate | None = None,
) -> bool:
    """Best-effort crash recovery guarded by the expected active run."""
    if run_id is None:
        logger.warning(
            "Cannot conditionally fail delivery without run ownership job_id=%s",
            job_id,
        )
        return False
    try:
        await job_store.fail_owned_run(
            job_id,
            run_id,
            failed_at or utc_now(),
            heartbeat_cutoff=heartbeat_cutoff,
            error_count=error_count,
            webhook_delivery=webhook_delivery,
        )
        return True
    except RunOwnershipError:
        logger.info(
            "Skipping crash finalization after ownership loss "
            "job_id=%s run_id=%s event=%s delivery_id=%s terminal_status=%s "
            "recovery_action=cas_ownership_race_noop ownership_outcome=lost",
            job_id,
            run_id,
            webhook_delivery.event if webhook_delivery is not None else "job.failed",
            (
                webhook_delivery.delivery_id
                if webhook_delivery is not None
                else None
            ),
            JobStatus.failed.value,
        )
        return False
    except Exception:
        logger.exception(
            "Failed conditional crash finalization job_id=%s run_id=%s",
            job_id,
            run_id,
        )
        return False
