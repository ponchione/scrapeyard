"""SQLite-backed implementation of the ErrorStore protocol."""

from __future__ import annotations

import json

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.models.job import ActionTaken, ErrorFilters, ErrorRecord, ErrorType
from scrapeyard.storage.database import get_db


def _row_to_error(row: tuple) -> ErrorRecord:
    # Columns: id, job_id, run_id, project, target_url, attempt, timestamp,
    #          error_type, http_status, fetcher_used, error_message,
    #          selectors_matched, action_taken, resolved
    selectors = row[11]
    return ErrorRecord(
        job_id=row[1],
        run_id=row[2],
        project=row[3],
        target_url=row[4],
        attempt=row[5],
        timestamp=parse_dt(row[6]),
        error_type=ErrorType(row[7]),
        http_status=row[8],
        fetcher_used=row[9],
        error_message=row[10],
        selectors_matched=(
            json.loads(selectors) if selectors is not None else None
        ),
        action_taken=ActionTaken(row[12]),
        resolved=bool(row[13]),
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
        clauses: list[str] = []
        params: list[object] = []

        if filters.project is not None:
            clauses.append("project = ?")
            params.append(filters.project)
        if filters.job_id is not None:
            clauses.append("job_id = ?")
            params.append(filters.job_id)
        if filters.since is not None:
            clauses.append("timestamp >= ?")
            params.append(fmt_dt(filters.since))
        if filters.error_type is not None:
            clauses.append("error_type = ?")
            params.append(filters.error_type.value)

        sql = (
            "SELECT id, job_id, run_id, project, target_url, attempt, "
            "timestamp, error_type, http_status, fetcher_used, "
            "error_message, selectors_matched, action_taken, resolved "
            "FROM errors"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset > 0:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)

        async with get_db("errors.db") as db:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [_row_to_error(r) for r in rows]
