# --- Builder stage: export Poetry deps to requirements.txt ---
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir poetry==1.8.5

WORKDIR /build
COPY pyproject.toml poetry.lock ./
RUN poetry export -f requirements.txt --without-hashes -o requirements.txt

# --- Final stage: lean runtime image ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH="/ms-playwright" \
    XDG_CACHE_HOME="/var/cache/scrapeyard" \
    CHROME_DEVEL_SANDBOX="/ms-playwright/chromium-1169/chrome-linux/chrome_sandbox"

WORKDIR /app

# Install build deps, Python deps, browser runtimes, then purge build deps.
COPY --from=builder /build/requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        libdbus-glib-1-2 \
        libgtk-3-0t64 \
        libxml2-dev \
        libxslt1-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --home-dir /home/scrapeyard --shell /bin/bash scrapeyard \
    && mkdir -p /data/db /data/results /data/adaptive /data/logs "$PLAYWRIGHT_BROWSERS_PATH" "$XDG_CACHE_HOME" \
    # Install all browser runtimes advertised by Scrapeyard into shared paths:
    # stock Playwright Chromium for standard dynamic fetches, rebrowser
    # Chromium for dynamic fetches with browser.stealth=true, and Camoufox
    # assets for stealthy. Dynamic stealth needs the rebrowser sandbox helper
    # to remain root-owned + setuid, while docker-compose relaxes seccomp so
    # Chromium can create the namespaces it expects.
    && python -m playwright install --with-deps chromium \
    && python -m rebrowser_playwright install chromium \
    && python -m camoufox fetch \
    && chown -R scrapeyard:scrapeyard /app /data "$XDG_CACHE_HOME" \
    && chown root:root /ms-playwright/chromium-1169/chrome-linux/chrome_sandbox \
    && chmod 4755 /ms-playwright/chromium-1169/chrome-linux/chrome_sandbox \
    && apt-get purge -y build-essential libxml2-dev libxslt1-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Copy application source.
COPY src/ src/
COPY sql/ sql/
COPY pyproject.toml README.md ./

# Install the project itself (editable not needed in container).
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8420/health >/dev/null || exit 1

CMD ["sh", "-lc", "mkdir -p /data/db /data/results /data/adaptive /data/logs \"$XDG_CACHE_HOME\" && chown -R scrapeyard:scrapeyard /app /data \"$XDG_CACHE_HOME\" && chown root:root \"$CHROME_DEVEL_SANDBOX\" && chmod 4755 \"$CHROME_DEVEL_SANDBOX\" && exec su scrapeyard -s /bin/sh -c 'uvicorn scrapeyard.main:app --host 0.0.0.0 --port 8420'"]
