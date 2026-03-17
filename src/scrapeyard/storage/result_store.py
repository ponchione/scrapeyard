"""Local filesystem + SQLite implementation of the ResultStore protocol."""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from scrapeyard.storage.database import get_db


@dataclass(frozen=True, slots=True)
class SaveResultMeta:
    """Metadata returned from a save_result call."""

    run_id: str
    file_path: str
    record_count: int | None


# Supported format → filename mapping.
_FORMAT_FILES: dict[str, list[str]] = {
    "json": ["results.json"],
    "markdown": ["results.md"],
    "html": ["raw.html"],
    "json+markdown": ["results.json", "results.md"],
}


def _generate_run_id() -> str:
    now = datetime.now(timezone.utc)
    short_uuid = uuid.uuid4().hex[:8]
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{short_uuid}"


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
        format: str,
        *,
        record_count: int | None = None,
        file_contents: dict[str, Any] | None = None,
    ) -> SaveResultMeta:
        if format not in _FORMAT_FILES:
            raise ValueError(f"Unsupported format: {format!r}")

        project, job_name = await self._job_lookup(job_id)
        run_id = _generate_run_id()
        run_dir = self._results_dir / project / job_name / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        filenames = _FORMAT_FILES[format]
        for filename in filenames:
            path = run_dir / filename
            value = data
            if file_contents and filename in file_contents:
                value = file_contents[filename]
            if filename.endswith(".json"):
                path.write_text(json.dumps(value, default=str, indent=2))
            else:
                path.write_text(str(value))

        async with get_db("results_meta.db") as db:
            await db.execute(
                """INSERT INTO results_meta
                   (job_id, project, run_id, status, record_count, file_path, format, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id,
                    project,
                    run_id,
                    "complete",
                    record_count,
                    str(run_dir),
                    format,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

        return SaveResultMeta(
            run_id=run_id,
            file_path=str(run_dir),
            record_count=record_count,
        )

    async def get_result(self, job_id: str, run_id: str | None = None) -> Any:
        if run_id is not None:
            sql = "SELECT run_id, file_path, format FROM results_meta WHERE job_id=? AND run_id=?"
            params: tuple = (job_id, run_id)
        else:
            sql = "SELECT run_id, file_path, format FROM results_meta WHERE job_id=? ORDER BY created_at DESC LIMIT 1"
            params = (job_id,)

        async with get_db("results_meta.db") as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()

        if row is None:
            raise KeyError(f"No results found for job {job_id!r}" + (f" run {run_id!r}" if run_id else ""))

        _, file_path, fmt = row
        run_dir = Path(file_path)
        filenames = _FORMAT_FILES[fmt]

        # Return the primary file content; prefer JSON if available.
        for filename in filenames:
            path = run_dir / filename
            if filename.endswith(".json"):
                return json.loads(path.read_text())
        # Fallback to first file as string.
        return (run_dir / filenames[0]).read_text()

    async def delete_results(self, job_id: str) -> None:
        """Delete all results for a job from disk and metadata DB."""
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                "SELECT file_path FROM results_meta WHERE job_id=?", (job_id,)
            )
            rows = await cursor.fetchall()
            for (file_path,) in rows:
                run_dir = Path(file_path)
                if run_dir.exists():
                    shutil.rmtree(run_dir)
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
            for row_id, file_path in rows:
                run_dir = Path(file_path)
                if run_dir.exists():
                    shutil.rmtree(run_dir)
            if rows:
                ids = [r[0] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                await db.execute(
                    f"DELETE FROM results_meta WHERE id IN ({placeholders})",
                    ids,
                )
                await db.commit()
        return len(rows)
