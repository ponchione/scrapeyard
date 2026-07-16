#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: ./scripts/run_container_browser_smoke.sh [--no-cache] [--image IMMUTABLE_REF] [--run-mode cold|warm]

Exercise basic, dynamic, dynamic-stealth, and Camoufox fetchers against an
isolated local fixture. ``--image`` consumes a prebuilt candidate without a
hidden rebuild. Use --no-cache only when producing a non-release CI image.

Configuration:
  SCRAPEYARD_SMOKE_PROJECT        Compose project (default: scrapeyard-container-smoke)
  SCRAPEYARD_SMOKE_API_PORT       loopback API port (default: 18420)
  SCRAPEYARD_SMOKE_FIXTURE_PORT   loopback fixture-control port (default: 18080)
  SCRAPEYARD_SMOKE_BUILD_TIMEOUT  build timeout seconds (default: 2400)
  SCRAPEYARD_SMOKE_START_TIMEOUT  health timeout seconds (default: 180)
  SCRAPEYARD_SMOKE_JOB_TIMEOUT    per-job timeout seconds (default: 120)
  SCRAPEYARD_SMOKE_DIAGNOSTICS_DIR focused artifact directory
EOF
}

NO_CACHE=0
IMAGE_REF=""
RUN_MODE="cold"
while (($#)); do
  case "$1" in
    --no-cache) NO_CACHE=1; shift ;;
    --image) (($# >= 2)) || { echo "--image requires a reference" >&2; exit 2; }; IMAGE_REF=$2; shift 2 ;;
    --run-mode) (($# >= 2)) || { echo "--run-mode requires cold or warm" >&2; exit 2; }; RUN_MODE=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$RUN_MODE" == "cold" || "$RUN_MODE" == "warm" ]] || {
  echo "--run-mode must be cold or warm" >&2
  exit 2
}
[[ -z "$IMAGE_REF" || "$NO_CACHE" == 0 ]] || {
  echo "--no-cache cannot be combined with --image" >&2
  exit 2
}

PROJECT="${SCRAPEYARD_SMOKE_PROJECT:-scrapeyard-container-smoke}"
API_PORT="${SCRAPEYARD_SMOKE_API_PORT:-18420}"
FIXTURE_PORT="${SCRAPEYARD_SMOKE_FIXTURE_PORT:-18080}"
BUILD_TIMEOUT="${SCRAPEYARD_SMOKE_BUILD_TIMEOUT:-2400}"
START_TIMEOUT="${SCRAPEYARD_SMOKE_START_TIMEOUT:-180}"
JOB_TIMEOUT="${SCRAPEYARD_SMOKE_JOB_TIMEOUT:-120}"
DIAGNOSTICS_DIR="${SCRAPEYARD_SMOKE_DIAGNOSTICS_DIR:-$ROOT_DIR/artifacts/container-browser-smoke}"
export SCRAPEYARD_BACKEND_SUBNET="${SCRAPEYARD_SMOKE_BACKEND_SUBNET:-172.29.14.0/24}"
export SCRAPEYARD_BACKEND_IP_RANGE="${SCRAPEYARD_SMOKE_BACKEND_IP_RANGE:-172.29.14.0/25}"
export SCRAPEYARD_EGRESS_SOURCE="${SCRAPEYARD_SMOKE_APP_ADDRESS:-172.29.14.250}"
export SCRAPEYARD_REDIS_DESTINATION="${SCRAPEYARD_SMOKE_REDIS_ADDRESS:-172.29.14.249}"
export SCRAPEYARD_EGRESS_POLICY_PROBE_HOST="${SCRAPEYARD_SMOKE_EGRESS_PROBE_ADDRESS:-172.29.14.248}"
COMPOSE_ARGS=(-p "$PROJECT" -f docker-compose.yml -f docker-compose.smoke.yml)
TOTAL_STARTED_AT=$SECONDS
CREDENTIAL_DIR=""
CLEANUP_COMPLETE=0
EGRESS_POLICY_ACTIVE=0
APPARMOR_PROFILE_ACTIVE=0
POLICY_MODE=""
SCRAPEYARD_EGRESS_INTERFACE=""
SCRAPEYARD_EGRESS_POLICY_ID="smoke"
export SCRAPEYARD_EGRESS_INTERFACE SCRAPEYARD_EGRESS_POLICY_ID

fail() {
  echo "container/browser smoke: $*" >&2
  exit 2
}

validate_integer() {
  local label=$1 value=$2 minimum=$3 maximum=$4
  [[ "$value" =~ ^[0-9]+$ ]] || fail "$label must be an integer"
  ((value >= minimum && value <= maximum)) || fail "$label must be between $minimum and $maximum"
}

for command in docker python3 curl timeout mktemp od; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
docker info >/dev/null 2>&1 || fail "the Docker daemon is unavailable"
[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || \
  fail "SCRAPEYARD_SMOKE_PROJECT must be a lowercase Compose-safe name (max 63 characters)"
validate_integer SCRAPEYARD_SMOKE_API_PORT "$API_PORT" 1 65535
validate_integer SCRAPEYARD_SMOKE_FIXTURE_PORT "$FIXTURE_PORT" 1 65535
validate_integer SCRAPEYARD_SMOKE_BUILD_TIMEOUT "$BUILD_TIMEOUT" 60 7200
validate_integer SCRAPEYARD_SMOKE_START_TIMEOUT "$START_TIMEOUT" 10 600
validate_integer SCRAPEYARD_SMOKE_JOB_TIMEOUT "$JOB_TIMEOUT" 10 600
[[ "$API_PORT" != "$FIXTURE_PORT" ]] || fail "API and fixture ports must differ"

port_is_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}
port_is_open "$API_PORT" && fail "port $API_PORT is already in use"
port_is_open "$FIXTURE_PORT" && fail "port $FIXTURE_PORT is already in use"

if [[ -e "$DIAGNOSTICS_DIR" && ! -d "$DIAGNOSTICS_DIR" ]]; then
  fail "diagnostics path exists and is not a directory: $DIAGNOSTICS_DIR"
fi
rm -rf -- "$DIAGNOSTICS_DIR"
mkdir -p "$DIAGNOSTICS_DIR"

CREDENTIAL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scrapeyard-smoke-credentials.XXXXXX")"
chmod 700 "$CREDENTIAL_DIR"
API_KEY_FILE="$CREDENTIAL_DIR/api-key"
printf 'item14-smoke-%s\n' "$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')" > "$API_KEY_FILE"
chmod 600 "$API_KEY_FILE"
API_KEY="$(<"$API_KEY_FILE")"
HEALTH_KEY="item14-health-$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
printf -v SCRAPEYARD_API_CREDENTIALS \
  '{"item14-smoke":{"secret":"%s","scopes":["submit","read","schedule-admin","delete"]},"item14-health":{"secret":"%s","scopes":["health-detail"]}}' \
  "$API_KEY" "$HEALTH_KEY"
export SCRAPEYARD_API_CREDENTIALS SCRAPEYARD_HEALTH_PROBE_API_KEY="$HEALTH_KEY"
ENCRYPTION_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
printf -v SCRAPEYARD_ENCRYPTION_KEYS '{"item20-smoke":"%s"}' "$ENCRYPTION_KEY"
export SCRAPEYARD_ENCRYPTION_KEYS
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID="item20-smoke"
export SCRAPEYARD_SMOKE_API_PORT="$API_PORT"
export SCRAPEYARD_SMOKE_FIXTURE_PORT="$FIXTURE_PORT"
export SCRAPEYARD_BIND_ADDRESS="127.0.0.1"
export SCRAPEYARD_PORT="$API_PORT"

compose() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

run_egress_policy() {
  local action=$1
  case "$POLICY_MODE" in
    root) security/install-docker-egress-policy.sh "$action" ;;
    sudo) sudo -n env \
      SCRAPEYARD_EGRESS_INTERFACE="$SCRAPEYARD_EGRESS_INTERFACE" \
      SCRAPEYARD_EGRESS_POLICY_ID="$SCRAPEYARD_EGRESS_POLICY_ID" \
      security/install-docker-egress-policy.sh "$action" ;;
    container)
      docker run --rm --network host --cap-add NET_ADMIN --cap-add NET_RAW \
        -e SCRAPEYARD_EGRESS_INTERFACE -e SCRAPEYARD_EGRESS_POLICY_ID \
        -e SCRAPEYARD_EGRESS_SOURCE -e SCRAPEYARD_REDIS_DESTINATION \
        -e SCRAPEYARD_EGRESS_POLICY_PROBE_HOST \
        -v "$ROOT_DIR:/repo:ro" \
        ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90 \
        sh -c 'apt-get update >/dev/null && apt-get install -y --no-install-recommends iptables python3 >/dev/null && /repo/security/install-docker-egress-policy.sh "$1"' \
        _ "$action"
      ;;
    *) fail "egress policy runner was not selected" ;;
  esac
}

