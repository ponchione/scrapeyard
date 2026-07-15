"""Test result retention auto-cleanup."""

import asyncio

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scrapeyard.storage.cleanup import (
    CleanupCycleOutcome,
    CleanupIncompleteError,
    HistoryRetentionPolicy,
    run_cleanup,
)
from scrapeyard.models.job import JobStatus
from scrapeyard.runtime.metrics import CLEANUP_ARTIFACT_FINDINGS
from scrapeyard.storage.types import (
    DeletionFinalizationAction,
    DeletionFinalizationOutcome,
    DeletionReservationAction,
    DeletionReservationOutcome,
    HistoryPruneResult,
    ResultArtifactFailure,
    ResultArtifactFailureKind,
    ReconciliationOperationFailure,
    ResultReconciliationReport,
)
from scrapeyard.storage.webhook_outbox import WebhookRetentionSummary


def _history_policy() -> HistoryRetentionPolicy:
    return HistoryRetentionPolicy(
        adhoc_job_retention_days=30,
        scheduled_run_retention_days=30,
        scheduled_run_retention_count=100,
        error_retention_days=30,
        webhook_tombstone_retention_days=30,
        adhoc_job_batch_size=10,
        scheduled_run_batch_size=20,
        error_batch_size=25,
    )


def _history_cleanup_mocks():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)
    job_store = AsyncMock()
    job_store.delete_expired_idempotency_records.return_value = 0
    job_store.list_adhoc_jobs_for_retention.return_value = []
    job_store.list_scheduled_runs_for_retention.return_value = []
    error_store = AsyncMock()
    error_store.delete_expired_errors.return_value = 0
    return result_store, job_store, error_store


@pytest.mark.asyncio
async def test_run_cleanup_delegates_age_based_deletion():
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=1)
    result_store.prune_excess_per_job = AsyncMock(return_value=0)
    result_store.reconcile_artifacts = AsyncMock(
        return_value=ResultReconciliationReport(dry_run=True)
    )

    await run_cleanup(result_store, retention_days=30, max_results_per_job=100)

    result_store.delete_expired.assert_awaited_once_with(30, limit=500)
    result_store.prune_excess_per_job.assert_awaited_once_with(100, limit=500)


@pytest.mark.asyncio
async def test_run_cleanup_delegates_per_job_pruning():
    result_store = AsyncMock()
    result_store.delete_expired = AsyncMock(return_value=0)
    result_store.prune_excess_per_job = AsyncMock(return_value=2)
    result_store.reconcile_artifacts = AsyncMock(
        return_value=ResultReconciliationReport(dry_run=True)
    )

    await run_cleanup(result_store, retention_days=14, max_results_per_job=3)

    result_store.delete_expired.assert_awaited_once_with(14, limit=500)
    result_store.prune_excess_per_job.assert_awaited_once_with(3, limit=500)


@pytest.mark.asyncio
async def test_run_cleanup_drains_repeated_full_result_batches():
    result_store = AsyncMock()
    result_store.delete_expired.side_effect = [500, 500, 7]
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)

    outcome = await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
    )

    assert outcome == CleanupCycleOutcome(saturated=False, processed_items=1007)
    assert result_store.delete_expired.await_count == 3


@pytest.mark.asyncio
async def test_run_cleanup_reports_saturation_at_per_phase_cycle_budget():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 500
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)

    outcome = await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        cycle_max_items_per_phase=1000,
    )

    assert outcome == CleanupCycleOutcome(saturated=True, processed_items=1000)
    assert result_store.delete_expired.await_count == 2


@pytest.mark.asyncio
async def test_run_cleanup_drains_artifact_backlog_larger_than_one_batch():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.side_effect = [
        ResultReconciliationReport(
            dry_run=True,
            metadata_rows_inspected=2,
            filesystem_entries_inspected=2,
            metadata_scan_exhausted=False,
            filesystem_scan_exhausted=False,
        ),
        ResultReconciliationReport(
            dry_run=True,
            metadata_rows_inspected=1,
            filesystem_entries_inspected=1,
        ),
    ]

    outcome = await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        result_cleanup_batch_size=2,
    )

    assert outcome == CleanupCycleOutcome(saturated=False, processed_items=6)
    assert result_store.reconcile_artifacts.await_count == 2


