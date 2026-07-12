#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: ./scripts/run_release_qualification.sh [--profile quick|full] [--phase NAME] [--no-build] [--no-cache]

Run Audit Item 15 against the production Dockerfile, real Redis AOF, and real
browser runtimes. The quick profile is the bounded automated lane; full is the
longer pre-release profile.

Configuration:
  SCRAPEYARD_QUALIFICATION_PROJECT          Compose project
  SCRAPEYARD_QUALIFICATION_API_PORT         loopback API port (default 19420)
  SCRAPEYARD_QUALIFICATION_FIXTURE_PORT     fixture control port (default 19080)
  SCRAPEYARD_QUALIFICATION_BUILD_TIMEOUT    seconds (default 2400)
  SCRAPEYARD_QUALIFICATION_PHASE_TIMEOUT    seconds (default 300)
  SCRAPEYARD_QUALIFICATION_GLOBAL_TIMEOUT   seconds (quick 1800, full 5400)
  SCRAPEYARD_QUALIFICATION_SOAK_SECONDS     seconds (quick 130, full 900)
  SCRAPEYARD_QUALIFICATION_DIAGNOSTICS_DIR  bounded reports/logs directory
  SCRAPEYARD_QUALIFICATION_READ_P95_MS      threshold (default 750)
  SCRAPEYARD_QUALIFICATION_RECOVERY_SECONDS threshold (default 30)
  SCRAPEYARD_QUALIFICATION_DRAIN_SECONDS    threshold (default 180)
  SCRAPEYARD_QUALIFICATION_MEMORY_PEAK_MIB  threshold (default 6144)
  SCRAPEYARD_QUALIFICATION_MEMORY_GROWTH_MIB threshold (default 512)
  SCRAPEYARD_QUALIFICATION_DISK_GROWTH_MIB  threshold (default 1024)
  SCRAPEYARD_QUALIFICATION_DB_GROWTH_MIB    threshold (default 64)
  SCRAPEYARD_QUALIFICATION_TASK_GROWTH      threshold (default 4)
  SCRAPEYARD_QUALIFICATION_CPU_PERCENT      threshold (default 400)

Phases: all, recovery, redis_restart, load, soak, backup_restore.
EOF
}

PROFILE="quick"
ONLY_PHASE="all"
BUILD=1
NO_CACHE=0
while (($#)); do
  case "$1" in
    --profile)
      (($# >= 2)) || { echo "--profile requires quick or full" >&2; exit 2; }
      PROFILE=$2
      shift 2
      ;;
    --no-build) BUILD=0; shift ;;
    --no-cache) NO_CACHE=1; shift ;;
    --phase)
      (($# >= 2)) || { echo "--phase requires a phase name" >&2; exit 2; }
      ONLY_PHASE=$2
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
[[ "$PROFILE" == "quick" || "$PROFILE" == "full" ]] || {
  echo "--profile must be quick or full" >&2
  exit 2
}
((BUILD == 1 || NO_CACHE == 0)) || {
  echo "--no-cache cannot be combined with --no-build" >&2
  exit 2
}

fail() {
  echo "release qualification: $*" >&2
  exit 2
}

case "$ONLY_PHASE" in
  all|recovery|redis_restart|load|soak|backup_restore) ;;
  *) fail "--phase must be all, recovery, redis_restart, load, soak, or backup_restore" ;;
esac

validate_integer() {
  local label=$1 value=$2 minimum=$3 maximum=$4
  [[ "$value" =~ ^[0-9]+$ ]] || fail "$label must be an integer"
  ((value >= minimum && value <= maximum)) || \
    fail "$label must be between $minimum and $maximum"
}

validate_number() {
  local label=$1 value=$2
  [[ "$value" =~ ^[0-9]+([.][0-9]+)?$ ]] || fail "$label must be a non-negative number"
}

for command in docker python3 curl timeout mktemp od grep find; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
docker info >/dev/null 2>&1 || fail "the Docker daemon is unavailable"

PROJECT="${SCRAPEYARD_QUALIFICATION_PROJECT:-scrapeyard-release-qualification}"
API_PORT="${SCRAPEYARD_QUALIFICATION_API_PORT:-19420}"
FIXTURE_PORT="${SCRAPEYARD_QUALIFICATION_FIXTURE_PORT:-19080}"
BUILD_TIMEOUT="${SCRAPEYARD_QUALIFICATION_BUILD_TIMEOUT:-2400}"
PHASE_TIMEOUT="${SCRAPEYARD_QUALIFICATION_PHASE_TIMEOUT:-300}"
if [[ "$PROFILE" == "quick" ]]; then
  GLOBAL_TIMEOUT="${SCRAPEYARD_QUALIFICATION_GLOBAL_TIMEOUT:-1800}"
  SOAK_SECONDS="${SCRAPEYARD_QUALIFICATION_SOAK_SECONDS:-130}"