run_apparmor_profile() {
  local action=$1
  case "$POLICY_MODE" in
    root) security/install-chromium-apparmor-profile.sh "$action" ;;
    sudo) sudo -n security/install-chromium-apparmor-profile.sh "$action" ;;
    container)
      docker run --rm --privileged --security-opt apparmor=unconfined \
        -v /sys/kernel/security:/sys/kernel/security \
        -v "$ROOT_DIR:/repo:ro" \
        ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90 \
        sh -c 'apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends apparmor >/dev/null && /repo/security/install-chromium-apparmor-profile.sh "$1"' \
        _ "$action"
      ;;
    *) fail "privileged policy runner was not selected" ;;
  esac
}

apparmor_profile_loaded() {
  docker run --rm --privileged --security-opt apparmor=unconfined \
    -v /sys/kernel/security:/sys/kernel/security:ro \
    ubuntu:24.04@sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90 \
    grep -q '^scrapeyard-chromium ' /sys/kernel/security/apparmor/profiles
}

capture_diagnostics() {
  set +e
  mkdir -p "$DIAGNOSTICS_DIR"
  compose ps -a > "$DIAGNOSTICS_DIR/compose-ps.txt" 2>&1
  compose logs --no-color --timestamps > "$DIAGNOSTICS_DIR/service-logs.txt" 2>&1
  curl --silent --show-error --max-time 5 \
    "http://127.0.0.1:$API_PORT/health" > "$DIAGNOSTICS_DIR/health-at-failure.json" 2>&1
  curl --silent --show-error --max-time 5 \
    "http://127.0.0.1:$FIXTURE_PORT/__stats" > "$DIAGNOSTICS_DIR/fixture-stats-at-failure.json" 2>&1
  if compose ps -q scrapeyard >/dev/null 2>&1; then
    compose cp scrapeyard:/data/results/item14-smoke \
      "$DIAGNOSTICS_DIR/result-artifacts" >/dev/null 2>&1
  fi
  set -e
}

