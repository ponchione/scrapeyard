# Persisted Secret Protection

## Inventory and boundaries

Reusable secrets can enter a job through proxy userinfo, target URL query
tokens, browser `extra_headers`, browser/CDP URLs, webhook URL query tokens,
and webhook headers. The raw YAML historically carried all of those into
`jobs.config_yaml`. A terminal webhook intent then copied the resolved webhook
URL, headers, and payload into `webhook_deliveries`; retry error text can also
contain a URL. Those SQLite job/snapshot/outbox fields are the durable
secret-bearing boundary. Redis receives only the immutable `job_id`, `run_id`,
trigger, and non-secret delivery metadata; submitted YAML is never serialized
into an arq payload. Redis AOF files, backups, and the configured seven-day
payload lifetime therefore do not create another plaintext configuration copy.
The config `project` and `name` must remain literal and cannot be derived from
a secret reference.

New writes encrypt the complete parent YAML, an immutable
`queued_run_snapshots` YAML copy before every delivery enters Redis, the YAML
snapshot owned by every claimed or failed `job_runs` row, and the complete outbox URL, headers, payload,
and non-null retry error. The run snapshot is bound to its run ID with distinct
associated data, allowing terminal webhook repair after a scheduled parent is
updated without exposing the old configuration. Encrypting complete values
avoids relying on an incomplete sensitive-key list. During execution, resolved references also
form a run-scoped redaction set. Result/API/error/webhook serializers remove
raw and URL-encoded occurrences from the complete diagnostic payload,
including extracted records. Diagnostic URLs preserve query keys but redact
every query value and the complete fragment. Selector failures retain
operation, field, selector type, exception type, and a SHA-256 query
fingerprint, never the raw query or exception message. Result records still
contain extracted site data by design and therefore require the same access,
retention, and backup controls as any scrape output. Logs record identifiers
and exception types rather than config/header values.

Browser `extra_headers` are target-origin credentials: they are retained only
when scheme, hostname, and effective port exactly match the configured target.
Browser interception removes them from cross-origin requests, redirects,
subdomains, and scheme changes before the request is continued.

Prefer deployment references in submitted YAML:

```yaml
proxy:
  url: ${SCRAPEYARD_SECRET_PROXY_URL}
webhook:
  url: https://hooks.example.com/events
  headers:
    Authorization: ${SCRAPEYARD_SECRET_WEBHOOK_AUTH}
```

Only names beginning `SCRAPEYARD_SECRET_` are eligible for resolution. Configure
`SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST` as a JSON map from each project to the
exact names it may use. The reserved `*` project adds explicitly shared names:

```bash
export SCRAPEYARD_SECRET_REFERENCE_ALLOWLIST='{"catalog":["SCRAPEYARD_SECRET_PROXY_URL","SCRAPEYARD_SECRET_WEBHOOK_AUTH"],"*":["SCRAPEYARD_SECRET_SHARED_CA"]}'
```

The default empty policy denies every secret reference. The config `project`
and `name` must remain literal, so neither authorization policy nor plaintext
storage paths can be derived from a secret reference.
Resolution happens each time YAML is validated/executed; the reference remains
in persisted YAML. A missing, unauthorized, empty, or shorter-than-eight-character
secret fails closed because deployment-secret output protection uses exact
substring redaction. Persisted results include a non-sensitive
`result_redaction.deployment_secret_matches` count whenever extracted result
values or group keys were changed by that protection. A
terminal webhook stores an encrypted copy of the resolved retry
request so an environment-secret rotation does not silently change an already
accepted delivery.

## Encryption envelope and key configuration

Durable values use AES-256-GCM with a fresh 96-bit nonce, field/row-specific
associated data, and this text envelope:

```text
syenc:v1:<key-id>:<urlsafe-base64(nonce+ciphertext+tag)>
```

Configure a JSON keyring and one active key. Each value is URL-safe base64 for
exactly 32 random bytes:

```bash
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID=prod-2026-07
export SCRAPEYARD_ENCRYPTION_KEYS='{"prod-2026-07":"<base64-32-byte-key>"}'
```

Both settings are required once jobs or unsanitized webhook rows exist. The
service fails closed on a missing key, unknown envelope key ID, malformed
envelope, or authentication failure. Errors never include ciphertext or
plaintext. Encryption protects offline databases/backups; it cannot protect
against a compromised running process that holds the key.

## Existing plaintext migration

After schema migrations and before workers/scheduler start, one atomic
`jobs.db` transaction:

1. authenticates every existing envelope;
2. encrypts every legacy job config, accepted queued snapshot, and unsanitized outbox request;
3. backfills a legacy run snapshot only when its hash proves the current parent
   is the exact configuration that run accepted;
4. writes a SHA-256 config fingerprint used only for compare-and-set behavior;
5. rotates parent, queued-snapshot, run-snapshot, and outbox envelopes to the configured active key.

Already-current envelopes are still authenticated and their config hashes are
verified, but their rows are not rewritten. A no-change restart therefore
performs no `UPDATE` statements; only plaintext, old-key envelopes, or stale
config hashes acquire the write lock and change rows.

Any failure rolls back the whole mutation, preserving the original rows. For
legacy plaintext, SQLite `secure_delete` is enabled and the WAL is checkpointed
and truncated after commit so a subsequent backup does not retain reusable
sentinels in old WAL cells. Make an offline backup before first migration and
test the restored copy with the intended key material.

## Rotation, backup, restore, and key loss

Rotation procedure:

1. Add the new key while retaining every old envelope key; set the new ID active.
2. Restart one instance. Startup atomically re-encrypts job and pending webhook
   state to the new active key.
3. Exercise job reads and pending webhook delivery, then back up the databases.
4. Remove retired keys and restart again. Missing old envelopes now fail closed
   instead of being skipped.

Pending webhook retries remain decryptable throughout the overlap and are
rewritten before an old key is removed. Terminal rows continue using the
existing delivered/failed retention cleanup, which removes request state and
leaves a non-secret tombstone.

Back up encryption keys separately from encrypted databases, with access and
retention at least as strict as the API credentials. A restored database opens
only with the key IDs embedded in its envelopes. Loss of all matching keys is
not recoverable: preserve the encrypted files, restore key material from the
deployment secret backup, and do not attempt destructive repair or plaintext
substitution.
