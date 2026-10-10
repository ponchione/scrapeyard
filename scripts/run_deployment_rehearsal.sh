#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/run_deployment_rehearsal.sh --candidate DIRECTORY --previous DIRECTORY

Rehearse a release with nothing but two retained release directories: deploy
the candidate through its own production wrapper, refuse a corrupted candidate,
leave the application stopped when host policy cannot be installed, refuse to
serve without the host egress policy, roll back to the previous release, and
roll forward again. Nothing is built or pulled.

Run it as root on a Docker host that runs nothing else. It installs the host
egress policy and the AppArmor profile, as a production deployment does, and
uses the production subnet.

Configuration:
  SCRAPEYARD_REHEARSAL_PROJECT          Compose project (default: scrapeyard-rehearsal)
  SCRAPEYARD_REHEARSAL_DEPLOY_TIMEOUT   seconds for one deployment (default: 300)
  SCRAPEYARD_REHEARSAL_JOB_TIMEOUT      seconds for the known run (default: 120)
  SCRAPEYARD_REHEARSAL_DIAGNOSTICS_DIR  evidence directory
EOF
}

CANDIDATE=""
PREVIOUS=""
while (($#)); do
  case "$1" in
    --candidate|--previous)
      (($# >= 2)) || { echo "$1 requires a release directory" >&2; exit 2; }
      if [[ "$1" == "--candidate" ]]; then CANDIDATE=$2; else PREVIOUS=$2; fi
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

fail() {
  echo "deployment rehearsal: $*" >&2
  exit 2
}

[[ -n "$CANDIDATE" && -n "$PREVIOUS" ]] || { usage >&2; exit 2; }
((EUID == 0)) || fail "run as root: the production wrapper installs host policy"
for command in docker python3 curl timeout mktemp od iptables ip realpath; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
docker info >/dev/null 2>&1 || fail "the Docker daemon is unavailable"

PROJECT="${SCRAPEYARD_REHEARSAL_PROJECT:-scrapeyard-rehearsal}"
DEPLOY_TIMEOUT="${SCRAPEYARD_REHEARSAL_DEPLOY_TIMEOUT:-300}"
JOB_TIMEOUT="${SCRAPEYARD_REHEARSAL_JOB_TIMEOUT:-120}"
DIAGNOSTICS_DIR="${SCRAPEYARD_REHEARSAL_DIAGNOSTICS_DIR:-$ROOT_DIR/artifacts/deployment-rehearsal}"
POLICY_ID="rehearsal"
# Production needs an operator proxy at a public address, and the rehearsal has
# none. This address passes the startup check and is made unreachable on this
# host for the length of the run, so nothing can be sent to whoever owns it.
PROXY_ADDRESS="203.66.81.250"
STARTED_AT=$SECONDS
STARTED_EPOCH=$(date +%s)

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || \
  fail "SCRAPEYARD_REHEARSAL_PROJECT must be a lowercase Compose-safe name (max 63 characters)"
for pair in "DEPLOY_TIMEOUT:$DEPLOY_TIMEOUT" "JOB_TIMEOUT:$JOB_TIMEOUT"; do
  [[ "${pair#*:}" =~ ^[0-9]+$ ]] && ((${pair#*:} >= 30 && ${pair#*:} <= 1800)) || \
    fail "SCRAPEYARD_REHEARSAL_${pair%%:*} must be between 30 and 1800"
done
CANDIDATE=$(realpath -- "$CANDIDATE")
PREVIOUS=$(realpath -- "$PREVIOUS")
[[ "$CANDIDATE" != "$PREVIOUS" ]] || fail "the candidate and the previous release must be different directories"
for release in "$CANDIDATE" "$PREVIOUS"; do
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/release_artifact.py verify "$release" >/dev/null || \
    fail "not a verified release: $release"
done

WORK_DIR=""
CORRUPTED=""
APP_CONTAINER=""
API_URL=""
BRIDGE=""
PROBE_ADDRESS=""
ROUTE_ACTIVE=0
APPARMOR_PROFILE_ACTIVE=0
CLEANUP_COMPLETE=0

manifest_value() {
  python3 - "$1/manifest.json" "$2" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
for key in sys.argv[2].split("."):
    value = value[key]
print(value)
PY
}

project_resources() {
  case "$1" in
    container) docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" ;;
    network) docker network ls -q --filter "label=com.docker.compose.project=$PROJECT" ;;
    volume) docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" ;;
  esac
}

assert_no_project_resources() {
  local kind ids
  for kind in container network volume; do
    ids="$(project_resources "$kind")"
    [[ -z "$ids" ]] || fail "rehearsal $kind resources already exist or were left behind: $ids"
  done
}

service_containers() {
  docker ps -aq --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$1"
}

apparmor_profile_loaded() {
  grep -q '^scrapeyard-chromium ' /sys/kernel/security/apparmor/profiles 2>/dev/null
}

policy_jumps() {
  iptables -S DOCKER-USER | grep -E -- " -j SY-EGRESS-$POLICY_ID(-[AB])?\$" || true
}

remove_egress_policy() {
  [[ -n "$BRIDGE" ]] || return 0
  SCRAPEYARD_EGRESS_INTERFACE="$BRIDGE" SCRAPEYARD_EGRESS_POLICY_ID="$POLICY_ID" \
    "$CANDIDATE/source/security/install-docker-egress-policy.sh" remove
}

capture_diagnostics() {
  set +e
  local container name
  for container in $(project_resources container); do
    name="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$container")"
    docker logs --timestamps --tail 300 "$container" > "$DIAGNOSTICS_DIR/$name.log" 2>&1
  done
  docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
    > "$DIAGNOSTICS_DIR/containers.txt" 2>&1
  iptables -S DOCKER-USER > "$DIAGNOSTICS_DIR/docker-user-chain.txt" 2>&1
  set -e
}

# Logs and evidence leave the host as a workflow artifact. Neither generated
# credential may be in them.
scan_diagnostics() {
  local key_file
  for key_file in "$WORK_DIR/client-key" "$WORK_DIR/monitor-key"; do
    [[ -f "$key_file" ]] || continue
    if grep -R -F -q -- "$(<"$key_file")" "$DIAGNOSTICS_DIR"; then
      rm -rf -- "$DIAGNOSTICS_DIR"
      fail "a generated credential appeared in the diagnostics; they were deleted"
    fi
  done
}

cleanup_resources() {
  set +e
  local ids
  ids="$(project_resources container)"
  [[ -z "$ids" ]] || docker rm -f -v $ids >/dev/null 2>&1
  remove_egress_policy >/dev/null 2>&1
  ids="$(project_resources network)"
  [[ -z "$ids" ]] || docker network rm $ids >/dev/null 2>&1
  ids="$(project_resources volume)"
  [[ -z "$ids" ]] || docker volume rm $ids >/dev/null 2>&1
  if ((APPARMOR_PROFILE_ACTIVE == 1)); then
    "$CANDIDATE/source/security/install-chromium-apparmor-profile.sh" remove >/dev/null 2>&1
    APPARMOR_PROFILE_ACTIVE=0
  fi
  if ((ROUTE_ACTIVE == 1)); then
    ip route del unreachable "$PROXY_ADDRESS/32" >/dev/null 2>&1
    ROUTE_ACTIVE=0
  fi
  [[ -z "$CORRUPTED" ]] || rm -rf -- "$CORRUPTED"
  set -e
  CLEANUP_COMPLETE=1
}

on_exit() {
  local status=$?
  trap - EXIT INT TERM HUP
  if ((status != 0)) && [[ -d "$DIAGNOSTICS_DIR" ]]; then
    echo "Deployment rehearsal failed; retaining diagnostics in $DIAGNOSTICS_DIR" >&2
    capture_diagnostics
    (scan_diagnostics) || true
  fi
  if ((CLEANUP_COMPLETE == 0)); then
    cleanup_resources
  fi
  [[ -z "$WORK_DIR" ]] || rm -rf -- "$WORK_DIR"
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT TERM HUP

assert_no_project_resources
[[ -z "$(policy_jumps)" ]] || fail "a rehearsal egress policy is already installed on this host"
if [[ -e "$DIAGNOSTICS_DIR" && ! -d "$DIAGNOSTICS_DIR" ]]; then
  fail "diagnostics path exists and is not a directory: $DIAGNOSTICS_DIR"
fi
rm -rf -- "$DIAGNOSTICS_DIR"
mkdir -p "$DIAGNOSTICS_DIR"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scrapeyard-rehearsal.XXXXXX")"
chmod 700 "$WORK_DIR"

for name in client monitor; do
  printf 'rehearsal-%s-%s\n' "$name" "$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')" \
    > "$WORK_DIR/$name-key"
  chmod 600 "$WORK_DIR/$name-key"
done
# The shape production uses: a client limited to submit and read in one
# project, and a separate monitoring credential.
printf -v SCRAPEYARD_API_CREDENTIALS \
  '{"rehearsal-client":{"secret":"%s","scopes":["submit","read"],"projects":["release-rehearsal"]},"rehearsal-monitor":{"secret":"%s","scopes":["health-detail"]}}' \
  "$(<"$WORK_DIR/client-key")" "$(<"$WORK_DIR/monitor-key")"
printf -v SCRAPEYARD_ENCRYPTION_KEYS '{"rehearsal":"%s"}' \
  "$(head -c 32 /dev/urandom | base64 | tr -d '\n')"
export SCRAPEYARD_API_CREDENTIALS SCRAPEYARD_ENCRYPTION_KEYS
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID="rehearsal"
export SCRAPEYARD_PROXY_URL="http://$PROXY_ADDRESS:3128"
export SCRAPEYARD_EGRESS_POLICY_ID="$POLICY_ID"
export COMPOSE_PROJECT_NAME="$PROJECT"
export PYTHONDONTWRITEBYTECODE=1

release_config() {
  (
    cd "$1/source"
    COMPOSE_FILE="$1/source/docker-compose.yml:$1/images.json" docker compose config --format json
  )
}

# The rendered configuration holds the generated credentials; it stays in the
# private work directory.
release_config "$CANDIDATE" > "$WORK_DIR/candidate-config.json"
release_config "$PREVIOUS" > "$WORK_DIR/previous-config.json"
docker network ls -q | xargs -r docker network inspect > "$WORK_DIR/networks.json"
python3 - "$WORK_DIR/networks.json" "$WORK_DIR/candidate-config.json" "$WORK_DIR/previous-config.json" <<'PY' \
  || fail "use an isolated Docker host"
import ipaddress
import json
import sys

existing = [
    (network["Name"], ipaddress.ip_network(item["Subnet"]))
    for network in json.load(open(sys.argv[1], encoding="utf-8"))
    for item in network.get("IPAM", {}).get("Config") or []
    if item.get("Subnet")
]
for path in sys.argv[2:]:
    for network in json.load(open(path, encoding="utf-8"))["networks"].values():
        for item in network.get("ipam", {}).get("config", []):
            subnet = ipaddress.ip_network(item["subnet"])
            for name, other in existing:
                if subnet.version == other.version and subnet.overlaps(other):
                    sys.exit(f"deployment rehearsal: {subnet} overlaps existing network {name}")
PY

ip route add unreachable "$PROXY_ADDRESS/32"
ROUTE_ACTIVE=1
if ! apparmor_profile_loaded; then
  APPARMOR_PROFILE_ACTIVE=1
fi

deploy() {
  local label=$1 release=$2
  shift 2
  echo "[$label] deploying $(manifest_value "$release" revision) with its own wrapper..."
  env "$@" timeout "$DEPLOY_TIMEOUT" \
    "$release/source/security/deploy-secure-compose.sh" --release "$release" \
    > "$DIAGNOSTICS_DIR/$label-deploy.log" 2>&1
}

app_is_running() {
  [[ -n "$APP_CONTAINER" ]] && \
    [[ "$(docker inspect --format '{{.State.Status}}' "$APP_CONTAINER" 2>/dev/null)" == "running" ]]
}

verify_api() {
  local label=$1
  shift
  python3 tests/rehearsal/verify_deployment.py check \
    --api-url "$API_URL" \
    --client-key-file "$WORK_DIR/client-key" \
    --monitor-key-file "$WORK_DIR/monitor-key" \
    "$@" > "$DIAGNOSTICS_DIR/$label-api.json"
}

# Everything a deployment must leave true, checked from outside the application.
verify_runtime() {
  local label=$1 release=$2 name=$3 service container network_id
  local -a found
  for service in scrapeyard redis egress-probe; do
    mapfile -t found < <(service_containers "$service")
    ((${#found[@]} == 1)) || fail "[$label] expected one $service container, found ${#found[@]}"
    container=${found[0]}
    [[ "$(docker inspect --format '{{.Image}}' "$container")" == \
       "$(manifest_value "$release" "images.$service")" ]] || \
      fail "[$label] $service does not run the image the release retains"
    [[ "$(docker inspect --format '{{.State.Status}}/{{.State.Health.Status}}' "$container")" == \
       "running/healthy" ]] || fail "[$label] $service is not running and healthy"
    [[ -z "$(docker port "$container")" ]] || fail "[$label] $service publishes a host port"
  done
  APP_CONTAINER="$(service_containers scrapeyard)"
  PROBE_ADDRESS="$(docker inspect --format \
    '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(service_containers egress-probe)")"
  API_URL="http://$(docker inspect --format \
    '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$APP_CONTAINER"):8420"
  network_id="$(docker inspect --format \
    '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}' "$APP_CONTAINER")"
  BRIDGE="br-${network_id:0:12}"

  # Docker's view of the container against what the release declares. The
  # inspection holds the credentials, so only the verdicts are kept.
  docker inspect "$APP_CONTAINER" > "$WORK_DIR/app-inspect.json"
  python3 - "$WORK_DIR/app-inspect.json" "$WORK_DIR/$name-config.json" "$PROJECT" <<'PY' \
    > "$DIAGNOSTICS_DIR/$label-confinement.json" || fail "[$label] the container is not confined as the release declares"
import json
import sys

container = json.load(open(sys.argv[1], encoding="utf-8"))[0]
declared = json.load(open(sys.argv[2], encoding="utf-8"))["services"]["scrapeyard"]
host = container["HostConfig"]
options = [option.replace("=", ":", 1) for option in host["SecurityOpt"] or []]
capabilities = {
    key: sorted(value.removeprefix("CAP_") for value in host[key] or [])
    for key in ("CapAdd", "CapDrop")
}
checks = {
    "apparmor_profile": container["AppArmorProfile"] == "scrapeyard-chromium",
    "seccomp_profile": any(o.startswith("seccomp:") and "unconfined" not in o for o in options),
    "no_new_privileges": "no-new-privileges:true" in options,
    "not_privileged": host["Privileged"] is False,
    "read_only_root": host["ReadonlyRootfs"] is True,
    "all_capabilities_dropped": capabilities["CapDrop"] == ["ALL"],
    "added_capabilities_as_declared": capabilities["CapAdd"] == sorted(declared.get("cap_add") or []),
    "init_process": host["Init"] is True,
    "non_root_user": container["Config"]["User"] not in ("", "0", "root", "0:0"),
    "pids_limit_as_declared": host["PidsLimit"] == declared["pids_limit"],
    "memory_limit_as_declared": host["Memory"] == int(declared["mem_limit"]),
    "cpu_limit_as_declared": host["NanoCpus"] == int(float(declared["cpus"]) * 1_000_000_000),
    "only_the_backend_network": list(container["NetworkSettings"]["Networks"]) == [f"{sys.argv[3]}_backend"],
    "no_docker_socket": not any("docker.sock" in mount["Source"] for mount in container["Mounts"]),
}
print(json.dumps(checks, indent=2, sort_keys=True))
sys.exit(0 if all(checks.values()) else 1)
PY

  # The kernel's view from inside the container.
  [[ "$(docker exec "$APP_CONTAINER" python -c '
from pathlib import Path
status = dict(
    line.split(":\t", 1) for line in Path("/proc/self/status").read_text().splitlines()
)
print(
    Path("/proc/self/attr/current").read_text().strip(),
    status["NoNewPrivs"].strip(), status["Seccomp"].strip(), status["CapEff"].strip(),
)')" == "scrapeyard-chromium (enforce) 1 2 0000000000000000" ]] || \
    fail "[$label] AppArmor, seccomp, no-new-privileges or capabilities are not in force"
  (($(docker top "$APP_CONTAINER" -eo pid,args | grep -F 'scrapeyard.main:app' | grep -c -v 'docker-init') == 1)) || \
    fail "[$label] expected exactly one application process"

  # The host policy, and that it holds from the application's network namespace.
  (($(policy_jumps | grep -c -F -- "-i $BRIDGE ") == 1 && $(policy_jumps | wc -l) == 1)) || \
    fail "[$label] expected one egress policy jump, on $BRIDGE"
  [[ "$(</proc/sys/net/bridge/bridge-nf-call-iptables)" == 1 ]] || \
    fail "[$label] bridge netfilter is off"
  docker exec "$APP_CONTAINER" python -c \
    "import socket; socket.create_connection(('$PROBE_ADDRESS', 8081), 3).close()" || \
    fail "[$label] the probe's allowed liveness port is unreachable"
  if docker exec "$APP_CONTAINER" curl --silent --output /dev/null --max-time 3 \
    "http://$PROBE_ADDRESS:8080/"; then
    fail "[$label] the egress policy let the application reach a private peer"
  fi
  if docker exec "$APP_CONTAINER" curl --silent --output /dev/null --max-time 3 \
    'http://169.254.169.254/latest/meta-data/'; then
    fail "[$label] the egress policy let the application reach the metadata address"
  fi

  python3 scripts/release_artifact.py verify "$release" >/dev/null || \
    fail "[$label] deploying changed the retained release"
  echo "[$label] running $(manifest_value "$release" revision): images, confinement, policy and one instance verified"
}

echo "Deploying the candidate release on a clean host..."
deploy candidate "$CANDIDATE"
verify_runtime candidate "$CANDIDATE" candidate
verify_api candidate
python3 tests/rehearsal/verify_deployment.py record \
  --api-url "$API_URL" \
  --client-key-file "$WORK_DIR/client-key" \
  --monitor-key-file "$WORK_DIR/monitor-key" \
  --known "$WORK_DIR/known-run.json" \
  --job-timeout "$JOB_TIMEOUT" > "$DIAGNOSTICS_DIR/known-run.json"
verify_api candidate-known --known "$WORK_DIR/known-run.json"

echo "Offering a corrupted candidate; the running release must be left alone..."
# Hard links keep the copy cheap, so it sits beside the release it copies.
CORRUPTED="$(dirname "$CANDIDATE")/.rehearsal-corrupted.$$"
cp -al "$CANDIDATE" "$CORRUPTED"
# Replace the link before writing, so the real release is not changed.
rm -f -- "$CORRUPTED/source/docker-compose.yml"
cp -- "$CANDIDATE/source/docker-compose.yml" "$CORRUPTED/source/docker-compose.yml"
echo "# changed after the release was retained" >> "$CORRUPTED/source/docker-compose.yml"
RUNNING_BEFORE="$APP_CONTAINER"
if deploy corrupted "$CORRUPTED"; then
  fail "a release whose files changed was deployed"
fi
[[ "$(service_containers scrapeyard)" == "$RUNNING_BEFORE" ]] && app_is_running || \
  fail "refusing the corrupted release disturbed the running application"
verify_runtime corrupted-refused "$CANDIDATE" candidate
verify_api corrupted-refused --known "$WORK_DIR/known-run.json"
rm -rf -- "$CORRUPTED"
CORRUPTED=""

echo "Failing the host policy installation; the application must stay stopped..."
JUMPS_BEFORE="$(policy_jumps)"
if deploy policy-failure "$CANDIDATE" SCRAPEYARD_EGRESS_ALLOW_CIDRS=not-a-network; then
  fail "the deployment continued although the egress policy could not be installed"
fi
app_is_running && fail "the application is running after a failed policy installation"
[[ "$(policy_jumps)" == "$JUMPS_BEFORE" ]] || \
  fail "a failed policy installation changed the active policy"

echo "Starting the application without the host egress policy; it must refuse to serve..."
remove_egress_policy
[[ -z "$(policy_jumps)" ]] || fail "the egress policy could not be removed"
docker start "$APP_CONTAINER" >/dev/null
REFUSAL_DEADLINE=$((SECONDS + 60))
until docker logs "$APP_CONTAINER" 2>&1 | grep -q 'controlled non-public challenge was reachable'; do
  if curl --silent --output /dev/null --max-time 2 "$API_URL/health"; then
    fail "the application served without the host egress policy"
  fi
  ((SECONDS < REFUSAL_DEADLINE)) || fail "the application did not report the missing egress policy"
  sleep 1
done
# It is restarted by Docker until the policy is back; it must never serve.
for _ in 1 2 3 4 5; do
  if curl --silent --output /dev/null --max-time 2 "$API_URL/health"; then
    fail "the application served without the host egress policy"
  fi
  sleep 1
done
docker stop --time 40 "$APP_CONTAINER" >/dev/null

echo "Rolling back to the previous release..."
deploy rollback "$PREVIOUS"
verify_runtime rollback "$PREVIOUS" previous
verify_api rollback --known "$WORK_DIR/known-run.json"

echo "Rolling forward to the candidate again..."
deploy roll-forward "$CANDIDATE"
verify_runtime roll-forward "$CANDIDATE" candidate
verify_api roll-forward --known "$WORK_DIR/known-run.json"

# Promotion and rollback must work from the retained archives alone.
docker events --since "$STARTED_EPOCH" --until "$(date +%s)" --filter type=image \
  --format '{{.Action}} {{.Actor.ID}}' > "$DIAGNOSTICS_DIR/image-events.txt"
if grep -E -q '^pull ' "$DIAGNOSTICS_DIR/image-events.txt"; then
  fail "an image was pulled during the rehearsal"
fi

capture_diagnostics
echo "Requesting graceful Scrapeyard shutdown..."
docker stop --time 40 "$APP_CONTAINER" >/dev/null
EXIT_CODE="$(docker inspect --format '{{.State.ExitCode}}' "$APP_CONTAINER")"
[[ "$EXIT_CODE" == "0" || "$EXIT_CODE" == "143" ]] || \
  fail "Scrapeyard exited with unexpected status $EXIT_CODE"

cat > "$DIAGNOSTICS_DIR/summary.txt" <<EOF
candidate_revision=$(manifest_value "$CANDIDATE" revision)
candidate_image=$(manifest_value "$CANDIDATE" images.scrapeyard)
previous_revision=$(manifest_value "$PREVIOUS" revision)
previous_image=$(manifest_value "$PREVIOUS" images.scrapeyard)
docker_engine=$(docker version --format '{{.Server.Version}}')
docker_compose=$(docker compose version --short)
kernel=$(uname -r)
total_seconds=$((SECONDS - STARTED_AT))
EOF
scan_diagnostics

cleanup_resources
assert_no_project_resources
[[ -z "$(policy_jumps)" ]] || fail "cleanup left the rehearsal egress policy installed"
rm -rf -- "$WORK_DIR"
WORK_DIR=""
trap - EXIT INT TERM HUP

echo "Deployment rehearsal passed in $((SECONDS - STARTED_AT))s: candidate deployed, corrupted release refused,"
echo "policy failures left nothing serving, and rollback and roll-forward kept the known run."
echo "Evidence: $DIAGNOSTICS_DIR"
