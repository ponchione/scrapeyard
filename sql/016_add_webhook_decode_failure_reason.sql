CREATE TABLE webhook_deliveries_rebuilt (
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
            'non_retryable_failure',
            'decode_failure'
        )
    )
);

INSERT INTO webhook_deliveries_rebuilt (
    delivery_id, job_id, run_id, event, url, headers_json, timeout_seconds,
    payload_json, status, attempts, next_attempt_at, last_attempt_at,
    delivered_at, failed_at, failure_reason, last_error, scrubbed_at,
    created_at, updated_at
)
SELECT
    delivery_id, job_id, run_id, event, url, headers_json, timeout_seconds,
    payload_json, status, attempts, next_attempt_at, last_attempt_at,
    delivered_at, failed_at, failure_reason, last_error, scrubbed_at,
    created_at, updated_at
FROM webhook_deliveries;

DROP TABLE webhook_deliveries;
ALTER TABLE webhook_deliveries_rebuilt RENAME TO webhook_deliveries;

CREATE INDEX idx_webhook_deliveries_due
    ON webhook_deliveries (status, next_attempt_at);
CREATE INDEX idx_webhook_deliveries_created
    ON webhook_deliveries (status, created_at);
CREATE INDEX idx_webhook_deliveries_job_run
    ON webhook_deliveries (job_id, run_id);
CREATE INDEX idx_webhook_deliveries_job_status
    ON webhook_deliveries (job_id, status);
CREATE INDEX idx_webhook_deliveries_terminal_cleanup
    ON webhook_deliveries (status, scrubbed_at, delivered_at, failed_at);
