"""Runtime health and project-summary helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from scrapeyard.storage.database import SQLiteProbeError, probe_db
from scrapeyard.storage.protocols import JobStore

logger = logging.getLogger(__name__)

ProjectSummaryRows = list[tuple[str, str, int]]
JobStoreFactory = Callable[[], JobStore]


class ProjectSummaryEntry(TypedDict):
    """Stable JSON shape emitted for one project's health summary."""

    job_count: int
    status: str
    status_counts: dict[str, int]


ProjectSummary = dict[str, ProjectSummaryEntry]


class RedisHealthPool(Protocol):
    """Worker-pool view required by the Redis readiness adapter."""

    async def ping(self) -> None: ...


class BackgroundService(Protocol):
    """Explicit process-local health contract for background services."""

    @property
    def background_ok(self) -> bool: ...

    @property
    def background_detail(self) -> str | None: ...


class BackgroundTask(Protocol):
    """Asyncio task surface needed by the process-local task probe."""

    def cancelled(self) -> bool: ...

    def done(self) -> bool: ...

    def exception(self) -> BaseException | None: ...


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str | None = None


class SingleFlightSyncProbe:
    """Admit at most one underlying synchronous probe until it really exits."""

    def __init__(self, executor: Executor) -> None:
        self._executor = executor
        self._future: Future[ProbeResult] | None = None
        self._lock = threading.Lock()

    def submit(self, function: Callable[[], ProbeResult]) -> Future[ProbeResult]:
        with self._lock:
            if self._future is None or self._future.done():
                self._future = self._executor.submit(function)
            return self._future


async def probe_redis(pool: RedisHealthPool) -> ProbeResult:
    """Ping Redis through the worker pool's shared connection."""
    try:
        await pool.ping()
    except Exception as exc:  # pragma: no cover — exercised via live tests
        return ProbeResult(False, f"redis ping failed: {type(exc).__name__}")
    return ProbeResult(True)


async def probe_sqlite(db_name: str = "jobs.db") -> ProbeResult:
    try:
        await probe_db(db_name)
    except SQLiteProbeError as exc:
        return ProbeResult(False, str(exc))
    except Exception as exc:  # pragma: no cover
        return ProbeResult(False, f"{db_name} probe failed: {type(exc).__name__}")
    return ProbeResult(True)


def probe_result_storage(path: str) -> ProbeResult:
    """Exercise an atomic one-byte create/read/remove in the artifact directory."""

    root = Path(path)
    probe = root / f".scrapeyard-readiness-{secrets.token_hex(8)}"
    descriptor: int | None = None
    try:
        descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(descriptor, b"1")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if probe.read_bytes() != b"1":
            return ProbeResult(False, "result storage probe read mismatch")
        return ProbeResult(True)
    except OSError as exc:
        return ProbeResult(False, f"result storage probe failed: {type(exc).__name__}")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            probe.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Unable to remove result storage readiness probe")


def probe_background_service(
    name: str,
    service: BackgroundService | None,
) -> ProbeResult:
    """Inspect the explicit health contract of one process-local service."""

    if service is None:
        return ProbeResult(False, f"{name} service missing")
    try:
        ok = bool(service.background_ok)
        detail = service.background_detail
    except Exception as exc:
        return ProbeResult(False, f"{name} state probe failed: {type(exc).__name__}")
    return ProbeResult(ok, None if ok else str(detail or f"{name} stopped"))


def probe_asyncio_task(name: str, task: BackgroundTask | None) -> ProbeResult:
    """Report a missing, stopped, or failed asyncio background task."""

    if task is None:
        return ProbeResult(False, f"{name} task missing")
    if task.cancelled():
        return ProbeResult(False, f"{name} task stopped")
    if not task.done():
        return ProbeResult(True)
    exception = task.exception()
    if exception is None:
        return ProbeResult(False, f"{name} task stopped")
    return ProbeResult(False, f"{name} task failed: {type(exception).__name__}")


def probe_disk(path: str, min_free_mb: int) -> ProbeResult:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return ProbeResult(False, f"disk usage probe failed: {type(exc).__name__}")
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        return ProbeResult(False, f"only {free_mb}MB free (min {min_free_mb}MB)")
    return ProbeResult(True, f"{free_mb}MB free")


def build_project_summary(rows: ProjectSummaryRows) -> ProjectSummary:
    summary: ProjectSummary = {}
    for project, status, count in rows:
        project_entry = summary.setdefault(
            project,
            {
                "job_count": 0,
                "status": "healthy",
                "status_counts": {
                    "queued": 0,
                    "running": 0,
                    "complete": 0,
                    "partial": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "deleting": 0,
                },
            },
        )
        project_entry["job_count"] += count
        if status in project_entry["status_counts"]:
            project_entry["status_counts"][status] += count

    for project_entry in summary.values():
        counts = project_entry["status_counts"]
        if counts["failed"] > 0:
            project_entry["status"] = "failing"
        elif (
            counts["partial"] > 0
            or counts["running"] > 0
            or counts["deleting"] > 0
        ):
            project_entry["status"] = "degraded"
        else:
            project_entry["status"] = "healthy"

    return summary


async def load_project_summary(get_job_store: JobStoreFactory) -> ProjectSummary:
    """Load a project summary, allowing readiness to classify load failures."""

    rows = await get_job_store().summary_by_project()
    return build_project_summary(rows)


class HealthCache:
    """Encapsulates health-endpoint state: uptime tracking and project summary cache."""

    def __init__(
        self,
        get_job_store: JobStoreFactory,
        cache_ttl_seconds: float = 5.0,
    ) -> None:
        self._get_job_store = get_job_store
        self.start_time: float = 0.0
        self._projects_cache: ProjectSummary = {}
        self._projects_cache_refreshed_at: float = 0.0
        self._cache_ttl = cache_ttl_seconds
        self._projects_refresh_lock = asyncio.Lock()
        self._projects_refresh_task: asyncio.Task[ProjectSummary] | None = None

    def mark_started(self) -> None:
        self.start_time = time.monotonic()

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.start_time if self.start_time else 0.0

    @property
    def cached_project_summary(self) -> ProjectSummary:
        """Return the last successful summary without starting database work."""

        return self._projects_cache

    async def _refresh_project_summary(
        self,
        timeout: float | None,
    ) -> ProjectSummary:
        load = load_project_summary(self._get_job_store)
        summary = await load if timeout is None else await asyncio.wait_for(load, timeout)
        self._projects_cache = summary
        self._projects_cache_refreshed_at = time.monotonic()
        return summary

    def _refresh_finished(self, task: asyncio.Task[ProjectSummary]) -> None:
        if self._projects_refresh_task is task:
            self._projects_refresh_task = None
        if not task.cancelled():
            # Retrieve failures even when every HTTP caller timed out while the
            # shielded refresh continued in the background.
            task.exception()

    async def project_summary(self, *, timeout: float | None = None) -> ProjectSummary:
        now = time.monotonic()
        if now - self._projects_cache_refreshed_at < self._cache_ttl:
            return self._projects_cache

        async with self._projects_refresh_lock:
            now = time.monotonic()
            if now - self._projects_cache_refreshed_at < self._cache_ttl:
                return self._projects_cache
            refresh = self._projects_refresh_task
            if refresh is None or refresh.done():
                refresh = asyncio.create_task(
                    self._refresh_project_summary(timeout),
                    name="scrapeyard-health-project-summary",
                )
                self._projects_refresh_task = refresh
                refresh.add_done_callback(self._refresh_finished)

        return await asyncio.shield(refresh)
