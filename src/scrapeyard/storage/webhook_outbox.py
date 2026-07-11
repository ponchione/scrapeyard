"""Durable SQLite webhook outbox storage."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.common.time import utc_now
from scrapeyard.storage.database import get_db

logger = logging.getLogger(__name__)


class WebhookDeliveryStatus(str, Enum):
    """Persistent webhook delivery states."""

    pending = "pending"
    delivered = "delivered"
    failed = "failed"


class WebhookFailureReason(str, Enum):
    """Stable terminal dead-letter reason codes."""

    attempt_exhausted = "attempt_exhausted"
    age_exhausted = "age_exhausted"
    permanent_http_response = "permanent_http_response"
    unsafe_url = "unsafe_url"
    non_retryable_failure = "non_retryable_failure"


@dataclass(frozen=True, slots=True)
class WebhookDeliveryCreate:
    """Values needed to enqueue a new durable webhook delivery."""

    delivery_id: str
    job_id: str
    run_id: str | None
    event: str
    url: str
    headers: dict[str, str]
    timeout_seconds: float
    payload: dict[str, Any]
    next_attempt_at: datetime


@dataclass(frozen=True, slots=True)
class WebhookDelivery:
    """A stored webhook delivery or scrubbed terminal tombstone row."""

    delivery_id: str
    job_id: str
    run_id: str | None
    event: str
    url: str
    headers: dict[str, str]
    timeout_seconds: float
    payload: dict[str, Any]
    status: WebhookDeliveryStatus
    attempts: int
    next_attempt_at: datetime
    last_attempt_at: datetime | None
    delivered_at: datetime | None
    failed_at: datetime | None
    failure_reason: WebhookFailureReason | None
    last_error: str | None
    scrubbed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_scrubbed(self) -> bool:
        return self.scrubbed_at is not None


@dataclass(frozen=True, slots=True)
class WebhookOutboxSummary:
    """Bounded-cost operational summary of durable outbox state."""

    pending: int
    delivered: int
    failed: int
    oldest_pending_age_seconds: float | None
    pending_attempts_min: int | None
    pending_attempts_max: int | None
    pending_attempts_total: int


@dataclass(frozen=True, slots=True)
class WebhookRetentionSummary:
    """Counts from one bounded terminal secret-scrubbing pass."""

    delivered_scrubbed: int = 0
    failed_scrubbed: int = 0


WEBHOOK_DELIVERY_COLUMNS = (
    "delivery_id",
    "job_id",
    "run_id",
    "event",
    "url",
    "headers_json",
    "timeout_seconds",
    "payload_json",
    "status",
    "attempts",
    "next_attempt_at",
    "last_attempt_at",
    "delivered_at",
    "failed_at",
    "failure_reason",
    "last_error",
    "scrubbed_at",
    "created_at",
    "updated_at",
)


def _dumps_json(value: dict[str, Any] | dict[str, str]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


async def insert_webhook_delivery(
    db: aiosqlite.Connection,
    delivery: WebhookDeliveryCreate,
    *,
    created_at: datetime,
) -> bool:
    """Insert one logical delivery without committing the caller's transaction."""

    if delivery.payload.get("delivery_id") != delivery.delivery_id:
        raise ValueError("Webhook payload delivery_id must match the durable row ID")
    cursor = await db.execute(
        """INSERT OR IGNORE INTO webhook_deliveries
           (delivery_id, job_id, run_id, event, url, headers_json,
            timeout_seconds, payload_json, status, attempts, next_attempt_at,
            last_attempt_at, delivered_at, failed_at, failure_reason, last_error,
            scrubbed_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL,
                   NULL, NULL, NULL, ?, ?)""",
        (
            delivery.delivery_id,
            delivery.job_id,
            delivery.run_id,
            delivery.event,
            delivery.url,
            _dumps_json(delivery.headers),
            delivery.timeout_seconds,
            _dumps_json(delivery.payload),
            fmt_dt(delivery.next_attempt_at),
            fmt_dt(created_at),
            fmt_dt(created_at),
        ),
    )
    return cursor.rowcount == 1


