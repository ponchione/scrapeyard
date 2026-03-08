# Dockerfile and Docker Compose Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Create Dockerfile, docker-compose.yml, and .dockerignore so the Scrapeyard service can be built and run as a Docker container on port 8420.

**Architecture:** Multi-stage Dockerfile (Poetry export in builder, pip install in final). Single docker-compose service with a named volume at `/data` and all `SCRAPEYARD_*` env vars listed explicitly. `.dockerignore` keeps the build context lean.

**Tech Stack:** Docker, Docker Compose, Poetry, uvicorn

---

### Task 1: Create .dockerignore

**Files:**
- Create: `.dockerignore`

**Step 1: Write the file**

Create `.dockerignore`:

```
__pycache__
*.pyc
*.pyo
.git
.gitignore
tests/
.ruff_cache
.pytest_cache
.venv
.mypy_cache
*.egg-info
dist/
docs/
work-orders/
.claude/
README.md
```

**Step 2: Verify the file looks right**

Run: `cat .dockerignore`
Expected: Contents as above.

**Step 3: Commit**

```bash
git add .dockerignore
git commit -m "chore: add .dockerignore to exclude dev artifacts from build context"
```

---

### Task 2: Create Dockerfile

**Files:**
- Create: `Dockerfile`

**Step 1: Write the Dockerfile**

Create `Dockerfile`:

```dockerfile
# --- Builder stage: export Poetry deps to requirements.txt ---
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir poetry==1.8.5

WORKDIR /build
COPY pyproject.toml poetry.lock ./
RUN poetry export -f requirements.txt --without-hashes -o requirements.txt

# --- Final stage: lean runtime image ---
FROM python:3.12-slim

# System deps for Scrapling (lxml, etc.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps from exported requirements.
COPY --from=builder /build/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY src/ src/
COPY pyproject.toml .

# Install the project itself (editable not needed in container).
RUN pip install --no-cache-dir --no-deps .

# Create the /data directory for the volume mount.
RUN mkdir -p /data/db /data/results /data/adaptive /data/logs

EXPOSE 8420

CMD ["uvicorn", "scrapeyard.main:app", "--host", "0.0.0.0", "--port", "8420"]
```

**Step 2: Run docker build**

Run: `docker build -t scrapeyard:test .`
Expected: Build completes successfully.

**Step 3: Commit**

```bash
git add Dockerfile
git commit -m "feat: add multi-stage Dockerfile for Scrapeyard service"
```

---

### Task 3: Create docker-compose.yml

**Files:**
- Create: `docker-compose.yml`

**Step 1: Write the file**

Create `docker-compose.yml`:

```yaml
services:
  scrapeyard:
    build: .
    ports:
      - "8420:8420"
    volumes:
      - scrapeyard-data:/data
    environment:
      # Workers
      SCRAPEYARD_WORKERS_MAX_CONCURRENT: "4"
      SCRAPEYARD_WORKERS_MAX_BROWSERS: "2"
      SCRAPEYARD_WORKERS_MEMORY_LIMIT_MB: "4096"
      # Scheduler
      SCRAPEYARD_SCHEDULER_JITTER_MAX_SECONDS: "120"
      # Storage
      SCRAPEYARD_STORAGE_RETENTION_DAYS: "30"
      SCRAPEYARD_STORAGE_RESULTS_DIR: "/data/results"
      SCRAPEYARD_STORAGE_MAX_RESULTS_PER_JOB: "100"
      # Data directories
      SCRAPEYARD_DB_DIR: "/data/db"
      SCRAPEYARD_ADAPTIVE_DIR: "/data/adaptive"
      SCRAPEYARD_LOG_DIR: "/data/logs"
      # Circuit breaker
      SCRAPEYARD_CIRCUIT_BREAKER_MAX_FAILURES: "3"
      SCRAPEYARD_CIRCUIT_BREAKER_COOLDOWN_SECONDS: "300"
    restart: unless-stopped

volumes:
  scrapeyard-data:
```

**Step 2: Run docker compose build**

Run: `docker compose build`
Expected: Build completes successfully.

**Step 3: Run docker compose up and verify health**

Run: `docker compose up -d && sleep 3 && curl -s http://localhost:8420/health && docker compose down`
Expected: `{"status":"ok",...}` with HTTP 200.

**Step 4: Commit**

```bash
git add docker-compose.yml
git commit -m "feat: add docker-compose.yml with named volume and all env vars"
```

---

### Task 4: Final verification

**Files:** None (verification only)

**Step 1: Full build from clean state**

Run: `docker compose build --no-cache`
Expected: Build completes successfully.

**Step 2: Start and health check**

Run: `docker compose up -d && sleep 3 && curl -sf http://localhost:8420/health; echo; docker compose down`
Expected: JSON response with `"status":"ok"` and exit code 0 from curl.

**Step 3: Verify existing tests still pass**

Run: `source .venv/bin/activate && pytest tests/ -v`
Expected: All PASS (Docker files don't affect Python tests).

**Step 4: Commit if any fixes were needed**

```bash
git add -u
git commit -m "chore: fixes from Docker verification"
```
