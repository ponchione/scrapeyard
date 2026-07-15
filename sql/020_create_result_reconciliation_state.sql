CREATE TABLE IF NOT EXISTS result_reconciliation_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    metadata_cursor INTEGER NOT NULL DEFAULT 0 CHECK (metadata_cursor >= 0),
    filesystem_project TEXT,
    filesystem_job_name TEXT,
    filesystem_run_id TEXT,
    CHECK (
        (filesystem_project IS NULL
         AND filesystem_job_name IS NULL
         AND filesystem_run_id IS NULL)
        OR
        (filesystem_project IS NOT NULL
         AND filesystem_job_name IS NOT NULL
         AND filesystem_run_id IS NOT NULL)
    )
);

INSERT OR IGNORE INTO result_reconciliation_state (singleton) VALUES (1);
