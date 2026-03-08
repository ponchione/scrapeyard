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