else
  GLOBAL_TIMEOUT="${SCRAPEYARD_QUALIFICATION_GLOBAL_TIMEOUT:-5400}"
  SOAK_SECONDS="${SCRAPEYARD_QUALIFICATION_SOAK_SECONDS:-900}"
fi
DIAGNOSTICS_DIR="${SCRAPEYARD_QUALIFICATION_DIAGNOSTICS_DIR:-$ROOT_DIR/artifacts/release-qualification-$PROFILE}"
READ_P95="${SCRAPEYARD_QUALIFICATION_READ_P95_MS:-750}"
RECOVERY_SECONDS="${SCRAPEYARD_QUALIFICATION_RECOVERY_SECONDS:-30}"
DRAIN_SECONDS="${SCRAPEYARD_QUALIFICATION_DRAIN_SECONDS:-180}"
MEMORY_PEAK="${SCRAPEYARD_QUALIFICATION_MEMORY_PEAK_MIB:-6144}"
MEMORY_GROWTH="${SCRAPEYARD_QUALIFICATION_MEMORY_GROWTH_MIB:-512}"
DISK_GROWTH="${SCRAPEYARD_QUALIFICATION_DISK_GROWTH_MIB:-1024}"
DB_GROWTH="${SCRAPEYARD_QUALIFICATION_DB_GROWTH_MIB:-64}"
TASK_GROWTH="${SCRAPEYARD_QUALIFICATION_TASK_GROWTH:-4}"
CPU_PERCENT="${SCRAPEYARD_QUALIFICATION_CPU_PERCENT:-400}"

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || \
  fail "project must be a lowercase Compose-safe name of at most 63 characters"
validate_integer API_PORT "$API_PORT" 1 65535
validate_integer FIXTURE_PORT "$FIXTURE_PORT" 1 65535
validate_integer BUILD_TIMEOUT "$BUILD_TIMEOUT" 60 7200
validate_integer PHASE_TIMEOUT "$PHASE_TIMEOUT" 60 1800
validate_integer GLOBAL_TIMEOUT "$GLOBAL_TIMEOUT" 300 14400
validate_integer SOAK_SECONDS "$SOAK_SECONDS" 120 3600
validate_integer TASK_GROWTH "$TASK_GROWTH" 0 100
for pair in \
  "READ_P95:$READ_P95" "RECOVERY_SECONDS:$RECOVERY_SECONDS" \
  "DRAIN_SECONDS:$DRAIN_SECONDS" "MEMORY_PEAK:$MEMORY_PEAK" \
  "MEMORY_GROWTH:$MEMORY_GROWTH" "DISK_GROWTH:$DISK_GROWTH" \
  "DB_GROWTH:$DB_GROWTH" "CPU_PERCENT:$CPU_PERCENT"; do
  validate_number "${pair%%:*}" "${pair#*:}"
done
[[ "$API_PORT" != "$FIXTURE_PORT" ]] || fail "API and fixture ports must differ"

port_is_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}
port_is_open "$API_PORT" && fail "port $API_PORT is already in use"
port_is_open "$FIXTURE_PORT" && fail "port $FIXTURE_PORT is already in use"

COMPOSE_ARGS=(
  -p "$PROJECT"
  -f docker-compose.yml
  -f docker-compose.smoke.yml
  -f docker-compose.qualification.yml
)
CREDENTIAL_DIR=""
BACKUP_DIR=""
CLEANUP_COMPLETE=0
APPARMOR_PROFILE_ACTIVE=0
POLICY_MODE=""
TOTAL_STARTED_AT=$SECONDS

compose() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
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

assert_no_project_resources() {
  local kind ids
  for kind in container network volume; do
    case "$kind" in
      container) ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ;;
      network) ids="$(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT")" ;;
      volume) ids="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")" ;;
    esac
    [[ -z "$ids" ]] || fail "cleanup left Item 15 $kind resources: $ids"
  done
}