def _loads_dict(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("Stored webhook JSON payload is not an object")
    return cast(dict[str, Any], parsed)


def _loads_headers(value: str) -> dict[str, str]:
    parsed = _loads_dict(value)
    return {str(key): str(item) for key, item in parsed.items()}


def _require_dt(value: str | None, column: str) -> datetime:
    parsed = parse_dt(value)
    if parsed is None:
        raise ValueError(f"Stored webhook delivery missing {column}")
    return parsed


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def row_to_webhook_delivery(row: Mapping[str, Any]) -> WebhookDelivery:
    """Decode a SQLite row into a webhook delivery model."""

    reason_value = row["failure_reason"]
    return WebhookDelivery(
        delivery_id=str(row["delivery_id"]),
        job_id=str(row["job_id"]),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        event=str(row["event"]),
        url=str(row["url"]),
        headers=_loads_headers(str(row["headers_json"])),
        timeout_seconds=float(row["timeout_seconds"]),
        payload=_loads_dict(str(row["payload_json"])),
        status=WebhookDeliveryStatus(str(row["status"])),
        attempts=int(row["attempts"]),
        next_attempt_at=_require_dt(
            cast(str | None, row["next_attempt_at"]),
            "next_attempt_at",
        ),
        last_attempt_at=parse_dt(cast(str | None, row["last_attempt_at"])),
        delivered_at=parse_dt(cast(str | None, row["delivered_at"])),
        failed_at=parse_dt(cast(str | None, row["failed_at"])),
        failure_reason=(
            None
            if reason_value is None
            else WebhookFailureReason(str(reason_value))
        ),
        last_error=None if row["last_error"] is None else str(row["last_error"]),
        scrubbed_at=parse_dt(cast(str | None, row["scrubbed_at"])),
        created_at=_require_dt(cast(str | None, row["created_at"]), "created_at"),
        updated_at=_require_dt(cast(str | None, row["updated_at"]), "updated_at"),
    )


def _decode_webhook_delivery(row: Mapping[str, Any]) -> WebhookDelivery | None:
    try:
        return row_to_webhook_delivery(row)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Skipping malformed webhook delivery delivery_id=%r error_type=%s",
            _row_value(row, "delivery_id"),
            type(exc).__name__,
        )
        return None


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _validate_limit(limit: int) -> None:
    if limit < 1:
        raise ValueError("Webhook outbox query limit must be positive")


