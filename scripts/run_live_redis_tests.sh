#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export SCRAPEYARD_API_KEYS="${SCRAPEYARD_API_KEYS:-live-redis-test-key}"
export SCRAPEYARD_TEST_REDIS_PORT="${SCRAPEYARD_TEST_REDIS_PORT:-56379}"

case "$SCRAPEYARD_TEST_REDIS_PORT" in
  ''|*[!0-9]*)
    echo "SCRAPEYARD_TEST_REDIS_PORT must be an integer" >&2
    exit 2
    ;;
esac
if ((SCRAPEYARD_TEST_REDIS_PORT < 1 || SCRAPEYARD_TEST_REDIS_PORT > 65535)); then
  echo "SCRAPEYARD_TEST_REDIS_PORT must be between 1 and 65535" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required to run the live Redis tests" >&2
  exit 2
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is required to run the live Redis tests" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "The Docker daemon is unavailable" >&2
  exit 2
fi
if (exec 3<>"/dev/tcp/127.0.0.1/$SCRAPEYARD_TEST_REDIS_PORT") 2>/dev/null; then
  echo "Port $SCRAPEYARD_TEST_REDIS_PORT is already in use" >&2
  exit 2
fi

COMPOSE_ARGS=(-p scrapeyard-live-redis-tests -f docker-compose.yml -f docker-compose.test.yml)

cleanup() {
  docker compose "${COMPOSE_ARGS[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
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

SCRAPEYARD_REDIS_DSN="redis://127.0.0.1:${SCRAPEYARD_TEST_REDIS_PORT}/15" \
poetry run pytest --no-cov -m live_redis tests/live_redis -q
