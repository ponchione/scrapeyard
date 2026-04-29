"""Durable SQLite webhook outbox storage."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, cast

from scrapeyard.common.dt import fmt_dt, parse_dt
from scrapeyard.common.time import utc_now
from scrapeyard.storage.database import get_db

_UNSET = object()


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
    "last_error",
    "created_at",
    "updated_at",
)


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


def row_to_webhook_delivery(row: Mapping[str, Any]) -> WebhookDelivery:
    """Decode a SQLite row into a webhook delivery model."""

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
        next_attempt_at=_require_dt(cast(str | None, row["next_attempt_at"]), "next_attempt_at"),
        last_attempt_at=parse_dt(cast(str | None, row["last_attempt_at"])),
        delivered_at=parse_dt(cast(str | None, row["delivered_at"])),
        last_error=None if row["last_error"] is None else str(row["last_error"]),
        created_at=_require_dt(cast(str | None, row["created_at"]), "created_at"),
        updated_at=_require_dt(cast(str | None, row["updated_at"]), "updated_at"),
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
                f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} "
                "FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else row_to_webhook_delivery(row)

    async def list_pending(self, *, limit: int | None = None) -> list[WebhookDelivery]:
        """List all pending deliveries ordered by next attempt time."""

        sql = (
            f"SELECT {', '.join(WEBHOOK_DELIVERY_COLUMNS)} FROM webhook_deliveries "
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

        await self._update_delivery_status(
            delivery_id,
            status=WebhookDeliveryStatus.delivered,
            attempted_at=delivered_at,
            attempts=attempts,
            delivered_at=delivered_at,
            last_error=None,
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

        await self._update_delivery_status(
            delivery_id,
            status=WebhookDeliveryStatus.pending,
            attempted_at=attempted_at,
            attempts=attempts,
            next_attempt_at=next_attempt_at,
            last_error=last_error,
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

        await self._update_delivery_status(
            delivery_id,
            status=WebhookDeliveryStatus.failed,
            attempted_at=attempted_at,
            attempts=attempts,
            last_error=last_error,
        )

    async def _update_delivery_status(
        self,
        delivery_id: str,
        *,
        status: WebhookDeliveryStatus,
        attempted_at: datetime,
        attempts: int,
        next_attempt_at: datetime | object = _UNSET,
        delivered_at: datetime | object = _UNSET,
        last_error: str | None | object = _UNSET,
    ) -> None:
        assignments = [
            "status = ?",
            "attempts = attempts + ?",
            "last_attempt_at = ?",
            "updated_at = ?",
        ]
        params: list[object] = [
            status.value,
            attempts,
            fmt_dt(attempted_at),
            fmt_dt(attempted_at),
        ]
        if next_attempt_at is not _UNSET:
            assignments.append("next_attempt_at = ?")
            params.append(fmt_dt(cast(datetime, next_attempt_at)))
        if delivered_at is not _UNSET:
            assignments.append("delivered_at = ?")
            params.append(fmt_dt(cast(datetime, delivered_at)))
        if last_error is not _UNSET:
            assignments.append("last_error = ?")
            params.append(cast(str | None, last_error))

        await self._execute_update(
            f"UPDATE webhook_deliveries SET {', '.join(assignments)} WHERE delivery_id = ?",
            (*params, delivery_id),
        )

    async def _execute_update(self, sql: str, params: Sequence[object]) -> None:
        async with get_db("jobs.db") as db:
            await db.execute(sql, params)
            await db.commit()
