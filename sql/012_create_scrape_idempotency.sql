CREATE TABLE IF NOT EXISTS scrape_idempotency (
    caller_scope           TEXT NOT NULL,
    key_digest             TEXT NOT NULL,
    request_hash           TEXT NOT NULL,
    job_id                 TEXT NOT NULL,
    run_id                 TEXT NOT NULL,
    response_mode          TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    PRIMARY KEY (caller_scope, key_digest),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scrape_idempotency_expires
    ON scrape_idempotency (expires_at, caller_scope, key_digest);