capture_diagnostics() {
  set +e
  mkdir -p "$DIAGNOSTICS_DIR/screenshots"
  compose ps -a > "$DIAGNOSTICS_DIR/compose-ps.txt" 2>&1
  for service in scrapeyard redis fixture; do
    compose logs --no-color --timestamps --tail 500 "$service" \
      > "$DIAGNOSTICS_DIR/$service.log" 2>&1
  done
  curl --silent --show-error --max-time 5 \
    "http://127.0.0.1:$API_PORT/health" > "$DIAGNOSTICS_DIR/health-final.json" 2>&1
  curl --silent --show-error --max-time 5 \
    "http://127.0.0.1:$FIXTURE_PORT/__stats" > "$DIAGNOSTICS_DIR/fixture-final.json" 2>&1
  compose exec -T redis redis-cli INFO persistence \
    > "$DIAGNOSTICS_DIR/redis-persistence.txt" 2>&1
  mapfile -t screenshots < <(
    compose exec -T scrapeyard find /data/results/item15-load-browser \
      -type f -name '*.png' 2>/dev/null | head -n 4
  )
  local index=0
  for screenshot in "${screenshots[@]:-}"; do
    [[ -n "$screenshot" ]] || continue
    compose cp "scrapeyard:$screenshot" "$DIAGNOSTICS_DIR/screenshots/browser-$index.png" \
      >/dev/null 2>&1
    index=$((index + 1))
  done
  set -e
}

cleanup_resources() {
  set +e
  timeout 120 docker compose "${COMPOSE_ARGS[@]}" down -v --remove-orphans >/dev/null 2>&1
  if ((APPARMOR_PROFILE_ACTIVE == 1)); then
    run_apparmor_profile remove >/dev/null 2>&1
    APPARMOR_PROFILE_ACTIVE=0
  fi
  [[ -n "$CREDENTIAL_DIR" ]] && rm -rf -- "$CREDENTIAL_DIR"
  [[ -n "$BACKUP_DIR" ]] && rm -rf -- "$BACKUP_DIR"
  unset SCRAPEYARD_API_CREDENTIALS SCRAPEYARD_ENCRYPTION_KEYS \
    SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID SCRAPEYARD_BIND_ADDRESS SCRAPEYARD_PORT
  unset SCRAPEYARD_SMOKE_API_PORT SCRAPEYARD_SMOKE_FIXTURE_PORT
  unset SCRAPEYARD_QUALIFICATION_CRASH_POINT
  set -e
  CLEANUP_COMPLETE=1
}

scan_diagnostics() {
  local key=$1
  if grep -R -F -- "$key" "$DIAGNOSTICS_DIR" >/dev/null 2>&1; then
    fail "generated API credential appeared in diagnostics"
  fi
  if grep -R -a -l -E 'SQLite format 3|BEGIN (IMMEDIATE )?TRANSACTION|CREATE TABLE webhook_deliveries' \
    "$DIAGNOSTICS_DIR" >/dev/null 2>&1; then
    fail "database or backup contents appeared in diagnostics"
  fi
  if find "$DIAGNOSTICS_DIR" -type f \( -name '*.db' -o -name '*.sqlite*' -o -name '*.aof' -o -name '*.rdb' \) \
    -print -quit | grep -q .; then
    fail "database or Redis persistence file appeared in diagnostics"
  fi
}

on_exit() {
  local status=$?
  trap - EXIT INT TERM HUP
  if ((status != 0)); then
    echo "Qualification failed; retaining bounded diagnostics in $DIAGNOSTICS_DIR" >&2
    capture_diagnostics
  fi
  if ((CLEANUP_COMPLETE == 0)); then
    cleanup_resources
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT TERM HUP

assert_no_project_resources
rm -rf -- "$DIAGNOSTICS_DIR"
mkdir -p "$DIAGNOSTICS_DIR"
CREDENTIAL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scrapeyard-qualification-credentials.XXXXXX")"
BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scrapeyard-qualification-backup.XXXXXX")"
chmod 700 "$CREDENTIAL_DIR" "$BACKUP_DIR"
API_KEY_FILE="$CREDENTIAL_DIR/api-key"
printf 'item15-qualification-%s\n' "$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')" > "$API_KEY_FILE"
chmod 600 "$API_KEY_FILE"
API_KEY="$(<"$API_KEY_FILE")"

printf -v SCRAPEYARD_API_CREDENTIALS \
  '{"item15-qualification":{"secret":"%s","scopes":["submit","read","schedule-admin","delete","health-detail"]}}' \
  "$API_KEY"
export SCRAPEYARD_API_CREDENTIALS
ENCRYPTION_KEY="$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
printf -v SCRAPEYARD_ENCRYPTION_KEYS \
  '{"item20-qualification":"%s"}' \
  "$ENCRYPTION_KEY"
export SCRAPEYARD_ENCRYPTION_KEYS
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID="item20-qualification"
export SCRAPEYARD_BIND_ADDRESS="127.0.0.1"
export SCRAPEYARD_PORT="$API_PORT"
export SCRAPEYARD_SMOKE_API_PORT="$API_PORT"
export SCRAPEYARD_SMOKE_FIXTURE_PORT="$FIXTURE_PORT"
export SCRAPEYARD_QUALIFICATION_CRASH_POINT=""

if ((BUILD == 1)); then
  build_args=(build scrapeyard)
  ((NO_CACHE == 0)) || build_args=(build --no-cache scrapeyard)
  echo "Building the production Dockerfile (profile=$PROFILE no_cache=$NO_CACHE)..."
  timeout "$BUILD_TIMEOUT" docker compose "${COMPOSE_ARGS[@]}" "${build_args[@]}"
else
  docker image inspect "$PROJECT-scrapeyard" >/dev/null 2>&1 || \
    fail "--no-build requires existing image $PROJECT-scrapeyard"
fi

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

echo "Starting isolated Redis, fixture, and production Scrapeyard runtime..."
timeout 120 docker compose "${COMPOSE_ARGS[@]}" up -d --no-build
health_deadline=$((SECONDS + 120))
until curl --silent --fail --max-time 3 "http://127.0.0.1:$API_PORT/health" \
  > "$DIAGNOSTICS_DIR/health-initial.json" 2>/dev/null; do
  ((SECONDS < health_deadline)) || fail "Scrapeyard did not become healthy within 120 seconds"
  sleep 1
done

THRESHOLDS_JSON="$(python3 - <<PY
import json
print(json.dumps({
    "read_p95_ms": float("$READ_P95"),
    "recovery_seconds": float("$RECOVERY_SECONDS"),
    "drain_seconds": float("$DRAIN_SECONDS"),
    "memory_peak_mib": float("$MEMORY_PEAK"),
    "memory_growth_mib": float("$MEMORY_GROWTH"),
    "disk_growth_mib": float("$DISK_GROWTH"),
    "db_growth_mib": float("$DB_GROWTH"),
    "task_growth": int("$TASK_GROWTH"),
    "cpu_percent": float("$CPU_PERCENT"),
}))
PY
)"

