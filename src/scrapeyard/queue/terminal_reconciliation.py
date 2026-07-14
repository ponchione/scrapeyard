"""Repair durable webhook intent for terminal runs from persisted state."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

from scrapeyard.config.loader import load_config
from scrapeyard.storage.protocols import JobStore, ResultStore
from scrapeyard.storage.types import (
    ResultMetadata,
    TerminalIntentAction,
    TerminalWebhookCandidate,
)
from scrapeyard.webhook.payload import (
    build_terminal_webhook_delivery,
    deterministic_delivery_id,
    should_fire,
)

logger = logging.getLogger(__name__)


class TerminalIntentReconciliationError(RuntimeError):
    """Raised when startup cannot safely determine or persist required intent."""


@dataclass(frozen=True, slots=True)
class TerminalIntentReconciliationSummary:
    """Operational counts from one complete terminal-intent repair pass."""

    inspected: int = 0
    required: int = 0
    existing: int = 0
    repaired: int = 0
    not_required: int = 0
    race_noop: int = 0
    metadata_missing: int = 0
    parent_converged: int = 0


def _event(candidate: TerminalWebhookCandidate) -> str:
    return f"job.{candidate.status.value}"


def _delivery_id(candidate: TerminalWebhookCandidate) -> str:
    return deterministic_delivery_id(
        job_id=candidate.job_id,
        run_id=candidate.run_id,
        event=_event(candidate),
    )


async def _load_result_metadata(
    result_store: ResultStore,
    candidate: TerminalWebhookCandidate,
) -> tuple[ResultMetadata | None, bool]:
    try:
        metadata = await result_store.get_result_metadata(
            candidate.job_id,
            candidate.run_id,
        )
    except Exception as exc:
        logger.warning(
            "Terminal webhook result metadata unavailable "
            "job_id=%s run_id=%s event=%s delivery_id=%s "
            "terminal_status=%s recovery_action=fallback_to_jobs_db "
            "error_type=%s",
            candidate.job_id,
            candidate.run_id,
            _event(candidate),
            _delivery_id(candidate),
            candidate.status.value,
            type(exc).__name__,
        )
        return None, True
    if metadata is None:
        logger.warning(
            "Terminal webhook result metadata missing "
            "job_id=%s run_id=%s event=%s delivery_id=%s "
            "terminal_status=%s recovery_action=fallback_to_jobs_db",
            candidate.job_id,
            candidate.run_id,
            _event(candidate),
            _delivery_id(candidate),
            candidate.status.value,
        )
        return None, True
    return metadata, False


async def reconcile_terminal_webhook_intents(
    *,
    job_store: JobStore,
    result_store: ResultStore,
    batch_size: int | None = None,
) -> TerminalIntentReconciliationSummary:
    """Ensure every applicable terminal run has one logical durable intent.

    Applicability comes from the raw YAML currently persisted with the job.
    Its SHA-256 must match the hash captured by the run; otherwise this pass
    fails closed because the current schema cannot reconstruct an older config.
    Result metadata is optional enrichment and never gates intent creation.
    """

    candidates = (
        await job_store.list_terminal_webhook_candidates()
        if batch_size is None
        else await job_store.list_terminal_webhook_candidates(limit=batch_size)
    )
    counts = {
        "required": 0,
        "existing": 0,
        "repaired": 0,
        "not_required": 0,
        "race_noop": 0,
        "metadata_missing": 0,
        "parent_converged": 0,
    }

    for candidate in candidates:
        event = _event(candidate)
        delivery_id = _delivery_id(candidate)
        logger.info(
            "Terminal webhook run inspected "
            "job_id=%s run_id=%s event=%s delivery_id=%s "
            "terminal_status=%s recovery_action=inspect_terminal_run",
            candidate.job_id,
            candidate.run_id,
            event,
            delivery_id,
            candidate.status.value,
        )
        try:
            if candidate.existing_delivery_id is not None:
                counts["required"] += 1
                result = await job_store.reconcile_terminal_webhook_candidate(
                    candidate,
                    None,
                )
                counts["parent_converged"] += int(result.parent_converged)
                if result.action is TerminalIntentAction.existing:
                    counts["existing"] += 1
                    logger.info(
                        "Terminal webhook intent already exists "
                        "job_id=%s run_id=%s event=%s delivery_id=%s "
                        "terminal_status=%s recovery_action=%s",
                        candidate.job_id,
                        candidate.run_id,
                        event,
                        result.delivery_id or candidate.existing_delivery_id,
                        candidate.status.value,
                        (
                            "existing_tombstone_noop"
                            if candidate.existing_delivery_scrubbed_at is not None
                            else "existing_intent_noop"
                        ),
                    )
                else:
                    counts["race_noop"] += 1
                    logger.info(
                        "Terminal webhook reconciliation CAS no-op "
                        "job_id=%s run_id=%s event=%s delivery_id=%s "
                        "terminal_status=%s recovery_action=existing_intent_race_noop",
                        candidate.job_id,
                        candidate.run_id,
                        event,
                        delivery_id,
                        candidate.status.value,
                    )
                continue

            config = await asyncio.to_thread(load_config, candidate.config_yaml)
            persisted_hash = hashlib.sha256(candidate.config_yaml.encode()).hexdigest()
            if persisted_hash != candidate.config_hash:
                raise ValueError(
                    "Persisted job config no longer matches the terminal run config hash"
                )

            webhook_required = (
                config.webhook is not None
                and should_fire(config.webhook, candidate.status)
            )
            delivery = None
            if webhook_required:
                counts["required"] += 1
                metadata: ResultMetadata | None = None
                metadata_missing = False
                metadata, metadata_missing = await _load_result_metadata(
                    result_store,
                    candidate,
                )
                counts["metadata_missing"] += int(metadata_missing)
                completed_at = candidate.completed_at or candidate.heartbeat_at
                delivery = build_terminal_webhook_delivery(
                    config=config,
                    job_id=candidate.job_id,
                    status=candidate.status,
                    run_id=candidate.run_id,
                    result_path=None if metadata is None else metadata.file_path,
                    result_count=(
                        candidate.record_count
                        if metadata is None or metadata.record_count is None
                        else metadata.record_count
                    ),
                    error_count=candidate.error_count,
                    started_at=candidate.started_at,
                    completed_at=completed_at,
                )

            result = await job_store.reconcile_terminal_webhook_candidate(
                candidate,
                delivery,
            )
            counts["parent_converged"] += int(result.parent_converged)

            if result.action is TerminalIntentAction.created:
                counts["repaired"] += 1
                logger.warning(
                    "Terminal webhook missing intent repaired "
                    "job_id=%s run_id=%s event=%s delivery_id=%s "
                    "terminal_status=%s recovery_action=missing_intent_repaired",
                    candidate.job_id,
                    candidate.run_id,
                    event,
                    result.delivery_id or delivery_id,
                    candidate.status.value,
                )
            elif result.action is TerminalIntentAction.existing:
                counts["existing"] += 1
                logger.info(
                    "Terminal webhook intent already exists "
                    "job_id=%s run_id=%s event=%s delivery_id=%s "
                    "terminal_status=%s recovery_action=existing_intent_noop",
                    candidate.job_id,
                    candidate.run_id,
                    event,
                    result.delivery_id or candidate.existing_delivery_id or delivery_id,
                    candidate.status.value,
                )
            elif result.action is TerminalIntentAction.not_required:
                counts["not_required"] += 1
                logger.info(
                    "Terminal webhook intent not required "
                    "job_id=%s run_id=%s event=%s delivery_id=%s "
                    "terminal_status=%s recovery_action=config_not_applicable_noop",
                    candidate.job_id,
                    candidate.run_id,
                    event,
                    delivery_id,
                    candidate.status.value,
                )
            else:
                counts["race_noop"] += 1
                logger.info(
                    "Terminal webhook reconciliation CAS no-op "
                    "job_id=%s run_id=%s event=%s delivery_id=%s "
                    "terminal_status=%s recovery_action=ownership_or_config_race_noop",
                    candidate.job_id,
                    candidate.run_id,
                    event,
                    delivery_id,
                    candidate.status.value,
                )
        except Exception as exc:
            logger.error(
                "Terminal webhook reconciliation failed "
                "job_id=%s run_id=%s event=%s delivery_id=%s "
                "terminal_status=%s recovery_action=abort_recovery_pass "
                "error_type=%s",
                candidate.job_id,
                candidate.run_id,
                event,
                delivery_id,
                candidate.status.value,
                type(exc).__name__,
            )
            raise TerminalIntentReconciliationError(
                "Terminal webhook intent reconciliation failed "
                f"for job_id={candidate.job_id!r} run_id={candidate.run_id!r}"
            ) from exc

    summary = TerminalIntentReconciliationSummary(
        inspected=len(candidates),
        required=counts["required"],
        existing=counts["existing"],
        repaired=counts["repaired"],
        not_required=counts["not_required"],
        race_noop=counts["race_noop"],
        metadata_missing=counts["metadata_missing"],
        parent_converged=counts["parent_converged"],
    )
    logger.info(
        "Terminal webhook reconciliation finished "
        "inspected_count=%s required_count=%s existing_count=%s "
        "repaired_count=%s not_required_count=%s race_noop_count=%s "
        "metadata_missing_count=%s parent_converged_count=%s "
        "recovery_action=reconciliation_complete",
        summary.inspected,
        summary.required,
        summary.existing,
        summary.repaired,
        summary.not_required,
        summary.race_noop,
        summary.metadata_missing,
        summary.parent_converged,
    )
    return summary
