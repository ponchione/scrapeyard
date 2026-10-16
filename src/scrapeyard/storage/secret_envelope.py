"""Versioned authenticated encryption for durable secret-bearing fields."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import aiosqlite
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scrapeyard.common.settings import get_settings
from scrapeyard.storage.database import db_transaction, get_db

_PREFIX = "syenc:v1:"
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SecretKeyConfigurationError(RuntimeError):
    """Raised when deployment key material is absent or malformed."""


class SecretDecryptionError(RuntimeError):
    """Raised when a durable envelope cannot be authenticated/decrypted."""


@dataclass(frozen=True, slots=True)
class EncryptionKeyring:
    keys: dict[str, bytes]
    active_key_id: str

    @classmethod
    def from_settings(cls, *, required: bool = True) -> EncryptionKeyring | None:
        settings = get_settings()
        raw = settings.encryption_keys.strip()
        active_key_id = settings.encryption_active_key_id.strip()
        if not raw and not active_key_id and not required:
            return None
        if not raw or not active_key_id:
            raise SecretKeyConfigurationError(
                "SCRAPEYARD_ENCRYPTION_KEYS and "
                "SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID must be configured together"
            )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretKeyConfigurationError(
                "SCRAPEYARD_ENCRYPTION_KEYS must be a JSON object"
            ) from exc
        if not isinstance(parsed, dict) or not parsed:
            raise SecretKeyConfigurationError(
                "SCRAPEYARD_ENCRYPTION_KEYS must be a non-empty JSON object"
            )
        keys: dict[str, bytes] = {}
        for key_id, encoded in parsed.items():
            if not isinstance(key_id, str) or _KEY_ID_RE.fullmatch(key_id) is None:
                raise SecretKeyConfigurationError("Encryption key IDs must be safe names")
            if not isinstance(encoded, str):
                raise SecretKeyConfigurationError("Encryption keys must be base64 strings")
            try:
                key = base64.b64decode(
                    encoded.encode("ascii"),
                    altchars=b"-_",
                    validate=True,
                )
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise SecretKeyConfigurationError(
                    f"Encryption key {key_id!r} is not valid URL-safe base64"
                ) from exc
            if len(key) != 32:
                raise SecretKeyConfigurationError(
                    f"Encryption key {key_id!r} must decode to exactly 32 bytes"
                )
            keys[key_id] = key
        if active_key_id not in keys:
            raise SecretKeyConfigurationError(
                "SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID is absent from the keyring"
            )
        return cls(keys, active_key_id)

    def protect(self, plaintext: str, *, purpose: str) -> str:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.keys[self.active_key_id]).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            purpose.encode("utf-8"),
        )
        token = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{_PREFIX}{self.active_key_id}:{token}"

    def reveal(self, value: str, *, purpose: str) -> str:
        key_id = envelope_key_id(value)
        if key_id is None:
            return value
        key = self.keys.get(key_id)
        if key is None:
            raise SecretDecryptionError(
                f"Persisted secret requires unavailable encryption key {key_id!r}"
            )
        token = value.split(":", 3)[3]
        try:
            combined = base64.b64decode(
                token.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            if len(combined) < 12 + 16:
                raise ValueError("short ciphertext")
            return AESGCM(key).decrypt(
                combined[:12],
                combined[12:],
                purpose.encode("utf-8"),
            ).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError, binascii.Error, InvalidTag, ValueError) as exc:
            raise SecretDecryptionError(
                "Persisted secret envelope failed authentication"
            ) from exc


def envelope_key_id(value: str) -> str | None:
    if not value.startswith(_PREFIX):
        return None
    parts = value.split(":", 3)
    if len(parts) != 4 or not parts[2] or not parts[3]:
        raise SecretDecryptionError("Persisted secret envelope is malformed")
    return parts[2]


def protect_text(plaintext: str, *, purpose: str) -> str:
    keyring = EncryptionKeyring.from_settings(required=True)
    assert keyring is not None
    return keyring.protect(plaintext, purpose=purpose)


def reveal_text(value: str, *, purpose: str) -> str:
    keyring = EncryptionKeyring.from_settings(required=True)
    assert keyring is not None
    return keyring.reveal(value, purpose=purpose)


# Rows per page while migrating, so startup memory does not grow with history.
_MIGRATION_PAGE_ROWS = 100


async def _paged_rows(db: aiosqlite.Connection, sql: str) -> AsyncIterator[Sequence[Any]]:
    """Yield rows of *sql* in key order, one page at a time.

    *sql* filters on its first selected column with ``> ?``, orders by it, and
    takes the page size as a second parameter. Keyset paging stays exact while
    the same transaction rewrites the rows it has already returned.
    """
    after = ""
    while True:
        async with db.execute(sql, (after, _MIGRATION_PAGE_ROWS)) as cursor:
            rows = list(await cursor.fetchall())
        for row in rows:
            yield row
        if len(rows) < _MIGRATION_PAGE_ROWS:
            return
        after = str(rows[-1][0])


async def migrate_persisted_secrets() -> None:
    """Atomically encrypt legacy plaintext and rotate envelopes to the active key."""

    keyring = EncryptionKeyring.from_settings(required=False)
    legacy_migrated = False
    secure_delete_before: int | None = None
    async with get_db("jobs.db") as db:

        async def rewrite(sql: str, params: tuple[Any, ...]) -> None:
            # Scrub the replaced plaintext or old-key ciphertext from freed
            # pages. Only rewrites need it; it multiplies WAL bytes per delete.
            nonlocal secure_delete_before
            if secure_delete_before is None:
                async with db.execute("PRAGMA secure_delete") as cursor:
                    setting = await cursor.fetchone()
                secure_delete_before = 0 if setting is None else int(setting[0])
                await db.execute("PRAGMA secure_delete = ON")
            await db.execute(sql, params)

        try:
            async with db_transaction(db):
                if keyring is None:
                    async with db.execute(
                        """SELECT EXISTS (SELECT 1 FROM jobs)
                               OR EXISTS (SELECT 1 FROM webhook_deliveries
                                          WHERE scrubbed_at IS NULL)
                               OR EXISTS (SELECT 1 FROM job_runs
                                          WHERE config_yaml IS NOT NULL)
                               OR EXISTS (SELECT 1 FROM queued_run_snapshots)"""
                    ) as cursor:
                        protected_state = await cursor.fetchone()
                    if protected_state is not None and protected_state[0]:
                        raise SecretKeyConfigurationError(
                            "Persisted jobs or webhook requests require deployment encryption keys"
                        )
                    return

                async for row in _paged_rows(
                    db,
                    """SELECT job_id, config_yaml, config_hash FROM jobs
                       WHERE job_id > ? ORDER BY job_id LIMIT ?""",
                ):
                    job_id = str(row[0])
                    stored = str(row[1])
                    purpose = f"jobs.config_yaml:{job_id}"
                    key_id = envelope_key_id(stored)
                    plaintext = keyring.reveal(stored, purpose=purpose)
                    expected_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
                    legacy_migrated |= key_id is None
                    protected = (
                        stored
                        if key_id == keyring.active_key_id
                        else keyring.protect(plaintext, purpose=purpose)
                    )
                    if protected != stored or str(row[2]) != expected_hash:
                        await rewrite(
                            "UPDATE jobs SET config_yaml = ?, config_hash = ? "
                            "WHERE job_id = ?",
                            (protected, expected_hash, job_id),
                        )

                async for row in _paged_rows(
                    db,
                    """SELECT run_id, config_yaml FROM job_runs
                       WHERE run_id > ? AND config_yaml IS NOT NULL
                       ORDER BY run_id LIMIT ?""",
                ):
                    run_id = str(row[0])
                    stored = str(row[1])
                    purpose = f"job_runs.config_yaml:{run_id}"
                    key_id = envelope_key_id(stored)
                    run_plaintext = keyring.reveal(stored, purpose=purpose)
                    legacy_migrated |= key_id is None
                    if key_id != keyring.active_key_id:
                        await rewrite(
                            "UPDATE job_runs SET config_yaml = ? WHERE run_id = ?",
                            (keyring.protect(run_plaintext, purpose=purpose), run_id),
                        )

                # Legacy runs without a snapshot take their parent's config when
                # its hash proves it is the config the run executed. A run
                # with a failure code was written after 018 added both
                # columns, by a path that stores the snapshot it has; a
                # missed scheduled fire has none on purpose and stays so.
                async for row in _paged_rows(
                    db,
                    """SELECT job_runs.run_id, job_runs.job_id, jobs.config_yaml,
                              job_runs.config_hash
                       FROM job_runs JOIN jobs ON jobs.job_id = job_runs.job_id
                       WHERE job_runs.run_id > ? AND job_runs.config_yaml IS NULL
                         AND job_runs.failure_code IS NULL
                       ORDER BY job_runs.run_id LIMIT ?""",
                ):
                    run_id = str(row[0])
                    run_plaintext = keyring.reveal(
                        str(row[2]), purpose=f"jobs.config_yaml:{row[1]}"
                    )
                    if hashlib.sha256(run_plaintext.encode("utf-8")).hexdigest() != str(row[3]):
                        continue
                    await rewrite(
                        "UPDATE job_runs SET config_yaml = ? WHERE run_id = ?",
                        (
                            keyring.protect(
                                run_plaintext, purpose=f"job_runs.config_yaml:{run_id}"
                            ),
                            run_id,
                        ),
                    )

                async for row in _paged_rows(
                    db,
                    """SELECT run_id, config_yaml, config_hash FROM queued_run_snapshots
                       WHERE run_id > ? ORDER BY run_id LIMIT ?""",
                ):
                    run_id = str(row[0])
                    stored = str(row[1])
                    purpose = f"queued_run_snapshots.config_yaml:{run_id}"
                    key_id = envelope_key_id(stored)
                    plaintext = keyring.reveal(stored, purpose=purpose)
                    expected_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
                    legacy_migrated |= key_id is None
                    protected = (
                        stored
                        if key_id == keyring.active_key_id
                        else keyring.protect(plaintext, purpose=purpose)
                    )
                    if protected != stored or str(row[2]) != expected_hash:
                        await rewrite(
                            """UPDATE queued_run_snapshots
                               SET config_yaml = ?, config_hash = ? WHERE run_id = ?""",
                            (protected, expected_hash, run_id),
                        )

                async for row in _paged_rows(
                    db,
                    """SELECT delivery_id, url, headers_json, payload_json, last_error
                       FROM webhook_deliveries
                       WHERE delivery_id > ? AND scrubbed_at IS NULL
                       ORDER BY delivery_id LIMIT ?""",
                ):
                    delivery_id = str(row[0])
                    updates: dict[str, str] = {}
                    for column, raw_value in (
                        ("url", row[1]),
                        ("headers_json", row[2]),
                        ("payload_json", row[3]),
                    ):
                        stored = str(raw_value)
                        purpose = f"webhook.{column}:{delivery_id}"
                        key_id = envelope_key_id(stored)
                        plaintext = keyring.reveal(stored, purpose=purpose)
                        legacy_migrated |= key_id is None
                        if key_id != keyring.active_key_id:
                            updates[column] = keyring.protect(
                                plaintext,
                                purpose=purpose,
                            )
                    if row[4] is not None:
                        stored_error = str(row[4])
                        purpose = f"webhook.last_error:{delivery_id}"
                        error_key_id = envelope_key_id(stored_error)
                        plaintext_error = keyring.reveal(stored_error, purpose=purpose)
                        legacy_migrated |= error_key_id is None
                        if error_key_id != keyring.active_key_id:
                            updates["last_error"] = keyring.protect(
                                plaintext_error,
                                purpose=purpose,
                            )
                    if updates:
                        assignments = ", ".join(f"{column} = ?" for column in updates)
                        await rewrite(
                            f"UPDATE webhook_deliveries SET {assignments} "
                            "WHERE delivery_id = ?",
                            (*updates.values(), delivery_id),
                        )

            if legacy_migrated:
                cursor = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                await cursor.fetchall()
                await cursor.close()
        finally:
            if secure_delete_before is not None:
                await db.execute(f"PRAGMA secure_delete = {secure_delete_before}")
