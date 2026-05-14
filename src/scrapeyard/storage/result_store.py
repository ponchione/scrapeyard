"""Local filesystem + SQLite implementation of the ResultStore protocol."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.paths import safe_join
from scrapeyard.common.time import utc_now
from scrapeyard.storage.database import get_db
from scrapeyard.storage.filesystem import (
    prepare_directory,
    read_json_file,
    remove_directories,
    write_json_file,
)
from scrapeyard.storage.result_queries import (
    EXCESS_RESULTS_PER_JOB_QUERY,
    EXPIRED_RESULTS_QUERY,
    JOB_RESULTS_DELETE_QUERY,
    build_result_lookup_query,
)
from scrapeyard.storage.types import ResultPayload, SaveResultMeta

logger = logging.getLogger(__name__)


class LocalResultStore:
    """Stores scrape results on the local filesystem with metadata in SQLite.

    Parameters
    ----------
    results_dir:
        Root directory for result files.
    job_lookup:
        Async callable that takes a ``job_id`` and returns ``(project, job_name)``.
    """

    def __init__(
        self,
        results_dir: str,
        job_lookup: Callable[[str], Awaitable[tuple[str, str]]],
    ) -> None:
        self._results_dir = Path(results_dir)
        self._job_lookup = job_lookup

    def _checked_result_dir(self, file_path: str) -> Path:
        path = Path(file_path)
        root = self._results_dir.resolve(strict=False)
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Unsafe result path outside results_dir: {file_path!r}") from exc
        return path

    def _checked_result_dirs(self, rows: Sequence[Mapping[str, Any]]) -> list[Path]:
        paths: list[Path] = []
        for row in rows:
            file_path = str(row["file_path"])
            try:
                paths.append(self._checked_result_dir(file_path))
            except ValueError:
                logger.warning("Skipping unsafe result directory during cleanup: %r", file_path)
        return paths

    async def _delete_by_ids(
        self,
        db: Any,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        if not rows:
            return 0

        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        await db.execute(
            f"DELETE FROM results_meta WHERE id IN ({placeholders})",
            ids,
        )
        await db.commit()
        # Delete files after metadata so a crash leaves orphaned files
        # (recoverable) rather than orphaned metadata rows pointing to
        # missing files.
        await asyncio.to_thread(remove_directories, self._checked_result_dirs(rows))
        return len(rows)

    async def save_result(
        self,
        job_id: str,
        data: Any,
        *,
        run_id: str | None = None,
        status: str = "complete",
        record_count: int | None = None,
    ) -> SaveResultMeta:
        project, job_name = await self._job_lookup(job_id)
        run_id = run_id or generate_run_id()
        run_dir = safe_join(self._results_dir, project, job_name, run_id)
        await asyncio.to_thread(prepare_directory, run_dir)

        path = run_dir / "results.json"
        await asyncio.to_thread(write_json_file, path, data)

        async with get_db("results_meta.db") as db:
            # Single atomic statement — the UNIQUE index on (job_id, run_id)
            # lets INSERT OR REPLACE handle the upsert without a separate DELETE.
            await db.execute(
                """INSERT OR REPLACE INTO results_meta
                   (job_id, project, run_id, status, record_count,
                    file_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    project,
                    run_id,
                    status,
                    record_count,
                    str(run_dir),
                    utc_now().isoformat(),
                ),
            )
            await db.commit()

        return SaveResultMeta(
            run_id=run_id,
            file_path=str(run_dir),
            record_count=record_count,
        )

    async def get_result(
        self, job_id: str, run_id: str | None = None,
    ) -> ResultPayload:
        sql, params = build_result_lookup_query(job_id, run_id)

        async with get_db("results_meta.db") as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()

        if row is None:
            raise KeyError(
                f"No results found for job {job_id!r}"
                + (f" run {run_id!r}" if run_id else "")
            )

        result_run_id = row["run_id"]
        file_path = row["file_path"]

        path = self._checked_result_dir(str(file_path)) / "results.json"
        data = await asyncio.to_thread(read_json_file, path)
        return ResultPayload(run_id=result_run_id, data=data)

    async def delete_results(self, job_id: str) -> None:
        """Delete all results for a job from disk and metadata DB."""
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(JOB_RESULTS_DELETE_QUERY, (job_id,))
            rows = list(await cursor.fetchall())
            await self._delete_by_ids(db, rows)

    async def delete_expired(self, retention_days: int) -> int:
        """Delete results older than *retention_days*. Returns count deleted."""
        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat()
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(EXPIRED_RESULTS_QUERY, (cutoff,))
            rows = list(await cursor.fetchall())
            return await self._delete_by_ids(db, rows)

    async def prune_excess_per_job(self, max_results_per_job: int) -> int:
        """Delete result runs exceeding the per-job retention limit."""
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                EXCESS_RESULTS_PER_JOB_QUERY,
                (max_results_per_job,),
            )
            rows = list(await cursor.fetchall())
            return await self._delete_by_ids(db, rows)
