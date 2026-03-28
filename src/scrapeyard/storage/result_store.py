"""Local filesystem + SQLite implementation of the ResultStore protocol."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from scrapeyard.common.ids import generate_run_id
from scrapeyard.storage.database import get_db
from scrapeyard.storage.filesystem import (
    prepare_directory,
    read_json_file,
    remove_directories,
    write_json_file,
)


@dataclass(frozen=True, slots=True)
class ResultPayload:
    """Wrapper returned by get_result with run context."""

    run_id: str
    data: Any


@dataclass(frozen=True, slots=True)
class SaveResultMeta:
    """Metadata returned from a save_result call."""

    run_id: str
    file_path: str
    record_count: int | None


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
        run_dir = self._results_dir / project / job_name / run_id
        await asyncio.to_thread(prepare_directory, run_dir)

        path = run_dir / "results.json"
        await asyncio.to_thread(write_json_file, path, data)

        async with get_db("results_meta.db") as db:
            await db.execute(
                "DELETE FROM results_meta WHERE job_id=? AND run_id=?",
                (job_id, run_id),
            )
            await db.execute(
                """INSERT INTO results_meta
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
                    datetime.now(timezone.utc).isoformat(),
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
        if run_id is not None:
            sql = (
                "SELECT run_id, file_path FROM results_meta"
                " WHERE job_id=? AND run_id=?"
            )
            params: tuple = (job_id, run_id)
        else:
            sql = (
                "SELECT run_id, file_path FROM results_meta"
                " WHERE job_id=? ORDER BY created_at DESC LIMIT 1"
            )
            params = (job_id,)

        async with get_db("results_meta.db") as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()

        if row is None:
            raise KeyError(
                f"No results found for job {job_id!r}"
                + (f" run {run_id!r}" if run_id else "")
            )

        result_run_id, file_path = row
        path = Path(file_path) / "results.json"
        data = await asyncio.to_thread(read_json_file, path)
        return ResultPayload(run_id=result_run_id, data=data)

    async def delete_results(self, job_id: str) -> None:
        """Delete all results for a job from disk and metadata DB."""
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                "SELECT file_path FROM results_meta WHERE job_id=?", (job_id,)
            )
            rows = await cursor.fetchall()
            if rows:
                await asyncio.to_thread(
                    remove_directories,
                    [file_path for (file_path,) in rows],
                )
            await db.execute("DELETE FROM results_meta WHERE job_id=?", (job_id,))
            await db.commit()

    async def delete_expired(self, retention_days: int) -> int:
        """Delete results older than *retention_days*. Returns count deleted."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                "SELECT id, file_path FROM results_meta WHERE created_at < ?",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            if rows:
                await asyncio.to_thread(
                    remove_directories,
                    [file_path for _, file_path in rows],
                )
                ids = [r[0] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"DELETE FROM results_meta WHERE id IN ({placeholders})",
                    ids,
                )
                await db.commit()
        return len(rows)
