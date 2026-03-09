# Scrapeyard User Guide

This guide explains how to run and use Scrapeyard as it exists today.

## What Scrapeyard Does
Scrapeyard is a config-driven web scraping API service. You submit YAML configs, and it:
- stores jobs in SQLite,
- executes scrapes,
- stores results on disk,
- exposes results and error records via HTTP.

## Project Layout (Important Paths)
- `src/scrapeyard/main.py`: FastAPI app entrypoint
- `src/scrapeyard/api/routes.py`: API endpoints
- `src/scrapeyard/config/schema.py`: YAML config schema
- `src/scrapeyard/storage/`: SQLite + filesystem storage
- `sql/`: DB migration scripts
- `docker-compose.yml`: containerized runtime

## Prerequisites
- Python 3.10+
- Poetry
- (Optional) Docker + Docker Compose

## Run Locally (Poetry)
```bash
poetry install
poetry run uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420
```

Health check:
```bash
curl -s http://127.0.0.1:8420/health
```

## Run with Docker
```bash
docker compose build
docker compose up -d
curl -s http://127.0.0.1:8420/health
```

Stop:
```bash
docker compose down
```

## Environment Variables
All service-level settings use `SCRAPEYARD_` prefix:
- `WORKERS_MAX_CONCURRENT` (default `4`)
- `WORKERS_MAX_BROWSERS` (default `2`)
- `WORKERS_MEMORY_LIMIT_MB` (default `4096`)
- `SCHEDULER_JITTER_MAX_SECONDS` (default `120`)
- `STORAGE_RETENTION_DAYS` (default `30`)
- `STORAGE_RESULTS_DIR` (default `/data/results`)
- `STORAGE_MAX_RESULTS_PER_JOB` (default `100`)
- `DB_DIR` (default `/data/db`)
- `ADAPTIVE_DIR` (default `/data/adaptive`)
- `LOG_DIR` (default `/data/logs`)
- `CIRCUIT_BREAKER_MAX_FAILURES` (default `3`)
- `CIRCUIT_BREAKER_COOLDOWN_SECONDS` (default `300`)

Example: `SCRAPEYARD_DB_DIR=/tmp/scrapeyard/db`

## YAML Config Basics

### Minimal on-demand config
```yaml
project: demo
name: example-job
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
```

### Multi-target config
```yaml
project: demo
name: multi-job
targets:
  - url: https://example.com
    fetcher: basic
    selectors:
      title: h1
  - url: https://example.org
    fetcher: basic
    selectors:
      heading: h2
```

### Scheduled job config
```yaml
project: demo
name: scheduled-job
schedule:
  cron: "*/15 * * * *"
  enabled: true
target:
  url: https://example.com
  fetcher: basic
  selectors:
    title: h1
```

### Key rules
- Exactly one of `target` or `targets` is required.
- `fetcher` values: `basic`, `stealthy`, `dynamic`.
- `execution.mode`: `auto`, `sync`, `async`.

## API Usage
Important: for YAML endpoints, send `Content-Type: application/x-yaml`.

### 1) On-demand scrape (`POST /scrape`)
```bash
curl -s -X POST http://127.0.0.1:8420/scrape \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @config.yaml
```

Response:
- `200`: sync path completed, returns results inline
- `202`: async path queued, returns `job_id` and `poll_url`

### 2) Poll results (`GET /results/{job_id}`)
```bash
curl -s "http://127.0.0.1:8420/results/<job_id>?latest=true"
```

Response:
- `200`: results available
- `202`: job still queued/running
- `404`: job or results not found

Historical run (if you know a run id):
```bash
curl -s "http://127.0.0.1:8420/results/<job_id>?run_id=<run_id>"
```

### 3) Create scheduled job (`POST /jobs`)
```bash
curl -s -X POST http://127.0.0.1:8420/jobs \
  -H 'Content-Type: application/x-yaml' \
  --data-binary @scheduled.yaml
```

Response: `201` with `job_id`, `project`, `name`, `schedule`.

### 4) List jobs (`GET /jobs`)
```bash
curl -s "http://127.0.0.1:8420/jobs"
curl -s "http://127.0.0.1:8420/jobs?project=demo"
```

### 5) Get job (`GET /jobs/{job_id}`)
```bash
curl -s "http://127.0.0.1:8420/jobs/<job_id>"
```

### 6) Delete job (`DELETE /jobs/{job_id}`)
```bash
curl -s -X DELETE "http://127.0.0.1:8420/jobs/<job_id>"
curl -s -X DELETE "http://127.0.0.1:8420/jobs/<job_id>?delete_results=true"
```

### 7) Query errors (`GET /errors`)
```bash
curl -s "http://127.0.0.1:8420/errors"
curl -s "http://127.0.0.1:8420/errors?project=demo"
curl -s "http://127.0.0.1:8420/errors?job_id=<job_id>"
```

### 8) Health (`GET /health`)
```bash
curl -s http://127.0.0.1:8420/health
```

Returns service status, uptime, worker stats, and per-project summary.

## How Sync vs Async Is Chosen
When `execution.mode: auto`:
- Sync if: exactly one target, no pagination, fetcher is `basic`
- Async otherwise

You can force behavior with `execution.mode: sync` or `execution.mode: async`.

## Result Storage
Results are written to:
- `{results_dir}/{project}/{job_name}/{run_id}/`

Common files:
- `results.json`
- `results.md`
- `raw.html`

Metadata is stored in SQLite `results_meta.db`.

## Maintenance Commands
Run unit tests:
```bash
poetry run pytest tests/unit
```

Run lint:
```bash
poetry run ruff check src tests
```

## Known Technical Debt
Queue backend currently uses a custom in-memory implementation instead of arq-backed flow.
See: `TECH-DEBT.md`.

## Common Issues
- `415 Content-Type must be application/x-yaml`
  - Set `Content-Type: application/x-yaml` on `POST /scrape` and `POST /jobs`.
- `404 No results found`
  - Job may still be running; check `/jobs/{job_id}` or retry `/results/{job_id}`.
- Capacity rejection (`503`)
  - Worker pool limits/memory guard rejected immediate sync processing; retry or lower load.
