from __future__ import annotations

import base64
import hashlib
import json
import shutil
from datetime import datetime, timezone

import pytest

from scrapeyard.common.settings import get_settings
from scrapeyard.models.job import Job
from scrapeyard.storage.database import close_db, get_db, init_db
from scrapeyard.storage.job_store import SQLiteJobStore
from scrapeyard.storage.secret_envelope import (
    EncryptionKeyring,
    SecretDecryptionError,
    SecretKeyConfigurationError,
    migrate_persisted_secrets,
)
from scrapeyard.storage.webhook_outbox import (
    SQLiteWebhookOutboxStore,
    WebhookDeliveryCreate,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
CONFIG_SECRET = "proxy-password-sentinel"
HEADER_SECRET = "webhook-bearer-sentinel"
URL_SECRET = "webhook-query-token-sentinel"
PAYLOAD_SECRET = "payload-secret-sentinel"


def _encoded(byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * 32).decode("ascii")


def _set_keys(monkeypatch, keys: dict[str, str], active: str) -> None:
    monkeypatch.setenv("SCRAPEYARD_ENCRYPTION_KEYS", json.dumps(keys))
    monkeypatch.setenv("SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID", active)
    get_settings.cache_clear()


def _clear_keys(monkeypatch) -> None:
    monkeypatch.delenv("SCRAPEYARD_ENCRYPTION_KEYS", raising=False)
    monkeypatch.delenv("SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID", raising=False)
    get_settings.cache_clear()


def _job(job_id: str = "secret-job") -> Job:
    return Job(
        job_id=job_id,
        project="secrets",
        name=job_id,
        config_yaml=f"""
project: secrets
name: {job_id}
proxy:
  url: https://proxy-user:{CONFIG_SECRET}@proxy.example.com:8443
target:
  url: https://example.com/path?api_key={URL_SECRET}
  browser:
    extra_headers:
      Authorization: Bearer browser-header-sentinel
  selectors:
    title: h1
webhook:
  url: https://hooks.example.com/callback?token={URL_SECRET}
  headers:
    Authorization: Bearer {HEADER_SECRET}
""",
    )


def _delivery(delivery_id: str = "secret-delivery") -> WebhookDeliveryCreate:
    return WebhookDeliveryCreate(
        delivery_id=delivery_id,
        job_id="secret-job",
        run_id="run-1",
        event="job.complete",
        url=f"https://hooks.example.com/callback?token={URL_SECRET}",
        headers={"Authorization": f"Bearer {HEADER_SECRET}"},
        timeout_seconds=5,
        payload={"delivery_id": delivery_id, "secret": PAYLOAD_SECRET},
        next_attempt_at=NOW,
    )


@pytest.mark.parametrize(
    "keys,active",
    [
        ({}, "missing"),
        ({"bad key id": _encoded(b"x")}, "bad key id"),
        ({"short": base64.urlsafe_b64encode(b"short").decode()}, "short"),
        ({"garbage": f"{_encoded(b'x')}!"}, "garbage"),
        ({"available": _encoded(b"x")}, "missing"),
    ],
)
def test_invalid_keyring_configuration_fails_closed(monkeypatch, keys, active):
    _set_keys(monkeypatch, keys, active)
    with pytest.raises(SecretKeyConfigurationError):
        EncryptionKeyring.from_settings()


async def test_new_rows_encrypt_full_config_and_retry_request_state(tmp_path):
    await init_db(str(tmp_path / "db"))
    jobs = SQLiteJobStore()
    outbox = SQLiteWebhookOutboxStore()
    job = _job().model_copy(update={"current_run_id": "run-1"})
    await jobs.save_job(job)
    assert await jobs.claim_run(
        "run-1",
        job.job_id,
        "adhoc",
        hashlib.sha256(job.config_yaml.encode()).hexdigest(),
        NOW,
    )
    await outbox.enqueue_delivery(_delivery(), now=NOW)

    async with get_db("jobs.db") as db:
        job_row = await (
            await db.execute("SELECT config_yaml, config_hash FROM jobs")
        ).fetchone()
        webhook_row = await (
            await db.execute(
                "SELECT url, headers_json, payload_json FROM webhook_deliveries"
            )
        ).fetchone()
        run_row = await (
            await db.execute("SELECT config_yaml FROM job_runs WHERE run_id = 'run-1'")
        ).fetchone()
    raw_values = " ".join(str(value) for value in (*job_row, *run_row, *webhook_row))
    for sentinel in (CONFIG_SECRET, HEADER_SECRET, URL_SECRET, PAYLOAD_SECRET):
        assert sentinel not in raw_values
    assert job_row[0].startswith("syenc:v1:test-v1:")
    assert job_row[1] == hashlib.sha256(_job().config_yaml.encode()).hexdigest()
    assert run_row[0].startswith("syenc:v1:test-v1:")
    assert all(str(value).startswith("syenc:v1:test-v1:") for value in webhook_row)

    restored_job = await jobs.get_job("secret-job")
    restored_delivery = await outbox.get_delivery("secret-delivery")
    assert restored_job.config_yaml == _job().config_yaml
    assert restored_delivery is not None
    assert restored_delivery.headers["Authorization"].endswith(HEADER_SECRET)
    assert restored_delivery.payload["secret"] == PAYLOAD_SECRET


async def test_retry_error_is_encrypted_at_rest_and_decrypts_for_dispatch(tmp_path):
    await init_db(str(tmp_path / "db"))
    outbox = SQLiteWebhookOutboxStore()
    await outbox.enqueue_delivery(_delivery(), now=NOW)
    retry_error = f"timeout contacting https://hooks.example/?token={URL_SECRET}"

    assert await outbox.mark_retryable_failure(
        "secret-delivery",
        attempted_at=NOW,
        next_attempt_at=NOW,
        last_error=retry_error,
    )

    async with get_db("jobs.db") as db:
        row = await (
            await db.execute(
                "SELECT last_error FROM webhook_deliveries "
                "WHERE delivery_id = 'secret-delivery'"
            )
        ).fetchone()
    assert row[0].startswith("syenc:v1:test-v1:")
    assert retry_error not in row[0]
    restored = await outbox.get_delivery("secret-delivery")
    assert restored is not None
    assert restored.last_error == retry_error


async def test_rotation_reencrypts_pending_state_then_old_key_can_be_removed(
    tmp_path,
    monkeypatch,
):
    _set_keys(monkeypatch, {"old": _encoded(b"o")}, "old")
    await init_db(str(tmp_path / "db"))
    jobs = SQLiteJobStore()
    outbox = SQLiteWebhookOutboxStore()
    job = _job().model_copy(update={"current_run_id": "run-1"})
    await jobs.save_job(job)
    assert await jobs.claim_run(
        "run-1",
        job.job_id,
        "adhoc",
        hashlib.sha256(job.config_yaml.encode()).hexdigest(),
        NOW,
    )
    await outbox.enqueue_delivery(_delivery(), now=NOW)
    await outbox.mark_retryable_failure(
        "secret-delivery",
        attempted_at=NOW,
        next_attempt_at=NOW,
        last_error="old-key retry error",
    )

    _set_keys(
        monkeypatch,
        {"old": _encoded(b"o"), "new": _encoded(b"n")},
        "new",
    )
    await migrate_persisted_secrets()
    async with get_db("jobs.db") as db:
        row = await (
            await db.execute(
                "SELECT jobs.config_yaml, job_runs.config_yaml, "
                "webhook_deliveries.headers_json, "
                "webhook_deliveries.last_error "
                "FROM jobs JOIN job_runs ON job_runs.job_id = jobs.job_id "
                "JOIN webhook_deliveries ON webhook_deliveries.job_id = jobs.job_id"
            )
        ).fetchone()
    assert all(str(value).startswith("syenc:v1:new:") for value in row)

    _set_keys(monkeypatch, {"new": _encoded(b"n")}, "new")
    assert (await jobs.get_job("secret-job")).config_yaml == _job().config_yaml
    pending = await outbox.get_delivery("secret-delivery")
    assert pending is not None and pending.payload["secret"] == PAYLOAD_SECRET
    assert pending.last_error == "old-key retry error"


async def test_backup_restore_requires_intended_key_material(tmp_path, monkeypatch):
    _set_keys(monkeypatch, {"backup": _encoded(b"b")}, "backup")
    source = tmp_path / "source"
    restored = tmp_path / "restored"
    await init_db(str(source))
    await SQLiteJobStore().save_job(_job())
    await close_db()
    shutil.copytree(source, restored)

    _clear_keys(monkeypatch)
    await init_db(str(restored))
    with pytest.raises(SecretKeyConfigurationError):
        await SQLiteJobStore().get_job("secret-job")
    await close_db()

    _set_keys(monkeypatch, {"backup": _encoded(b"b")}, "backup")
    await init_db(str(restored))
    assert (await SQLiteJobStore().get_job("secret-job")).config_yaml == _job().config_yaml


async def test_plaintext_migration_is_atomic_and_purges_sentinel_bytes(
    tmp_path,
):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    plaintext = _job("legacy-job").config_yaml
    async with get_db("jobs.db") as db:
        await db.execute(
            """INSERT INTO jobs
               (job_id, project, name, status, config_yaml, created_at)
               VALUES ('legacy-job', 'secrets', 'legacy-job', 'queued', ?, ?)""",
            (plaintext, NOW.isoformat()),
        )
        await db.execute(
            """INSERT INTO webhook_deliveries
               (delivery_id, job_id, event, url, headers_json, payload_json,
                next_attempt_at, created_at, updated_at)
               VALUES ('legacy-delivery', 'legacy-job', 'job.complete', ?, ?, ?, ?, ?, ?)""",
            (
                f"https://hooks.example.com/?token={URL_SECRET}",
                json.dumps({"Authorization": HEADER_SECRET}),
                json.dumps(
                    {"delivery_id": "legacy-delivery", "secret": PAYLOAD_SECRET}
                ),
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        await db.commit()

    await migrate_persisted_secrets()
    assert (await SQLiteJobStore().get_job("legacy-job")).config_yaml == plaintext
    legacy_delivery = await SQLiteWebhookOutboxStore().get_delivery(
        "legacy-delivery"
    )
    assert legacy_delivery is not None
    assert legacy_delivery.payload["secret"] == PAYLOAD_SECRET
    await close_db()
    raw_backup = b"".join(path.read_bytes() for path in db_dir.glob("jobs.db*"))
    assert CONFIG_SECRET.encode() not in raw_backup
    assert HEADER_SECRET.encode() not in raw_backup
    assert URL_SECRET.encode() not in raw_backup
    assert PAYLOAD_SECRET.encode() not in raw_backup


async def test_encrypted_pending_webhook_survives_normal_restart(tmp_path):
    db_dir = tmp_path / "db"
    await init_db(str(db_dir))
    await SQLiteWebhookOutboxStore().enqueue_delivery(_delivery(), now=NOW)
    await close_db()

    await init_db(str(db_dir))
    await migrate_persisted_secrets()
    restored = await SQLiteWebhookOutboxStore().get_delivery("secret-delivery")
    assert restored is not None
    assert restored.url.endswith(URL_SECRET)
    assert restored.headers["Authorization"].endswith(HEADER_SECRET)


async def test_second_startup_with_active_key_performs_no_row_updates(tmp_path):
    await init_db(str(tmp_path / "db"))
    await SQLiteJobStore().save_job(_job())
    outbox = SQLiteWebhookOutboxStore()
    await outbox.enqueue_delivery(_delivery(), now=NOW)
    await outbox.mark_retryable_failure(
        "secret-delivery",
        attempted_at=NOW,
        next_attempt_at=NOW,
        last_error="encrypted retry error",
    )
    async with get_db("jobs.db") as db:
        before_job = tuple(
            await (
                await db.execute(
                    "SELECT config_yaml, config_hash FROM jobs WHERE job_id = 'secret-job'"
                )
            ).fetchone()
        )
        before_webhook = tuple(
            await (
                await db.execute(
                    """SELECT url, headers_json, payload_json, last_error
                       FROM webhook_deliveries
                       WHERE delivery_id = 'secret-delivery'"""
                )
            ).fetchone()
        )
        await db.execute("CREATE TABLE migration_update_audit (table_name TEXT)")
        await db.execute(
            """CREATE TRIGGER audit_job_secret_update AFTER UPDATE ON jobs
               BEGIN
                   INSERT INTO migration_update_audit VALUES ('jobs');
               END"""
        )
        await db.execute(
            """CREATE TRIGGER audit_webhook_secret_update
               AFTER UPDATE ON webhook_deliveries
               BEGIN
                   INSERT INTO migration_update_audit VALUES ('webhook_deliveries');
               END"""
        )
        await db.commit()

    await migrate_persisted_secrets()

    async with get_db("jobs.db") as db:
        updates = await (
            await db.execute("SELECT table_name FROM migration_update_audit")
        ).fetchall()
        after_job = tuple(
            await (
                await db.execute(
                    "SELECT config_yaml, config_hash FROM jobs WHERE job_id = 'secret-job'"
                )
            ).fetchone()
        )
        after_webhook = tuple(
            await (
                await db.execute(
                    """SELECT url, headers_json, payload_json, last_error
                       FROM webhook_deliveries
                       WHERE delivery_id = 'secret-delivery'"""
                )
            ).fetchone()
        )

    assert updates == []
    assert after_job == before_job
    assert after_webhook == before_webhook


async def test_current_envelope_with_stale_hash_repairs_only_job_row(tmp_path):
    await init_db(str(tmp_path / "db"))
    await SQLiteJobStore().save_job(_job())
    await SQLiteWebhookOutboxStore().enqueue_delivery(_delivery(), now=NOW)
    async with get_db("jobs.db") as db:
        before_envelope = (
            await (
                await db.execute(
                    "SELECT config_yaml FROM jobs WHERE job_id = 'secret-job'"
                )
            ).fetchone()
        )[0]
        await db.execute(
            "UPDATE jobs SET config_hash = 'stale' WHERE job_id = 'secret-job'"
        )
        await db.execute("CREATE TABLE migration_update_audit (table_name TEXT)")
        await db.execute(
            """CREATE TRIGGER audit_job_secret_update AFTER UPDATE ON jobs
               BEGIN
                   INSERT INTO migration_update_audit VALUES ('jobs');
               END"""
        )
        await db.execute(
            """CREATE TRIGGER audit_webhook_secret_update
               AFTER UPDATE ON webhook_deliveries
               BEGIN
                   INSERT INTO migration_update_audit VALUES ('webhook_deliveries');
               END"""
        )
        await db.commit()

    await migrate_persisted_secrets()

    async with get_db("jobs.db") as db:
        row = await (
            await db.execute(
                "SELECT config_yaml, config_hash FROM jobs WHERE job_id = 'secret-job'"
            )
        ).fetchone()
        updates = await (
            await db.execute("SELECT table_name FROM migration_update_audit")
        ).fetchall()
    assert row[0] == before_envelope
    assert row[1] == hashlib.sha256(_job().config_yaml.encode()).hexdigest()
    assert [update[0] for update in updates] == ["jobs"]


async def test_migration_failure_rolls_back_without_destroying_plaintext(
    tmp_path,
):
    await init_db(str(tmp_path / "db"))
    first_plaintext = _job("a-legacy").config_yaml
    async with get_db("jobs.db") as db:
        await db.executemany(
            """INSERT INTO jobs
               (job_id, project, name, status, config_yaml, created_at)
               VALUES (?, 'secrets', ?, 'queued', ?, ?)""",
            [
                ("a-legacy", "a-legacy", first_plaintext, NOW.isoformat()),
                (
                    "z-corrupt",
                    "z-corrupt",
                    "syenc:v1:test-v1:not-valid-ciphertext",
                    NOW.isoformat(),
                ),
            ],
        )
        await db.commit()

    with pytest.raises(SecretDecryptionError):
        await migrate_persisted_secrets()
    async with get_db("jobs.db") as db:
        row = await (
            await db.execute(
                "SELECT config_yaml, config_hash FROM jobs WHERE job_id = 'a-legacy'"
            )
        ).fetchone()
    assert row[0] == first_plaintext
    assert row[1] == ""


async def test_missing_keys_fail_closed_without_mutating_ciphertext(
    tmp_path,
    monkeypatch,
):
    await init_db(str(tmp_path / "db"))
    await SQLiteJobStore().save_job(_job())
    async with get_db("jobs.db") as db:
        before = (
            await (
                await db.execute(
                    "SELECT config_yaml FROM jobs WHERE job_id = 'secret-job'"
                )
            ).fetchone()
        )[0]

    _clear_keys(monkeypatch)
    with pytest.raises(SecretKeyConfigurationError):
        await migrate_persisted_secrets()
    async with get_db("jobs.db") as db:
        after = (
            await (
                await db.execute(
                    "SELECT config_yaml FROM jobs WHERE job_id = 'secret-job'"
                )
            ).fetchone()
        )[0]
    assert after == before


async def test_missing_envelope_key_id_fails_closed(tmp_path, monkeypatch):
    _set_keys(monkeypatch, {"old": _encoded(b"o")}, "old")
    await init_db(str(tmp_path / "db"))
    await SQLiteJobStore().save_job(_job())

    _set_keys(monkeypatch, {"new": _encoded(b"n")}, "new")
    with pytest.raises(SecretDecryptionError, match="unavailable encryption key"):
        await migrate_persisted_secrets()
