"""Test result retention auto-cleanup."""

import asyncio

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scrapeyard.storage.cleanup import CleanupIncompleteError, run_cleanup
from scrapeyard.storage.types import ResultReconciliationReport
from scrapeyard.storage.webhook_outbox import WebhookRetentionSummary


@pytest.mark.asyncio
async def test_run_cleanup_delegates_age_based_deletion():
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=1)
    result_store.prune_excess_per_job = AsyncMock(return_value=0)
    result_store.reconcile_artifacts = AsyncMock(
        return_value=ResultReconciliationReport(dry_run=True)
    )

    await run_cleanup(result_store, retention_days=30, max_results_per_job=100)

    result_store.delete_expired.assert_awaited_once_with(30)
    result_store.prune_excess_per_job.assert_awaited_once_with(100)


@pytest.mark.asyncio
async def test_run_cleanup_delegates_per_job_pruning():
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=0)
    result_store.prune_excess_per_job = AsyncMock(return_value=2)
    result_store.reconcile_artifacts = AsyncMock(
        return_value=ResultReconciliationReport(dry_run=True)
    )

    await run_cleanup(result_store, retention_days=14, max_results_per_job=3)

    result_store.delete_expired.assert_awaited_once_with(14)
    result_store.prune_excess_per_job.assert_awaited_once_with(3)


@pytest.mark.asyncio
async def test_run_cleanup_removes_bounded_expired_idempotency_records():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True
    )
    job_store = AsyncMock()
    job_store.delete_expired_idempotency_records.return_value = 2
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        job_store=job_store,
        idempotency_cleanup_batch_size=25,
        now=now,
    )

    job_store.delete_expired_idempotency_records.assert_awaited_once_with(
        now,
        limit=25,
    )


@pytest.mark.asyncio
async def test_run_cleanup_scrubs_bounded_terminal_webhook_rows():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True
    )
    outbox = AsyncMock()
    outbox.scrub_terminal_deliveries.return_value = WebhookRetentionSummary(2, 3)
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)

    await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        webhook_outbox_store=outbox,
        webhook_delivered_retention_days=7,
        webhook_failed_retention_days=30,
        webhook_cleanup_batch_size=25,
        now=now,
    )

    outbox.scrub_terminal_deliveries.assert_awaited_once_with(
        delivered_before=now - timedelta(days=7),
        failed_before=now - timedelta(days=30),
        scrubbed_at=now,
        limit=25,
    )


@pytest.mark.asyncio
async def test_webhook_cleanup_failure_is_contained(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True
    )
    outbox = AsyncMock()
    outbox.scrub_terminal_deliveries.side_effect = RuntimeError("secret text")

    with pytest.raises(CleanupIncompleteError, match="webhook_retention"):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
            webhook_outbox_store=outbox,
            webhook_delivered_retention_days=7,
            webhook_failed_retention_days=30,
        )

    assert "error_type=RuntimeError" in caplog.text
    assert "secret text" not in caplog.text


@pytest.mark.asyncio
async def test_reconciliation_failure_is_contained_before_webhook_cleanup(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.side_effect = RuntimeError("secret text")
    outbox = AsyncMock()
    outbox.scrub_terminal_deliveries.return_value = WebhookRetentionSummary(0, 0)

    with pytest.raises(CleanupIncompleteError, match="artifact_reconciliation"):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
            webhook_outbox_store=outbox,
            webhook_delivered_retention_days=7,
            webhook_failed_retention_days=30,
        )

    assert "error_type=RuntimeError" in caplog.text
    assert "secret text" not in caplog.text
    outbox.scrub_terminal_deliveries.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_result_failure_does_not_skip_later_cleanup_phases(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.side_effect = RuntimeError("secret text")
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True
    )
    job_store = AsyncMock()
    job_store.delete_expired_idempotency_records.return_value = 0
    outbox = AsyncMock()
    outbox.scrub_terminal_deliveries.return_value = WebhookRetentionSummary(0, 0)

    with pytest.raises(CleanupIncompleteError, match="expired_result_retention"):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
            webhook_outbox_store=outbox,
            webhook_delivered_retention_days=7,
            webhook_failed_retention_days=30,
            job_store=job_store,
        )

    result_store.prune_excess_per_job.assert_awaited_once_with(100)
    result_store.reconcile_artifacts.assert_awaited_once()
    job_store.delete_expired_idempotency_records.assert_awaited_once()
    outbox.scrub_terminal_deliveries.assert_awaited_once()
    assert "error_type=RuntimeError" in caplog.text
    assert "secret text" not in caplog.text


@pytest.mark.asyncio
async def test_per_job_result_failure_does_not_skip_later_cleanup_phases(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.side_effect = RuntimeError("secret text")
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True
    )
    outbox = AsyncMock()
    outbox.scrub_terminal_deliveries.return_value = WebhookRetentionSummary(0, 0)

    with pytest.raises(CleanupIncompleteError, match="per_job_result_retention"):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
            webhook_outbox_store=outbox,
            webhook_delivered_retention_days=7,
            webhook_failed_retention_days=30,
        )

    result_store.reconcile_artifacts.assert_awaited_once()
    outbox.scrub_terminal_deliveries.assert_awaited_once()
    assert "error_type=RuntimeError" in caplog.text
    assert "secret text" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["delete_expired", "prune_excess_per_job"])
async def test_result_retention_cancellation_propagates(phase):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    getattr(result_store, phase).side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
        )


@pytest.mark.asyncio
async def test_reconciliation_cancellation_propagates():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
        )
