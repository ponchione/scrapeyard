"""Storage protocol definitions for cloud-ready abstraction (spec section 9.1)."""

from __future__ import annotations

from typing import Any, Protocol

from scrapeyard.models.job import ErrorFilters, ErrorRecord, Job


class JobStore(Protocol):
    """Async interface for job persistence."""

    async def save_job(self, job: Job) -> str: ...

    async def get_job(self, job_id: str) -> Job: ...

    async def list_jobs(self, project: str) -> list[Job]: ...

    async def delete_job(self, job_id: str) -> None: ...


class ResultStore(Protocol):
    """Async interface for scrape result persistence."""

    async def save_result(self, job_id: str, data: Any, format: str) -> str: ...

    async def get_result(self, job_id: str, run_id: str | None = None) -> Any: ...


class ErrorStore(Protocol):
    """Async interface for structured error record persistence."""

    async def log_error(self, error: ErrorRecord) -> None: ...

    async def query_errors(self, filters: ErrorFilters) -> list[ErrorRecord]: ...