@pytest.mark.asyncio
async def test_run_cleanup_drains_artifact_scans_independently():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.side_effect = [
        ResultReconciliationReport(
            dry_run=True,
            metadata_rows_inspected=1,
            filesystem_entries_inspected=2,
            metadata_scan_exhausted=True,
            filesystem_scan_exhausted=False,
        ),
        ResultReconciliationReport(
            dry_run=True,
            filesystem_entries_inspected=1,
        ),
    ]

    outcome = await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        result_cleanup_batch_size=2,
    )

    assert outcome == CleanupCycleOutcome(saturated=False, processed_items=4)
    assert result_store.reconcile_artifacts.await_args_list[1].kwargs[
        "metadata_scan_limit"
    ] == 0
    assert result_store.reconcile_artifacts.await_args_list[1].kwargs[
        "filesystem_scan_limit"
    ] == 2


@pytest.mark.asyncio
async def test_run_cleanup_artifact_item_ceiling_marks_cycle_saturated():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True,
        metadata_rows_inspected=2,
        filesystem_entries_inspected=2,
        metadata_scan_exhausted=False,
        filesystem_scan_exhausted=False,
    )

    outcome = await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        result_cleanup_batch_size=2,
        cycle_max_items_per_phase=4,
    )

    assert outcome == CleanupCycleOutcome(saturated=True, processed_items=8)
    assert result_store.reconcile_artifacts.await_count == 2


@pytest.mark.asyncio
async def test_run_cleanup_artifact_time_ceiling_marks_cycle_saturated(monkeypatch):
    elapsed = 0.0
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0

    async def reconcile_page(**_kwargs):
        nonlocal elapsed
        elapsed = 1.0
        return ResultReconciliationReport(
            dry_run=True,
            metadata_rows_inspected=2,
            filesystem_entries_inspected=2,
            metadata_scan_exhausted=False,
            filesystem_scan_exhausted=False,
        )

    result_store.reconcile_artifacts.side_effect = reconcile_page
    monkeypatch.setattr("scrapeyard.storage.cleanup.time.monotonic", lambda: elapsed)

    outcome = await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        result_cleanup_batch_size=2,
        cycle_max_seconds=0.5,
    )

    assert outcome == CleanupCycleOutcome(saturated=True, processed_items=4)
    result_store.reconcile_artifacts.assert_awaited_once()
    assert result_store.reconcile_artifacts.await_args.kwargs["deadline"] == 0.5


@pytest.mark.asyncio
async def test_run_cleanup_removes_bounded_expired_idempotency_records():
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)
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
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)
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
async def test_adhoc_history_cleanup_drains_error_batches_without_deleting_results():
    result_store, job_store, error_store = _history_cleanup_mocks()
    job_store.list_adhoc_jobs_for_retention.return_value = ["old-job"]
    job_store.reserve_job_deletion.side_effect = [
        DeletionReservationOutcome(
            DeletionReservationAction.created,
            "old-job",
            "run",
            JobStatus.complete,
            False,
        ),
        DeletionReservationOutcome(
            DeletionReservationAction.resumed,
            "old-job",
            "run",
            JobStatus.deleting,
            False,
        ),
    ]
    error_store.delete_errors_for_job_batch.side_effect = [(25, True), (2, False)]
    job_store.finalize_job_deletion.return_value = DeletionFinalizationOutcome(
        DeletionFinalizationAction.deleted,
        "old-job",
    )
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        job_store=job_store,
        error_store=error_store,
        history_policy=_history_policy(),
        now=now,
    )
    job_store.finalize_job_deletion.assert_awaited_once_with(
        "old-job",
        delete_results=False,
        preserve_idempotency_after=now,
    )
    assert job_store.reserve_job_deletion.await_count == 2
    assert error_store.delete_errors_for_job_batch.await_count == 2
    result_store.delete_results.assert_not_awaited()


@pytest.mark.asyncio
async def test_adhoc_history_finalization_failure_retries_persisted_reservation():
    result_store, job_store, error_store = _history_cleanup_mocks()
    job_store.list_adhoc_jobs_for_retention.return_value = ["old-job"]
    job_store.reserve_job_deletion.side_effect = [
        DeletionReservationOutcome(
            DeletionReservationAction.created,
            "old-job",
            "run",
            JobStatus.complete,
            False,
        ),
        DeletionReservationOutcome(
            DeletionReservationAction.resumed,
            "old-job",
            "run",
            JobStatus.deleting,
            False,
        ),
    ]
    error_store.delete_errors_for_job_batch.return_value = (0, False)
    job_store.finalize_job_deletion.side_effect = [
        RuntimeError("database unavailable"),
        DeletionFinalizationOutcome(DeletionFinalizationAction.deleted, "old-job"),
    ]

    with pytest.raises(CleanupIncompleteError, match="adhoc_finalization"):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
            job_store=job_store,
            error_store=error_store,
            history_policy=_history_policy(),
        )

    await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        job_store=job_store,
        error_store=error_store,
        history_policy=_history_policy(),
    )
    assert job_store.finalize_job_deletion.await_count == 2


