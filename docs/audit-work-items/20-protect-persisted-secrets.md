# Protect Persisted Secrets

Priority: P1

## Problem

Raw YAML job configs and webhook outbox headers are stored in plaintext SQLite. API serialization and logs redact secrets, but host access, database backups, and support copies can expose proxy credentials, webhook authorization headers, browser headers, and URL tokens.

Relevant code:

- `src/scrapeyard/storage/job_store.py`
- `src/scrapeyard/storage/webhook_outbox.py`
- `src/scrapeyard/api/serializers.py`
- `src/scrapeyard/engine/url_guard.py`
- `docs/DEPLOYMENT.md`

## Required Outcome

Long-lived persisted state and backups must not contain reusable secret material unless protected by an explicit encryption/key-management design.

## Implementation Scope

1. Inventory all secret-bearing config fields and persisted/logged/result paths.
2. Prefer secret references resolved at execution time over embedding values in YAML.
3. Encrypt any secrets that must remain in SQLite using a versioned envelope and deployment-provided key.
4. Ensure webhook retries can still resolve required credentials after restart and rotation.
5. Define backup, restore, rotation, and lost-key behavior.
6. Add retention cleanup for secret-bearing outbox rows.
7. Migrate or explicitly handle existing plaintext rows.

## Acceptance Criteria

- New database rows and backups do not expose plaintext proxy/webhook/browser credentials.
- API responses and logs remain redacted.
- Key rotation does not invalidate pending webhook deliveries unexpectedly.
- Restored data can be decrypted only with the intended deployment key material.
- Migration failures do not destroy existing configs.

## Verification

- Add storage tests that inspect raw SQLite values for plaintext sentinel secrets.
- Add rotation, restart, backup/restore, and missing-key tests.
- Run serializers, URL-redaction, webhook, storage, and full suites.

## Non-Goals

- Encryption does not protect against a fully compromised running process with access to the decryption key.
