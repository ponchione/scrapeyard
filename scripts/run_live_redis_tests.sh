#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE_ARGS=(-p scrapeyard-live-redis-tests -f docker-compose.yml -f docker-compose.test.yml)

cleanup() {
  docker compose "${COMPOSE_ARGS[@]}" down >/dev/null 2>&1 || true
}

trap cleanup EXIT

docker compose "${COMPOSE_ARGS[@]}" up -d redis

for _ in {1..30}; do
  if docker compose "${COMPOSE_ARGS[@]}" exec -T redis redis-cli ping >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

docker compose "${COMPOSE_ARGS[@]}" exec -T redis redis-cli ping >/dev/null

SCRAPEYARD_REDIS_DSN="redis://127.0.0.1:56379/15" \
poetry run pytest -m live_redis tests/live_redis -q
