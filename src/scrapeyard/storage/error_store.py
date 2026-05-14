"""SQLite-backed implementation of the ErrorStore protocol."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import cast

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.models.job import ActionTaken, ErrorFilters, ErrorRecord, ErrorType
from scrapeyard.storage.database import get_db
from scrapeyard.storage.error_queries import build_query_errors_query

logger = logging.getLogger(__name__)


def _loads_selectors_matched(value: str | None) -> dict[str, int] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring malformed selectors_matched JSON in error row: %s", exc)
        return None
    if not isinstance(parsed, dict):
        logger.warning("Ignoring malformed selectors_matched JSON in error row: expected object")
        return None

    selectors: dict[str, int] = {}
    for selector, count in parsed.items():
        if not isinstance(count, int) or isinstance(count, bool):
            logger.warning(
                "Ignoring malformed selectors_matched JSON in error row: expected integer counts"
            )
            return None
        selectors[str(selector)] = count
    return selectors


def _row_to_error(row: Mapping[str, object]) -> ErrorRecord:
    selectors = cast(str | None, row["selectors_matched"])
    timestamp = parse_dt(cast(str | None, row["timestamp"]))
    if timestamp is None:
        raise ValueError("Error row is missing timestamp")
    return ErrorRecord(
        job_id=cast(str, row["job_id"]),
        run_id=cast(str, row["run_id"]),
        project=cast(str, row["project"]),
        target_url=cast(str, row["target_url"]),
        attempt=cast(int, row["attempt"]),
        timestamp=timestamp,
        error_type=ErrorType(cast(str, row["error_type"])),
        http_status=cast(int | None, row["http_status"]),
        fetcher_used=cast(str, row["fetcher_used"]),
        error_message=cast(str | None, row["error_message"]),
        selectors_matched=_loads_selectors_matched(selectors),
        action_taken=ActionTaken(cast(str, row["action_taken"])),
        resolved=bool(row["resolved"]),
    )


class SQLiteErrorStore:
    """SQLite implementation of :class:`~scrapeyard.storage.protocols.ErrorStore`."""

    async def log_error(self, error: ErrorRecord) -> None:
        await self.log_errors([error])

    async def log_errors(self, errors: list[ErrorRecord]) -> None:
        if not errors:
            return

        rows = [
            (
                error.job_id,
                error.run_id,
                error.project,
                error.target_url,
                error.attempt,
                fmt_dt(error.timestamp),
                error.error_type.value,
                error.http_status,
                error.fetcher_used,
                error.error_message,
                json.dumps(error.selectors_matched)
                if error.selectors_matched is not None
                else None,
                error.action_taken.value,
                int(error.resolved),
            )
            for error in errors
        ]
        async with get_db("errors.db") as db:
            await db.executemany(
                """INSERT INTO errors
                   (job_id, run_id, project, target_url, attempt,
                    timestamp, error_type, http_status, fetcher_used,
                    error_message, selectors_matched, action_taken,
                    resolved)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await db.commit()

    async def query_errors(
        self,
        filters: ErrorFilters,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ErrorRecord]:
        sql, params = build_query_errors_query(filters, limit, offset)
        async with get_db("errors.db") as db:
            cursor = await db.execute(sql, params)
            rows = cast(list[Mapping[str, object]], await cursor.fetchall())
        return [_row_to_error(r) for r in rows]

    async def count_errors_for_run(self, run_id: str) -> int:
        """Return the number of error records for a given run_id."""
        async with get_db("errors.db") as db:
            cursor = await db.execute(
                "SELECT COUNT(*) AS error_count FROM errors WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            return row["error_count"] if row else 0

    async def delete_errors_for_job(self, job_id: str) -> None:
        """Delete all error records for a job."""
        async with get_db("errors.db") as db:
            await db.execute("DELETE FROM errors WHERE job_id=?", (job_id,))
            await db.commit()
