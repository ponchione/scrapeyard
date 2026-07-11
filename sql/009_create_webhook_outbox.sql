CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id     TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL,
    run_id          TEXT,
    event           TEXT NOT NULL,
    url             TEXT NOT NULL,
    headers_json    TEXT NOT NULL DEFAULT '{}',
    timeout_seconds REAL NOT NULL DEFAULT 10,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_attempt_at TEXT,
    delivered_at    TEXT,
    failed_at       TEXT,
    failure_reason  TEXT,
    last_error      TEXT,
    scrubbed_at     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    CHECK (status IN ('pending', 'delivered', 'failed')),
    CHECK (
        failure_reason IS NULL OR failure_reason IN (
            'attempt_exhausted',
            'age_exhausted',
            'permanent_http_response',
            'unsafe_url',
            'non_retryable_failure'
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_due
    ON webhook_deliveries (status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_created
    ON webhook_deliveries (status, created_at);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_job_run
    ON webhook_deliveries (job_id, run_id);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_job_status
    ON webhook_deliveries (job_id, status);