cleanup_resources() {
  set +e
  if ((EGRESS_POLICY_ACTIVE == 1)); then
    run_egress_policy remove >/dev/null 2>&1
    EGRESS_POLICY_ACTIVE=0
  fi
  timeout 90 docker compose "${COMPOSE_ARGS[@]}" down -v --remove-orphans >/dev/null 2>&1
  if ((APPARMOR_PROFILE_ACTIVE == 1)); then
    run_apparmor_profile remove >/dev/null 2>&1
    APPARMOR_PROFILE_ACTIVE=0
  fi
  set -e
  CLEANUP_COMPLETE=1
}

on_exit() {
  local status=$?
  trap - EXIT INT TERM HUP
  if ((status != 0)); then
    echo "Smoke lane failed; retaining focused diagnostics in $DIAGNOSTICS_DIR" >&2
    capture_diagnostics
  fi
  if ((CLEANUP_COMPLETE == 0)); then
    cleanup_resources
  fi
  if [[ -n "$CREDENTIAL_DIR" ]]; then
    rm -rf -- "$CREDENTIAL_DIR"
  fi
  unset SCRAPEYARD_API_CREDENTIALS SCRAPEYARD_HEALTH_PROBE_API_KEY SCRAPEYARD_IMAGE
  unset SCRAPEYARD_ENCRYPTION_KEYS SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID
  unset SCRAPEYARD_BIND_ADDRESS SCRAPEYARD_PORT
  unset SCRAPEYARD_BACKEND_SUBNET SCRAPEYARD_BACKEND_IP_RANGE
  unset SCRAPEYARD_EGRESS_SOURCE SCRAPEYARD_REDIS_DESTINATION
  exit "$status"
}

trap on_exit EXIT
trap 'exit 130' INT TERM HUP

assert_no_project_resources() {
  local kind ids
  for kind in container network volume; do
    case "$kind" in
      container) ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ;;
      network) ids="$(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT")" ;;
      volume) ids="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")" ;;
    esac
    [[ -z "$ids" ]] || fail "cleanup left Item 14 $kind resources: $ids"
  done
}

assert_no_project_resources

BUILD_STARTED_AT=$SECONDS
if [[ -n "$IMAGE_REF" ]]; then
  docker image inspect "$IMAGE_REF" >/dev/null 2>&1 || fail "candidate image is unavailable: $IMAGE_REF"
  export SCRAPEYARD_IMAGE="$IMAGE_REF"
  BUILD_DURATION=0
