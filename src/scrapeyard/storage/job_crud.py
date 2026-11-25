"""Core job CRUD, admission check, and idempotency queries for jobs.db."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import cast

import aiosqlite

from scrapeyard.common.dt import fmt_dt
from scrapeyard.common.settings import get_settings
from scrapeyard.models.job import Job, JobMetadata, JobStatus
from scrapeyard.queue.memory import memory_headroom_mb
from scrapeyard.storage.job_rows import row_to_job, row_to_job_metadata
from scrapeyard.storage.job_sql import JOB_COLUMNS, JOB_METADATA_COLUMNS, select_columns
from scrapeyard.storage.secret_envelope import protect_text
from scrapeyard.storage.types import IdempotentJobAction, IdempotentJobOutcome

_OFFLOAD_CRYPTO_THRESHOLD_CHARS = 16 * 1024


def prepare_config_crypto(
    config_yaml: str,
    *,
    job_purpose: str,
    snapshot_purpose: str | None = None,
) -> tuple[str, str, str | None]:
    config_hash = hashlib.sha256(config_yaml.encode("utf-8")).hexdigest()
    protected_primary = protect_text(config_yaml, purpose=job_purpose)
    protected_snapshot = (
        None if snapshot_purpose is None else protect_text(config_yaml, purpose=snapshot_purpose)
    )
    return config_hash, protected_primary, protected_snapshot


async def prepare_config_crypto_async(
    config_yaml: str,
    *,
    job_purpose: str,
    snapshot_purpose: str | None = None,
) -> tuple[str, str, str | None]:
    if len(config_yaml) >= _OFFLOAD_CRYPTO_THRESHOLD_CHARS:
        return await asyncio.to_thread(
            prepare_config_crypto,
            config_yaml,
            job_purpose=job_purpose,
            snapshot_purpose=snapshot_purpose,
        )
    return prepare_config_crypto(
        config_yaml, job_purpose=job_purpose, snapshot_purpose=snapshot_purpose
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def sha256_text_async(text: str) -> str:
    if len(text) >= _OFFLOAD_CRYPTO_THRESHOLD_CHARS:
        return await asyncio.to_thread(sha256_text, text)
    return sha256_text(text)


async def check_admission(
    db: aiosqlite.Connection,
    *,
    capacity_error_cls: type[Exception],
) -> None:
    """Check the just-reserved run inside its write transaction."""

    settings = get_settings()
    cursor = await db.execute(
        """SELECT COUNT(*) FROM jobs
           WHERE status IN ('queued', 'running') AND current_run_id IS NOT NULL"""
    )
    row = await cursor.fetchone()
    assert row is not None
    if row[0] > settings.workers_max_accepted_runs:
        raise capacity_error_cls("backlog")
    if memory_headroom_mb(settings.workers_memory_limit_mb) <= 0:
        raise capacity_error_cls("memory")


async def get_job_in_db(db: aiosqlite.Connection, job_id: str) -> Job | None:
    cursor = await db.execute(
        f"SELECT {select_columns(JOB_COLUMNS)} FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else row_to_job(cast(Mapping[str, object], row))


async def get_job_metadata_in_db(
    db: aiosqlite.Connection,
    job_id: str,
) -> JobMetadata | None:
    cursor = await db.execute(
        f"SELECT {select_columns(JOB_METADATA_COLUMNS)} FROM jobs WHERE job_id = ?",
        (job_id,),
    )
    row = await cursor.fetchone()
    return None if row is None else row_to_job_metadata(cast(Mapping[str, object], row))


async def insert_job(
    db: aiosqlite.Connection,
    job: Job,
    *,
    prepared: tuple[str, str, str | None],
    capacity_error_cls: type[Exception],
) -> None:
    current_trigger = job.current_trigger
    if current_trigger is None and job.current_run_id is not None:
        current_trigger = "scheduled" if job.schedule_cron is not None else "adhoc"
    needs_snapshot = job.status is JobStatus.queued and job.current_run_id is not None
    config_hash, protected_config, protected_snapshot = prepared
    await db.execute(
        """INSERT INTO jobs (job_id, project, name, status,
           config_yaml, config_hash, created_at, updated_at, schedule_cron,
           schedule_timezone, schedule_enabled, current_run_id, current_trigger,
           deletion_requested_at, delete_results_on_delete,
           schedule_failure_at, schedule_failure_code,
           schedule_consecutive_failures)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job.job_id,
            job.project,
            job.name,
            job.status.value,
            protected_config,
            config_hash,
            fmt_dt(job.created_at),
            fmt_dt(job.updated_at),
            job.schedule_cron,
            job.schedule_timezone,
            int(job.schedule_enabled),
            job.current_run_id,
            current_trigger,
            fmt_dt(job.deletion_requested_at),
            (
                None
                if job.delete_results_on_delete is None
                else int(job.delete_results_on_delete)
            ),
            fmt_dt(job.schedule_failure_at),
            job.schedule_failure_code,
            job.schedule_consecutive_failures,
        ),
    )
    if needs_snapshot and job.current_run_id is not None:
        await check_admission(db, capacity_error_cls=capacity_error_cls)
        trigger = current_trigger or "adhoc"
        queued_at = job.updated_at or job.created_at
        snapshot_ciphertext = (
            protected_snapshot
            if protected_snapshot is not None
            else protect_text(
                job.config_yaml,
                purpose=f"queued_run_snapshots.config_yaml:{job.current_run_id}",
            )
        )
        await db.execute(
            """INSERT INTO queued_run_snapshots
                   (run_id, job_id, trigger, config_hash, config_yaml, queued_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
            (
                job.current_run_id,
                job.job_id,
                trigger,
                config_hash,
                snapshot_ciphertext,
                fmt_dt(queued_at),
            ),
        )


async def create_idempotent_job(
    db: aiosqlite.Connection,
    job: Job,
    *,
    prepared: tuple[str, str, str | None],
    caller_scope: str,
    key_digest: str,
    request_hash: str,
    response_mode: str,
    expires_at: datetime,
    is_duplicate_error: Callable[[Exception], bool],
    duplicate_error_cls: type[Exception],
    capacity_error_cls: type[Exception],
) -> IdempotentJobOutcome:
    """Create one job/idempotency pair or return the serialized winner."""

    assert job.current_run_id is not None
    await db.execute(
        """DELETE FROM scrape_idempotency
           WHERE caller_scope = ? AND key_digest = ? AND expires_at <= ?""",
        (caller_scope, key_digest, fmt_dt(job.created_at)),
    )
    cursor = await db.execute(
        f"""SELECT scrape_idempotency.request_hash,
                   scrape_idempotency.run_id,
                   scrape_idempotency.response_mode,
                   {select_columns(JOB_COLUMNS, table_alias='jobs')}
            FROM scrape_idempotency
            JOIN jobs ON jobs.job_id = scrape_idempotency.job_id
            WHERE scrape_idempotency.caller_scope = ?
              AND scrape_idempotency.key_digest = ?""",
        (caller_scope, key_digest),
    )
    row = await cursor.fetchone()
    if row is not None:
        existing = row_to_job(cast(Mapping[str, object], row))
        action = (
            IdempotentJobAction.matched
            if row["request_hash"] == request_hash and row["response_mode"] == response_mode
            else IdempotentJobAction.conflict
        )
        return IdempotentJobOutcome(
            action=action,
            job=existing,
            run_id=cast(str, row["run_id"]),
            response_mode=cast(str, row["response_mode"]),
        )

    try:
        await insert_job(
            db,
            job,
            prepared=prepared,
            capacity_error_cls=capacity_error_cls,
        )
    except aiosqlite.IntegrityError as exc:
        if is_duplicate_error(exc):
            raise duplicate_error_cls(job.project, job.name) from exc
        raise
    await db.execute(
        """INSERT INTO scrape_idempotency
           (caller_scope, key_digest, request_hash, job_id, run_id,
            response_mode, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            caller_scope,
            key_digest,
            request_hash,
            job.job_id,
            job.current_run_id,
            response_mode,
            fmt_dt(job.created_at),
            fmt_dt(expires_at),
        ),
    )
    return IdempotentJobOutcome(
        action=IdempotentJobAction.created,
        job=job,
        run_id=job.current_run_id,
        response_mode=response_mode,
    )


async def get_submission_receipt(
    db: aiosqlite.Connection,
    caller_scope: str,
    key_digest: str,
    observed_at: datetime,
) -> dict[str, object] | None:
    cursor = await db.execute(
        """SELECT i.job_id, i.run_id, i.request_hash AS config_hash, j.project,
                  i.created_at, i.expires_at
           FROM scrape_idempotency i JOIN jobs j ON j.job_id = i.job_id
           WHERE i.caller_scope = ? AND i.key_digest = ? AND i.expires_at > ?""",
        (caller_scope, key_digest, fmt_dt(observed_at)),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def delete_expired_idempotency_records(
    db: aiosqlite.Connection,
    expired_before: datetime,
    *,
    limit: int,
) -> int:
    cursor = await db.execute(
        """DELETE FROM scrape_idempotency
           WHERE rowid IN (
               SELECT rowid FROM scrape_idempotency
               WHERE expires_at <= ?
               ORDER BY expires_at, caller_scope, key_digest
               LIMIT ?
           )""",
        (fmt_dt(expired_before), limit),
    )
    return cursor.rowcount