class SQLiteWebhookOutboxStore:
    """SQLite-backed durable webhook outbox using jobs.db."""

    async def enqueue_delivery(
        self,
        delivery: WebhookDeliveryCreate,
        *,
        now: datetime | None = None,
    ) -> None:
        """Persist a delivery if it has not already been enqueued."""

        created_at = now or utc_now()
        async with get_db("jobs.db") as db:
            await insert_webhook_delivery(db, delivery, created_at=created_at)
            await db.commit()

    async def get_delivery(self, delivery_id: str) -> WebhookDelivery | None:
        """Return one delivery or terminal tombstone by ID."""

        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} "
                "FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
            row = await cursor.fetchone()
        return (
            None
            if row is None
            else _decode_webhook_delivery(cast(Mapping[str, Any], row))
        )

    async def list_pending(
        self,
        *,
        limit: int | None = None,
    ) -> list[WebhookDelivery]:
        """List pending deliveries in deterministic schedule order."""

        sql = (
            f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} FROM webhook_deliveries "
            "WHERE status = 'pending' "
            "ORDER BY next_attempt_at ASC, created_at ASC, delivery_id ASC"
        )
        params: tuple[object, ...] = ()
        if limit is not None:
            _validate_limit(limit)
            sql += " LIMIT ?"
            params = (limit,)
        return await self._fetch_deliveries(sql, params)

    async def list_due_pending(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> list[WebhookDelivery]:
        """Return only due pending rows in one bounded deterministic batch."""

        _validate_limit(limit)
        return await self._fetch_deliveries(
            f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} "
            "FROM webhook_deliveries "
            "WHERE status = 'pending' AND next_attempt_at <= ? "
            "ORDER BY next_attempt_at ASC, created_at ASC, delivery_id ASC "
            "LIMIT ?",
            (fmt_dt(now), limit),
        )

    async def list_exhausted_pending(
        self,
        *,
        attempts_gte: int,
        created_at_lte: datetime,
        limit: int,
    ) -> list[WebhookDelivery]:
        """Return bounded pending rows already exhausted by attempts or age."""

        _validate_limit(limit)
        return await self._fetch_deliveries(
            f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} "
            "FROM webhook_deliveries "
            "WHERE status = 'pending' AND (attempts >= ? OR created_at <= ?) "
            "ORDER BY created_at ASC, delivery_id ASC LIMIT ?",
            (attempts_gte, fmt_dt(created_at_lte), limit),
        )

    async def next_pending_due_at(self) -> datetime | None:
        """Return the earliest pending schedule time without loading its row."""

        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT MIN(next_attempt_at) FROM webhook_deliveries "
                "WHERE status = 'pending'"
            )
            row = await cursor.fetchone()
        return None if row is None else parse_dt(cast(str | None, row[0]))

    async def oldest_pending_created_at(self) -> datetime | None:
        """Return the oldest pending creation time for age-bound wakeups."""

        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                "SELECT MIN(created_at) FROM webhook_deliveries "
                "WHERE status = 'pending'"
            )
            row = await cursor.fetchone()
        return None if row is None else parse_dt(cast(str | None, row[0]))

    async def summarize(self, *, now: datetime | None = None) -> WebhookOutboxSummary:
        """Return status counts, oldest pending age, and pending attempt range."""

        observed_at = now or utc_now()
        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """SELECT
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'delivered' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END),
                       MIN(CASE WHEN status = 'pending' THEN created_at END),
                       MIN(CASE WHEN status = 'pending' THEN attempts END),
                       MAX(CASE WHEN status = 'pending' THEN attempts END),
                       SUM(CASE WHEN status = 'pending' THEN attempts ELSE 0 END)
                   FROM webhook_deliveries"""
            )
            row = await cursor.fetchone()
        assert row is not None
        oldest = parse_dt(cast(str | None, row[3]))
        oldest_age = (
            None
            if oldest is None
            else max(0.0, (_as_utc(observed_at) - _as_utc(oldest)).total_seconds())
        )
        return WebhookOutboxSummary(
            pending=int(row[0] or 0),
            delivered=int(row[1] or 0),
            failed=int(row[2] or 0),
            oldest_pending_age_seconds=oldest_age,
            pending_attempts_min=None if row[4] is None else int(row[4]),
            pending_attempts_max=None if row[5] is None else int(row[5]),
            pending_attempts_total=int(row[6] or 0),
        )

    async def begin_attempt(
        self,
        delivery_id: str,
        *,
        expected_attempts: int,
        attempted_at: datetime,
    ) -> WebhookDelivery | None:
        """Durably reserve one attempt with a pending-state compare-and-set."""

        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                """UPDATE webhook_deliveries
                   SET attempts = attempts + 1,
                       last_attempt_at = ?,
                       updated_at = ?
                   WHERE delivery_id = ?
                     AND status = 'pending'
                     AND attempts = ?""",
                (
                    fmt_dt(attempted_at),
                    fmt_dt(attempted_at),
                    delivery_id,
                    expected_attempts,
                ),
            )
            if cursor.rowcount != 1:
                await db.rollback()
                return None
            cursor = await db.execute(
                f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} "
                "FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
            row = await cursor.fetchone()
            await db.commit()
        return (
            None
            if row is None
            else _decode_webhook_delivery(cast(Mapping[str, Any], row))
        )

    async def mark_delivered(
        self,
        delivery_id: str,
        *,
        delivered_at: datetime,
        expected_attempts: int | None = None,
        attempts: int = 1,
    ) -> bool:
        """Transition a pending row to delivered; terminal rows never reset."""

        assignments = [
            "status = 'delivered'",
            "delivered_at = ?",
            "failed_at = NULL",
            "failure_reason = NULL",
            "last_error = NULL",
            "updated_at = ?",
        ]
        params: list[object] = [fmt_dt(delivered_at), fmt_dt(delivered_at)]
        if expected_attempts is None:
            assignments.extend(["attempts = attempts + ?", "last_attempt_at = ?"])
            params.extend([attempts, fmt_dt(delivered_at)])
        return await self._terminal_update(
            delivery_id,
            assignments=assignments,
            params=params,
            expected_attempts=expected_attempts,
        )

    async def mark_retryable_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        next_attempt_at: datetime,
        last_error: str,
        expected_attempts: int | None = None,
        attempts: int = 1,
    ) -> bool:
        """Keep a pending row scheduled after a transient failure."""

        assignments = [
            "next_attempt_at = ?",
            "last_error = ?",
            "updated_at = ?",
        ]
        params: list[object] = [
            fmt_dt(next_attempt_at),
            last_error,
            fmt_dt(attempted_at),
        ]
        if expected_attempts is None:
            assignments.extend(["attempts = attempts + ?", "last_attempt_at = ?"])
            params.extend([attempts, fmt_dt(attempted_at)])
        return await self._pending_update(
            delivery_id,
            assignments=assignments,
            params=params,
            expected_attempts=expected_attempts,
        )

    async def mark_permanent_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        last_error: str,
        reason: WebhookFailureReason = WebhookFailureReason.non_retryable_failure,
        expected_attempts: int | None = None,
        attempts: int = 1,
    ) -> bool:
        """Compatibility wrapper for a terminal failure after an attempt."""

        return await self.mark_failed(
            delivery_id,
            failed_at=attempted_at,
            reason=reason,
            last_error=last_error,
            expected_attempts=expected_attempts,
            attempts=attempts,
        )

    async def mark_failed(
        self,
        delivery_id: str,
        *,
        failed_at: datetime,
        reason: WebhookFailureReason,
        last_error: str | None,
        expected_attempts: int | None = None,
        attempts: int = 0,
    ) -> bool:
        """Dead-letter a pending row with a stable machine-readable reason."""

        assignments = [
            "status = 'failed'",
            "failed_at = ?",
            "failure_reason = ?",
            "last_error = ?",
            "updated_at = ?",
        ]
        params: list[object] = [
            fmt_dt(failed_at),
            reason.value,
            last_error,
            fmt_dt(failed_at),
        ]
        if expected_attempts is None and attempts:
            assignments.extend(["attempts = attempts + ?", "last_attempt_at = ?"])
            params.extend([attempts, fmt_dt(failed_at)])
        return await self._terminal_update(
            delivery_id,
            assignments=assignments,
            params=params,
            expected_attempts=expected_attempts,
        )

    async def scrub_terminal_deliveries(
        self,
        *,
        delivered_before: datetime,
        failed_before: datetime,
        scrubbed_at: datetime,
        limit: int,
    ) -> WebhookRetentionSummary:
        """Scrub bounded terminal rows while retaining logical tombstones."""

        _validate_limit(limit)
        async with get_db("jobs.db") as db:
            delivered_ids = await self._terminal_ids_for_scrub(
                db,
                status=WebhookDeliveryStatus.delivered,
                terminal_column="delivered_at",
                cutoff=delivered_before,
                limit=limit,
            )
            failed_ids = await self._terminal_ids_for_scrub(
                db,
                status=WebhookDeliveryStatus.failed,
                terminal_column="failed_at",
                cutoff=failed_before,
                limit=limit,
            )
            for delivery_ids in (delivered_ids, failed_ids):
                if not delivery_ids:
                    continue
                placeholders = ",".join("?" for _ in delivery_ids)
                await db.execute(
                    f"""UPDATE webhook_deliveries
                        SET url = '',
                            headers_json = '{{}}',
                            timeout_seconds = 0,
                            payload_json = '{{}}',
                            last_error = NULL,
                            scrubbed_at = ?,
                            updated_at = ?
                        WHERE delivery_id IN ({placeholders})
                          AND status IN ('delivered', 'failed')
                          AND scrubbed_at IS NULL""",
                    (fmt_dt(scrubbed_at), fmt_dt(scrubbed_at), *delivery_ids),
                )
            await db.commit()
        return WebhookRetentionSummary(
            delivered_scrubbed=len(delivered_ids),
            failed_scrubbed=len(failed_ids),
        )

    async def _fetch_deliveries(
        self,
        sql: str,
        params: Sequence[object] = (),
    ) -> list[WebhookDelivery]:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            rows = cast(list[Mapping[str, Any]], await cursor.fetchall())
        return [
            delivery
            for row in rows
            if (delivery := _decode_webhook_delivery(row)) is not None
        ]

    async def _pending_update(
        self,
        delivery_id: str,
        *,
        assignments: list[str],
        params: list[object],
        expected_attempts: int | None,
    ) -> bool:
        where = "delivery_id = ? AND status = 'pending'"
        params.append(delivery_id)
        if expected_attempts is not None:
            where += " AND attempts = ?"
            params.append(expected_attempts)
        return await self._execute_update(
            f"UPDATE webhook_deliveries SET {', '.join(assignments)} WHERE {where}",
            params,
        )

    async def _terminal_update(
        self,
        delivery_id: str,
        *,
        assignments: list[str],
        params: list[object],
        expected_attempts: int | None,
    ) -> bool:
        return await self._pending_update(
            delivery_id,
            assignments=assignments,
            params=params,
            expected_attempts=expected_attempts,
        )

    async def _execute_update(
        self,
        sql: str,
        params: Sequence[object],
    ) -> bool:
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            await db.commit()
        return cursor.rowcount == 1

    @staticmethod
    async def _terminal_ids_for_scrub(
        db: aiosqlite.Connection,
        *,
        status: WebhookDeliveryStatus,
        terminal_column: str,
        cutoff: datetime,
        limit: int,
    ) -> list[str]:
        if terminal_column not in {"delivered_at", "failed_at"}:
            raise ValueError("Invalid webhook terminal timestamp column")
        cursor = await db.execute(
            f"""SELECT delivery_id
                FROM webhook_deliveries
                WHERE status = ?
                  AND scrubbed_at IS NULL
                  AND {terminal_column} IS NOT NULL
                  AND {terminal_column} <= ?
                ORDER BY {terminal_column} ASC, delivery_id ASC
                LIMIT ?""",
            (status.value, fmt_dt(cutoff), limit),
        )
        return [str(row[0]) for row in await cursor.fetchall()]