else
  export SCRAPEYARD_APP_VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
  export SCRAPEYARD_SOURCE_REVISION="$(git rev-parse HEAD)"
  export SCRAPEYARD_BUILD_CREATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  export SCRAPEYARD_IMAGE="$PROJECT-scrapeyard:candidate"
  BUILD_ARGS=(build scrapeyard)
  if ((NO_CACHE == 1)); then
    BUILD_ARGS=(build --no-cache scrapeyard)
  fi
  echo "Building the production Dockerfile (no_cache=$NO_CACHE)..."
  timeout "$BUILD_TIMEOUT" docker compose "${COMPOSE_ARGS[@]}" "${BUILD_ARGS[@]}"
  BUILD_DURATION=$((SECONDS - BUILD_STARTED_AT))
fi

IMAGE_ID="$(docker image inspect --format '{{.Id}}' "$SCRAPEYARD_IMAGE")"
IMAGE_SIZE="$(docker image inspect --format '{{.Size}}' "$IMAGE_ID")"
while IFS='=' read -r label expected; do
  actual="$(docker image inspect --format "{{index .Config.Labels \"$label\"}}" "$IMAGE_ID")"
  [[ "$actual" == "$expected" ]] || fail "image label $label expected $expected, got $actual"
done <<'EOF'
org.scrapeyard.browser.scrapling.version=0.4.11
org.scrapeyard.browser.playwright.version=1.61.0
org.scrapeyard.browser.patchright.version=1.61.2
org.scrapeyard.browser.chromium.version=149.0.7827.55
org.scrapeyard.browser.chromium.revision=1228
org.scrapeyard.browser.camoufox-package.version=0.6.0
org.scrapeyard.browser.camoufox.version=150.0.2
org.scrapeyard.browser.camoufox.release=beta.25
org.scrapeyard.browser.camoufox.sha256=b146b98b0c2c41023716feef36451f319a534309f72c54584a4b0b88670f510b
org.scrapeyard.browser.ubo.version=1.72.2
org.scrapeyard.browser.ubo.sha256=40c315b0da7871868155ecfae7a50a58dfa0920aebd865e008214986f1b7c578
org.opencontainers.image.base.digest=sha256:4fbb8e6a8395de5a7550b33509421a2bafbc0aab6c06ba2cef9ebffbc7092d90
EOF

if ((EUID == 0)); then
  POLICY_MODE=root
elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
  POLICY_MODE=sudo
else
  POLICY_MODE=container
fi
if ! apparmor_profile_loaded; then
  APPARMOR_PROFILE_ACTIVE=1
fi
run_apparmor_profile install

echo "Starting controlled dependencies before the application..."
timeout 120 docker compose "${COMPOSE_ARGS[@]}" up -d --no-build \
  egress-probe redis fixture
NETWORK_ID="$(docker network inspect --format '{{.Id}}' "${PROJECT}_backend")"
[[ "$NETWORK_ID" =~ ^[a-f0-9]{12,}$ ]] || fail "could not resolve the Compose backend bridge"
SCRAPEYARD_EGRESS_INTERFACE="br-${NETWORK_ID:0:12}"
export SCRAPEYARD_EGRESS_INTERFACE

run_egress_policy install
EGRESS_POLICY_ACTIVE=1

echo "Starting Scrapeyard after the connected-IP policy is installed..."
timeout 120 docker compose "${COMPOSE_ARGS[@]}" up -d --no-build scrapeyard peer
HEALTH_DEADLINE=$((SECONDS + START_TIMEOUT))
until curl --silent --fail --max-time 5 -H "X-API-Key: $HEALTH_KEY" \
  "http://127.0.0.1:$API_PORT/health/ready" \
  > "$DIAGNOSTICS_DIR/health.json" 2>/dev/null; do
  ((SECONDS < HEALTH_DEADLINE)) || fail "Scrapeyard did not become healthy within ${START_TIMEOUT}s"
  sleep 2
done

PORT_BINDING="$(docker port "$(compose ps -q scrapeyard)" 8420/tcp)"
[[ "$PORT_BINDING" == "127.0.0.1:$API_PORT" ]] || \
  fail "smoke ingress is not loopback-only: $PORT_BINDING"
compose exec -T peer python -c \
  "import urllib.request; assert urllib.request.urlopen('http://scrapeyard:8420/health/live', timeout=3).status == 200"

if compose exec -T scrapeyard python -c \
  "import urllib.request; urllib.request.urlopen('http://fixture.private.test:8080/protected?case=network-layer', timeout=2).read()"; then
  fail "network policy allowed a private target"
fi
if compose exec -T scrapeyard python -c \
  "import urllib.request; urllib.request.urlopen('http://169.254.169.254/latest/meta-data/', timeout=2).read()"; then
  fail "network policy allowed a metadata target"
fi

python3 tests/smoke/verify_api.py \
  --api-url "http://127.0.0.1:$API_PORT" \
  --fixture-url "http://127.0.0.1:$FIXTURE_PORT" \
  --api-key-file "$API_KEY_FILE" \
  --diagnostics-dir "$DIAGNOSTICS_DIR/payloads" \
  --job-timeout "$JOB_TIMEOUT"

compose exec -T --user scrapeyard scrapeyard python - \
  < tests/smoke/verify_runtime.py > "$DIAGNOSTICS_DIR/runtime-contract.json"

# Retrieve only the focused smoke project's bounded results/screenshots, never
# the SQLite databases, profiles, credentials, or the complete /data tree.
RETRIEVED_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scrapeyard-smoke-results.XXXXXX")"
compose cp scrapeyard:/data/results/item14-smoke "$RETRIEVED_DIR/item14-smoke" >/dev/null
python3 - "$RETRIEVED_DIR/item14-smoke" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
screenshots = list(root.rglob("*.png"))
assert len(screenshots) >= 4, f"expected at least four retrieved screenshots, found {len(screenshots)}"
assert all(path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for path in screenshots)
print(f"retrieved and validated {len(screenshots)} screenshots")
PY
rm -rf -- "$RETRIEVED_DIR"

compose ps -a > "$DIAGNOSTICS_DIR/compose-ps-before-shutdown.txt"
docker stats --no-stream --format '{{json .}}' \
  "$(compose ps -q scrapeyard)" "$(compose ps -q redis)" "$(compose ps -q fixture)" \
  > "$DIAGNOSTICS_DIR/container-stats.jsonl"

echo "Requesting graceful Scrapeyard shutdown..."
timeout 60 docker compose "${COMPOSE_ARGS[@]}" stop -t 40 scrapeyard
SCRAPEYARD_CONTAINER="$(compose ps -aq scrapeyard)"
EXIT_CODE="$(docker inspect --format '{{.State.ExitCode}}' "$SCRAPEYARD_CONTAINER")"
compose logs --no-color scrapeyard > "$DIAGNOSTICS_DIR/scrapeyard-shutdown.log" 2>&1
grep -Eq 'Application shutdown complete|Finished server process' \
  "$DIAGNOSTICS_DIR/scrapeyard-shutdown.log" || fail "Uvicorn graceful-shutdown marker is missing"
[[ "$EXIT_CODE" == "0" || "$EXIT_CODE" == "143" ]] || \
  fail "Scrapeyard exited with unexpected status $EXIT_CODE"

TOTAL_DURATION=$((SECONDS - TOTAL_STARTED_AT))
cat > "$DIAGNOSTICS_DIR/timings.txt" <<EOF
no_cache=$NO_CACHE
run_mode=$RUN_MODE
build_seconds=$BUILD_DURATION
total_seconds=$TOTAL_DURATION
image_id=$IMAGE_ID
image_bytes=$IMAGE_SIZE
EOF

for secret in "$API_KEY" "$HEALTH_KEY" "$ENCRYPTION_KEY"; do
  if grep -R -a -F -- "$secret" "$DIAGNOSTICS_DIR" >/dev/null 2>&1; then
    fail "generated secret appeared in smoke diagnostics"
  fi
done

cleanup_resources
assert_no_project_resources
rm -rf -- "$CREDENTIAL_DIR"
CREDENTIAL_DIR=""
unset SCRAPEYARD_API_CREDENTIALS SCRAPEYARD_HEALTH_PROBE_API_KEY SCRAPEYARD_IMAGE
unset SCRAPEYARD_ENCRYPTION_KEYS SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID
unset SCRAPEYARD_BIND_ADDRESS SCRAPEYARD_PORT
unset SCRAPEYARD_BACKEND_SUBNET SCRAPEYARD_BACKEND_IP_RANGE
unset SCRAPEYARD_EGRESS_SOURCE SCRAPEYARD_REDIS_DESTINATION
trap - EXIT INT TERM HUP

echo "Container/browser smoke passed."
echo "Build: ${BUILD_DURATION}s; total: ${TOTAL_DURATION}s; image: ${IMAGE_SIZE} bytes."
echo "Focused diagnostics: $DIAGNOSTICS_DIR"
