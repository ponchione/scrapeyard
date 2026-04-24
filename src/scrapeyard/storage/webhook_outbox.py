"""Durable SQLite webhook outbox storage."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, cast

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.common.time import utc_now
from scrapeyard.storage.database import get_db


class WebhookDeliveryStatus(str, Enum):
    """Persistent webhook delivery states."""

    pending = "pending"
    delivered = "delivered"
    failed = "failed"


@dataclass(frozen=True)
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


@dataclass(frozen=True)
class WebhookDelivery:
    """A stored webhook delivery row."""

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
    last_error: str | None
    created_at: datetime
    updated_at: datetime


WEBHOOK_DELIVERY_COLUMNS = """
    delivery_id, job_id, run_id, event, url, headers_json,
    timeout_seconds, payload_json, status, attempts, next_attempt_at,
    last_attempt_at, delivered_at, last_error, created_at, updated_at
"""


def _dumps_json(value: dict[str, Any] | dict[str, str]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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


def row_to_webhook_delivery(row: Sequence[Any]) -> WebhookDelivery:
    """Decode a SQLite row into a webhook delivery model."""

    return WebhookDelivery(
        delivery_id=str(row[0]),
        job_id=str(row[1]),
        run_id=None if row[2] is None else str(row[2]),
        event=str(row[3]),
        url=str(row[4]),
        headers=_loads_headers(str(row[5])),
        timeout_seconds=float(row[6]),
        payload=_loads_dict(str(row[7])),
        status=WebhookDeliveryStatus(str(row[8])),
        attempts=int(row[9]),
        next_attempt_at=_require_dt(cast(str | None, row[10]), "next_attempt_at"),
        last_attempt_at=parse_dt(cast(str | None, row[11])),
        delivered_at=parse_dt(cast(str | None, row[12])),
        last_error=None if row[13] is None else str(row[13]),
        created_at=_require_dt(cast(str | None, row[14]), "created_at"),
        updated_at=_require_dt(cast(str | None, row[15]), "updated_at"),
    )


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
            await db.execute(
                """INSERT OR IGNORE INTO webhook_deliveries
                   (delivery_id, job_id, run_id, event, url, headers_json,
                    timeout_seconds, payload_json, status, attempts, next_attempt_at,
                    last_attempt_at, delivered_at, last_error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, ?, ?)""",
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
            await db.commit()

    async def get_delivery(self, delivery_id: str) -> WebhookDelivery | None:
        """Return one delivery by ID, or None if it does not exist."""

        async with get_db("jobs.db") as db:
            cursor = await db.execute(
                f"SELECT {WEBHOOK_DELIVERY_COLUMNS} FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else row_to_webhook_delivery(row)

    async def list_due_pending(
        self,
        now: datetime,
        *,
        limit: int | None = None,
    ) -> list[WebhookDelivery]:
        """List pending deliveries due at or before *now*."""

        sql = (
            f"SELECT {WEBHOOK_DELIVERY_COLUMNS} FROM webhook_deliveries "
            "WHERE status = 'pending' AND next_attempt_at <= ? "
            "ORDER BY next_attempt_at ASC, created_at ASC"
        )
        params: tuple[object, ...] = (fmt_dt(now),)
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [row_to_webhook_delivery(row) for row in rows]

    async def list_pending(self, *, limit: int | None = None) -> list[WebhookDelivery]:
        """List all pending deliveries ordered by next attempt time."""

        sql = (
            f"SELECT {WEBHOOK_DELIVERY_COLUMNS} FROM webhook_deliveries "
            "WHERE status = 'pending' "
            "ORDER BY next_attempt_at ASC, created_at ASC"
        )
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        async with get_db("jobs.db") as db:
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [row_to_webhook_delivery(row) for row in rows]

    async def mark_delivered(
        self,
        delivery_id: str,
        *,
        delivered_at: datetime,
        attempts: int = 1,
    ) -> None:
        """Mark a delivery successful after one or more attempts."""

        await self._execute_status_update(
            """UPDATE webhook_deliveries
               SET status = 'delivered',
                   attempts = attempts + ?,
                   last_attempt_at = ?,
                   delivered_at = ?,
                   last_error = NULL,
                   updated_at = ?
               WHERE delivery_id = ?""",
            (
                attempts,
                fmt_dt(delivered_at),
                fmt_dt(delivered_at),
                fmt_dt(delivered_at),
                delivery_id,
            ),
        )

    async def mark_retryable_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        next_attempt_at: datetime,
        last_error: str,
        attempts: int = 1,
    ) -> None:
        """Record a transient failure and keep the delivery pending."""

        await self._execute_status_update(
            """UPDATE webhook_deliveries
               SET status = 'pending',
                   attempts = attempts + ?,
                   last_attempt_at = ?,
                   next_attempt_at = ?,
                   last_error = ?,
                   updated_at = ?
               WHERE delivery_id = ?""",
            (
                attempts,
                fmt_dt(attempted_at),
                fmt_dt(next_attempt_at),
                last_error,
                fmt_dt(attempted_at),
                delivery_id,
            ),
        )

    async def mark_permanent_failure(
        self,
        delivery_id: str,
        *,
        attempted_at: datetime,
        last_error: str,
        attempts: int = 1,
    ) -> None:
        """Mark a delivery permanently failed and leave it inspectable."""

        await self._execute_status_update(
            """UPDATE webhook_deliveries
               SET status = 'failed',
                   attempts = attempts + ?,
                   last_attempt_at = ?,
                   last_error = ?,
                   updated_at = ?
               WHERE delivery_id = ?""",
            (
                attempts,
                fmt_dt(attempted_at),
                last_error,
                fmt_dt(attempted_at),
                delivery_id,
            ),
        )

    async def _execute_status_update(self, sql: str, params: Sequence[object]) -> None:
        async with get_db("jobs.db") as db:
            await db.execute(sql, params)
            await db.commit()
