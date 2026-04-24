"""Storage protocol definitions for cloud-ready abstraction (spec section 9.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from scrapeyard.models.job import ErrorFilters, ErrorRecord, Job, JobRun
from scrapeyard.storage.types import ResultPayload, SaveResultMeta
from scrapeyard.storage.webhook_outbox import WebhookDelivery, WebhookDeliveryCreate


class JobStore(Protocol):
    """Async interface for job persistence."""

    async def save_job(self, job: Job) -> str: ...

    async def update_job(self, job: Job) -> None: ...

    async def update_job_status(self, job: Job) -> None: ...

    async def update_job_schedule_state(self, job: Job) -> None: ...

    async def get_job(self, job_id: str) -> Job: ...

    async def list_jobs(self, project: str | None = None) -> list[Job]: ...

    async def delete_job(self, job_id: str) -> None: ...

    async def get_job_runs(
        self, job_id: str, limit: int = 10,
    ) -> list[JobRun]: ...

    async def get_job_run_stats(
        self, job_id: str,
    ) -> tuple[int, datetime | None]: ...

    async def list_jobs_with_stats(
        self,
        project: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[Job, int, datetime | None]]: ...

    async def summary_by_project(self) -> list[tuple[str, str, int]]: ...

    async def create_run(
        self,
        run_id: str,
        job_id: str,
        trigger: str,
        config_hash: str,
        started_at: datetime,
    ) -> None: ...

    async def finalize_run(
        self,
        run_id: str,
        status: str,
        record_count: int,
        error_count: int,
    ) -> None: ...

    async def fail_run(self, run_id: str) -> None:
        """Mark a running run as failed (crash recovery)."""
        ...

    async def recover_stale_running_jobs(
        self,
        cutoff: datetime,
        recovered_at: datetime,
    ) -> int:
        """Mark stale running runs/jobs as failed and return recovered job count."""
        ...

    async def list_scheduled_jobs(
        self,
    ) -> list[tuple[str, str, bool]]:
        """Return (job_id, schedule_cron, schedule_enabled) for all scheduled jobs."""
        ...


class ResultStore(Protocol):
    """Async interface for scrape result persistence."""

    async def save_result(
        self,
        job_id: str,
        data: Any,
        *,
        run_id: str | None = None,
        status: str = "complete",
        record_count: int | None = None,
    ) -> SaveResultMeta: ...

    async def get_result(
        self, job_id: str, run_id: str | None = None,
    ) -> ResultPayload: ...

    async def delete_results(self, job_id: str) -> None: ...

    async def delete_expired(self, retention_days: int) -> int: ...

    async def prune_excess_per_job(self, max_results_per_job: int) -> int: ...


class ErrorStore(Protocol):
    """Async interface for structured error record persistence."""

    async def log_error(self, error: ErrorRecord) -> None: ...

    async def log_errors(self, errors: list[ErrorRecord]) -> None: ...

    async def query_errors(
        self,
        filters: ErrorFilters,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ErrorRecord]: ...

    async def count_errors_for_run(self, run_id: str) -> int: ...

    async def delete_errors_for_job(self, job_id: str) -> None: ...


class WebhookOutboxStore(Protocol):
    """Async interface for durable webhook delivery persistence."""

    async def enqueue_delivery(
        self,
        delivery: WebhookDeliveryCreate,
        *,
        now: datetime | None = None,
    ) -> None: ...

    async def get_delivery(self, delivery_id: str) -> WebhookDelivery | None: ...

    async def list_due_pending(
        self,
        now: datetime,
        *,
        limit: int | None = None,
    ) -> list[WebhookDelivery]: ...

    async def list_pending(self, *, limit: int | None = None) -> list[WebhookDelivery]: ...

    async def mark_delivered(
        self,
        delivery_id: str,
        *,
        delivered_at: datetime,
        attempts: int = 1,
    ) -> None: ...

    async def mark_retryable_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        next_attempt_at: datetime,
        last_error: str,
        attempts: int = 1,
    ) -> None: ...

    async def mark_permanent_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        last_error: str,
        attempts: int = 1,
    ) -> None: ...
