# --- Builder stage: export Poetry deps to requirements.txt ---
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir poetry==1.8.5

WORKDIR /build
COPY pyproject.toml poetry.lock ./
RUN poetry export -f requirements.txt --without-hashes -o requirements.txt

# --- Final stage: lean runtime image ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build deps, Python deps, then purge build deps in one layer.
COPY --from=builder /build/requirements.txt .
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libdbus-glib-1-2 \
        libgtk-3-0t64 \
        libxml2-dev \
        libxslt1-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y build-essential libxml2-dev libxslt1-dev \
    && apt-get autoremove -y \
    && python -m playwright install --with-deps chromium \
    && python -m camoufox fetch \
    && rm -rf /var/lib/apt/lists/*

# Copy application source.
COPY src/ src/
COPY sql/ sql/
COPY pyproject.toml README.md ./

# Install the project itself (editable not needed in container).
RUN pip install --no-cache-dir --no-deps .

# Place SQL migrations where database.py expects them (../../sql relative to the package).
RUN cp -r sql/ "$(python -c 'import scrapeyard, pathlib; print(pathlib.Path(scrapeyard.__file__).resolve().parent / "../../sql")')"

# Create the /data directory for the volume mount.
RUN mkdir -p /data/db /data/results /data/adaptive /data/logs

EXPOSE 8420

CMD ["uvicorn", "scrapeyard.main:app", "--host", "0.0.0.0", "--port", "8420"]