@pytest.mark.asyncio
async def test_scheduled_history_error_failure_prevents_prune_until_retry():
    result_store, job_store, error_store = _history_cleanup_mocks()
    job_store.list_scheduled_runs_for_retention.return_value = [("job", "run")]
    error_store.delete_errors_for_run_batch.side_effect = [
        RuntimeError("errors database unavailable"),
        (0, False),
    ]
    job_store.prune_scheduled_run_for_retention.return_value = HistoryPruneResult(
        True,
        1,
    )

    with pytest.raises(CleanupIncompleteError, match="scheduled_errors"):
        await run_cleanup(
            result_store,
            retention_days=30,
            max_results_per_job=100,
            job_store=job_store,
            error_store=error_store,
            history_policy=_history_policy(),
        )
    job_store.prune_scheduled_run_for_retention.assert_not_awaited()

    await run_cleanup(
        result_store,
        retention_days=30,
        max_results_per_job=100,
        job_store=job_store,
        error_store=error_store,
        history_policy=_history_policy(),
    )
    job_store.prune_scheduled_run_for_retention.assert_awaited_once()


@pytest.mark.asyncio
async def test_webhook_cleanup_failure_is_contained(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)
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
            reconciliation_dry_run=False,
        )

    assert "error_type=RuntimeError" in caplog.text
    assert "secret text" not in caplog.text
    outbox.scrub_terminal_deliveries.assert_awaited_once()


@pytest.mark.asyncio
async def test_reconciliation_operation_failures_mark_cleanup_incomplete(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=False,
        operation_failures=(
            ReconciliationOperationFailure(
                action="remove_run",
                identifier="project/job/run",
                error_type="OSError",
            ),
        ),
    )
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
            reconciliation_dry_run=False,
        )

    outbox.scrub_terminal_deliveries.assert_awaited_once()
    assert "completed with operation failures" in caplog.text


@pytest.mark.asyncio
async def test_artifact_findings_increment_bounded_metrics_and_warn(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True,
        missing_result_files=1,
        corrupt_result_files=1,
        unreadable_result_files=1,
        unsafe_metadata_paths=1,
        artifact_failures=tuple(
            ResultArtifactFailure(
                job_id="job",
                run_id=f"run-{kind.value}",
                kind=kind,
            )
            for kind in ResultArtifactFailureKind
        ),
    )
    before = {
        kind.value: CLEANUP_ARTIFACT_FINDINGS.labels(kind.value)._value.get()
        for kind in ResultArtifactFailureKind
    }

    await run_cleanup(result_store, retention_days=30, max_results_per_job=100)

    after = {
        kind.value: CLEANUP_ARTIFACT_FINDINGS.labels(kind.value)._value.get()
        for kind in ResultArtifactFailureKind
    }
    assert after == {kind: value + 1 for kind, value in before.items()}
    assert "retained-result integrity failures" in caplog.text
    assert "job" not in caplog.text


@pytest.mark.asyncio
async def test_clean_artifact_scan_does_not_increment_finding_metrics(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.return_value = 0
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(
        dry_run=True,
        valid_artifacts=3,
    )
    before = {
        kind.value: CLEANUP_ARTIFACT_FINDINGS.labels(kind.value)._value.get()
        for kind in ResultArtifactFailureKind
    }

    await run_cleanup(result_store, retention_days=30, max_results_per_job=100)

    assert {
        kind.value: CLEANUP_ARTIFACT_FINDINGS.labels(kind.value)._value.get()
        for kind in ResultArtifactFailureKind
    } == before
    assert "retained-result integrity failures" not in caplog.text


@pytest.mark.asyncio
async def test_expired_result_failure_does_not_skip_later_cleanup_phases(caplog):
    result_store = AsyncMock()
    result_store.delete_expired.side_effect = RuntimeError("secret text")
    result_store.prune_excess_per_job.return_value = 0
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)
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

    result_store.prune_excess_per_job.assert_awaited_once_with(100, limit=500)
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
    result_store.reconcile_artifacts.return_value = ResultReconciliationReport(dry_run=True)
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