driver_args=(
  --api-url "http://127.0.0.1:$API_PORT"
  --fixture-url "http://127.0.0.1:$FIXTURE_PORT"
  --api-key-file "$API_KEY_FILE"
  --diagnostics-dir "$DIAGNOSTICS_DIR"
  --backup-dir "$BACKUP_DIR"
  --repo-root "$ROOT_DIR"
  --profile "$PROFILE"
  --only-phase "$ONLY_PHASE"
  --soak-seconds "$SOAK_SECONDS"
  --phase-timeout "$PHASE_TIMEOUT"
  --thresholds-json "$THRESHOLDS_JSON"
)
for argument in "${COMPOSE_ARGS[@]}"; do
  driver_args+=("--compose-arg=$argument")
done

echo "Running recovery, Redis, load, soak, and fresh-restore phases..."
timeout "$GLOBAL_TIMEOUT" python3 tests/qualification/run_qualification.py "${driver_args[@]}"

capture_diagnostics
echo "Requesting graceful Scrapeyard shutdown..."
timeout 60 docker compose "${COMPOSE_ARGS[@]}" stop -t 40 scrapeyard
SCRAPEYARD_CONTAINER="$(compose ps -aq scrapeyard)"
SCRAPEYARD_EXIT_CODE="$(docker inspect --format '{{.State.ExitCode}}' "$SCRAPEYARD_CONTAINER")"
[[ "$SCRAPEYARD_EXIT_CODE" == "0" || "$SCRAPEYARD_EXIT_CODE" == "143" ]] || \
  fail "Scrapeyard exited with unexpected status $SCRAPEYARD_EXIT_CODE"
compose logs --no-color --tail 300 scrapeyard > "$DIAGNOSTICS_DIR/graceful-shutdown.log" 2>&1
grep -Eq 'Application shutdown complete|Finished server process' \
  "$DIAGNOSTICS_DIR/graceful-shutdown.log" || fail "graceful-shutdown marker is missing"

cat > "$DIAGNOSTICS_DIR/runner-summary.txt" <<EOF
profile=$PROFILE
phase=$ONLY_PHASE
total_seconds=$((SECONDS - TOTAL_STARTED_AT))
soak_seconds=$SOAK_SECONDS
global_timeout_seconds=$GLOBAL_TIMEOUT
phase_timeout_seconds=$PHASE_TIMEOUT
intended_host=4 CPU / 8 GiB RAM / 15 GiB free disk
EOF
scan_diagnostics "$API_KEY"

cleanup_resources
assert_no_project_resources
[[ ! -e "$BACKUP_DIR" ]] || fail "temporary backup directory remains: $BACKUP_DIR"
[[ ! -e "$CREDENTIAL_DIR" ]] || fail "generated credential directory remains: $CREDENTIAL_DIR"
CREDENTIAL_DIR=""
BACKUP_DIR=""
trap - EXIT INT TERM HUP

echo "Release qualification passed: profile=$PROFILE total_seconds=$((SECONDS - TOTAL_STARTED_AT))."
echo "Bounded reports: $DIAGNOSTICS_DIR"
