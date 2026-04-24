"""Runtime health and project-summary helpers."""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scrapeyard.storage.database import get_db

logger = logging.getLogger(__name__)

ProjectSummaryRows = list[tuple[str, str, int]]
JobStoreFactory = Callable[[], Any]


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str | None = None


async def probe_redis(pool: Any) -> ProbeResult:
    """Ping Redis through the worker pool's shared connection."""
    redis = getattr(pool, "redis", None)
    if redis is None:
        return ProbeResult(False, "redis pool not connected")
    try:
        await redis.ping()
    except Exception as exc:  # pragma: no cover — exercised via live tests
        return ProbeResult(False, f"redis ping failed: {exc}")
    return ProbeResult(True)


async def probe_sqlite(db_name: str = "jobs.db") -> ProbeResult:
    try:
        async with get_db(db_name) as db:
            await db.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover
        return ProbeResult(False, f"sqlite probe failed: {exc}")
    return ProbeResult(True)


def probe_disk(path: str, min_free_mb: int) -> ProbeResult:
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return ProbeResult(False, f"disk_usage({path!r}) failed: {exc}")
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        return ProbeResult(False, f"only {free_mb}MB free on {path!r} (min {min_free_mb}MB)")
    return ProbeResult(True, f"{free_mb}MB free")


def build_project_summary(rows: ProjectSummaryRows) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
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
        elif counts["partial"] > 0 or counts["running"] > 0:
            project_entry["status"] = "degraded"
        else:
            project_entry["status"] = "healthy"

    return summary


async def load_project_summary(get_job_store: JobStoreFactory) -> dict[str, dict[str, Any]]:
    rows: ProjectSummaryRows = []
    try:
        rows = await get_job_store().summary_by_project()
    except RuntimeError:
        rows = []
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
        self._projects_cache: dict[str, dict[str, Any]] = {}
        self._projects_cache_refreshed_at: float = 0.0
        self._cache_ttl = cache_ttl_seconds

    def mark_started(self) -> None:
        self.start_time = time.monotonic()

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.start_time if self.start_time else 0.0

    async def project_summary(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        if now - self._projects_cache_refreshed_at < self._cache_ttl:
            return self._projects_cache

        summary = await load_project_summary(self._get_job_store)
        self._projects_cache = summary
        self._projects_cache_refreshed_at = now
        return summary
