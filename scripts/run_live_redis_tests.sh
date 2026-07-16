#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "${SCRAPEYARD_API_CREDENTIALS:-}" ]]; then
  export SCRAPEYARD_API_CREDENTIALS='{"live-redis":{"secret":"live-redis-test-key-0000","scopes":["submit","read","schedule-admin","delete"]},"live-redis-health":{"secret":"live-redis-health-key-0000","scopes":["health-detail"]}}'
  export SCRAPEYARD_HEALTH_PROBE_API_KEY="live-redis-health-key-0000"
fi
if [[ -z "${SCRAPEYARD_ENCRYPTION_KEYS:-}" ]]; then
  export SCRAPEYARD_ENCRYPTION_KEYS='{"live-test-v1":"MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="}'
fi
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID="${SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID:-live-test-v1}"
export SCRAPEYARD_TEST_REDIS_PORT="${SCRAPEYARD_TEST_REDIS_PORT:-56379}"
# The base Compose network is intentionally stable for production. Give this
# disposable test project its own subnet so it can run alongside a deployed
# stack without Docker rejecting the overlapping IPAM pool.
export SCRAPEYARD_BACKEND_SUBNET="${SCRAPEYARD_TEST_BACKEND_SUBNET:-172.29.13.0/24}"
export SCRAPEYARD_BACKEND_IP_RANGE="${SCRAPEYARD_TEST_BACKEND_IP_RANGE:-172.29.13.0/25}"
export SCRAPEYARD_EGRESS_POLICY_PROBE_HOST="${SCRAPEYARD_TEST_EGRESS_POLICY_PROBE_HOST:-172.29.13.248}"
export SCRAPEYARD_REDIS_DESTINATION="${SCRAPEYARD_TEST_REDIS_DESTINATION:-172.29.13.249}"
export SCRAPEYARD_EGRESS_SOURCE="${SCRAPEYARD_TEST_EGRESS_SOURCE:-172.29.13.250}"

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

env \
  -u SCRAPEYARD_BACKEND_SUBNET \
  -u SCRAPEYARD_BACKEND_IP_RANGE \
  -u SCRAPEYARD_EGRESS_POLICY_PROBE_HOST \
  -u SCRAPEYARD_REDIS_DESTINATION \
  -u SCRAPEYARD_EGRESS_SOURCE \
  SCRAPEYARD_REDIS_DSN="redis://127.0.0.1:${SCRAPEYARD_TEST_REDIS_PORT}/15" \
  poetry run pytest -W error --no-cov -m live_redis tests/live_redis -q
