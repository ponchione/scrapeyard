"""Versioned authenticated encryption for durable secret-bearing fields."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from dataclasses import dataclass
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


async def migrate_persisted_secrets() -> None:
    """Atomically encrypt legacy plaintext and rotate envelopes to the active key."""

    keyring = EncryptionKeyring.from_settings(required=False)
    legacy_migrated = False
    async with get_db("jobs.db") as db:
        await db.execute("PRAGMA secure_delete = ON")
        async with db_transaction(db):
            job_rows = await (
                await db.execute(
                    "SELECT job_id, config_yaml, config_hash FROM jobs ORDER BY job_id"
                )
            ).fetchall()
            webhook_rows = await (
                await db.execute(
                    """SELECT delivery_id, url, headers_json, payload_json,
                              last_error, scrubbed_at
                       FROM webhook_deliveries ORDER BY delivery_id"""
                )
            ).fetchall()
            protected_state_exists = bool(job_rows) or any(
                row[5] is None for row in webhook_rows
            )
            if keyring is None:
                if protected_state_exists:
                    raise SecretKeyConfigurationError(
                        "Persisted jobs or webhook requests require deployment encryption keys"
                    )
                return

            for row in job_rows:
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
                    await db.execute(
                        "UPDATE jobs SET config_yaml = ?, config_hash = ? "
                        "WHERE job_id = ?",
                        (protected, expected_hash, job_id),
                    )

            for row in webhook_rows:
                if row[5] is not None:
                    continue
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
                    await db.execute(
                        f"UPDATE webhook_deliveries SET {assignments} "
                        "WHERE delivery_id = ?",
                        (*updates.values(), delivery_id),
                    )

    if legacy_migrated:
        async with get_db("jobs.db") as db:
            cursor = await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await cursor.fetchall()
            await cursor.close()
