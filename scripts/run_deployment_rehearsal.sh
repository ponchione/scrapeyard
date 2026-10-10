#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/run_deployment_rehearsal.sh --candidate DIRECTORY --previous DIRECTORY [--boot-service]

Rehearse a release with nothing but two retained release directories: deploy
the candidate through its own production wrapper, refuse a corrupted candidate,
leave the application stopped when host policy cannot be installed, refuse to
serve without the host egress policy, roll back to the previous release, and
roll forward again. Nothing is built or pulled.

With --boot-service it then installs the candidate's systemd unit as the
deployment guide does and checks a refused and an interrupted promotion, a
Docker restart, a Docker crash, a Docker start with everything a reboot loses
gone, a recreated deployment network, a start that fails until its setting is
repaired, and a promoted rollback. That part needs systemd. It stops Docker,
unloads the AppArmor profile and switches the bridge netfilter hook off for a
moment, and the paths it installs must not exist yet: /etc/scrapeyard,
/etc/systemd/system/scrapeyard.service, /usr/local/sbin/scrapeyard-release
and /srv/scrapeyard-releases/current.

Run it as root on a Docker host that runs nothing else. It installs the host
egress policy and the AppArmor profile, as a production deployment does, and
uses the production subnet.

Configuration:
  SCRAPEYARD_REHEARSAL_PROJECT          Compose project (default: scrapeyard-rehearsal)
  SCRAPEYARD_REHEARSAL_DEPLOY_TIMEOUT   seconds for one deployment (default: 600)
  SCRAPEYARD_REHEARSAL_JOB_TIMEOUT      seconds for the known run (default: 120)
  SCRAPEYARD_REHEARSAL_DIAGNOSTICS_DIR  evidence directory
EOF
}

CANDIDATE=""
PREVIOUS=""
BOOT_SERVICE=0
while (($#)); do
  case "$1" in
    --boot-service) BOOT_SERVICE=1; shift ;;
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
DEPLOY_TIMEOUT="${SCRAPEYARD_REHEARSAL_DEPLOY_TIMEOUT:-600}"
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

UNIT="scrapeyard.service"
UNIT_FILE="/etc/systemd/system/$UNIT"
UNIT_DROP_IN="/etc/systemd/system/$UNIT.d"
ENV_DIR="/etc/scrapeyard"
RELEASES_ROOT="/srv/scrapeyard-releases"
MANAGE="/usr/local/sbin/scrapeyard-release"
if ((BOOT_SERVICE == 1)); then
  for command in systemctl systemd-run apparmor_parser; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required for --boot-service"
  done
  for path in "$ENV_DIR" "$UNIT_FILE" "$UNIT_DROP_IN" "$MANAGE" "$RELEASES_ROOT/current"; do
    [[ ! -e "$path" && ! -L "$path" ]] || fail "$path already exists; use an isolated host"
  done
  [[ -f "$CANDIDATE/source/security/systemd/$UNIT" && \
     -x "$CANDIDATE/source/security/manage-release.sh" ]] || \
    fail "the candidate release has no boot service to rehearse"
  # The simulated reboot unloads the profile, which must then be this run's own.
  ! grep -q '^scrapeyard-chromium ' /sys/kernel/security/apparmor/profiles 2>/dev/null || \
    fail "the AppArmor profile is already loaded; use an isolated host"
  [[ "$(systemctl is-active docker.service || true)" == "active" ]] || \
    fail "docker.service must be running under systemd"
fi

WORK_DIR=""
BOOT_SERVICE_ACTIVE=0
RELEASES_ROOT_CREATED=0
RELEASE_OWNER=""
PROMOTE_UNIT="scrapeyard-promote.service"
CORRUPTED=""
APP_CONTAINER=""
API_URL=""
BRIDGE=""
PROBE_ADDRESS=""
ROUTE_ACTIVE=0
APPARMOR_PROFILE_ACTIVE=0
PREFLIGHT_PASSED=0
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

# Removes the rehearsal's policy from every interface it is installed on. The
# interfaces come from the jumps themselves: the deployment bridge changes when
# the network is recreated, and one step installs the policy somewhere else.
remove_egress_policy() {
  local interface
  for interface in $(policy_jumps | sed -n 's/^-A DOCKER-USER -i \([^ ]*\) -j .*/\1/p' | sort -u); do
    SCRAPEYARD_EGRESS_INTERFACE="$interface" SCRAPEYARD_EGRESS_POLICY_ID="$POLICY_ID" \
      "$CANDIDATE/source/security/install-docker-egress-policy.sh" remove
  done
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
  if ((BOOT_SERVICE_ACTIVE == 1)); then
    systemctl status --no-pager --lines 0 "$UNIT" > "$DIAGNOSTICS_DIR/boot-service-status.txt" 2>&1
    journalctl --no-pager --unit "$UNIT" --since "@$STARTED_EPOCH" --lines 400 \
      > "$DIAGNOSTICS_DIR/boot-service-journal.log" 2>&1
  fi
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
  if ((BOOT_SERVICE_ACTIVE == 1)); then
    systemctl stop "$PROMOTE_UNIT" >/dev/null 2>&1
    systemctl disable --now "$UNIT" >/dev/null 2>&1
    rm -rf -- "$UNIT_FILE" "$UNIT_DROP_IN" "$ENV_DIR" "$MANAGE" "$RELEASES_ROOT/current"
    ((RELEASES_ROOT_CREATED == 0)) || rmdir -- "$RELEASES_ROOT" >/dev/null 2>&1
    systemctl daemon-reload >/dev/null 2>&1
    systemctl reset-failed "$UNIT" >/dev/null 2>&1
    # A step that stopped Docker may have failed before starting it again,
    # and nothing below can be removed without it.
    systemctl start docker.service >/dev/null 2>&1
    [[ -z "$RELEASE_OWNER" ]] || chown -R "$RELEASE_OWNER" "$CANDIDATE" "$PREVIOUS" >/dev/null 2>&1
    BOOT_SERVICE_ACTIVE=0
  fi
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
  if ((status != 0 && PREFLIGHT_PASSED == 1)) && [[ -d "$DIAGNOSTICS_DIR" ]]; then
    echo "Deployment rehearsal failed; retaining diagnostics in $DIAGNOSTICS_DIR" >&2
    capture_diagnostics
    (scan_diagnostics) || true
  fi
  # Before the preflight passes, anything found on the host is someone else's.
  if ((PREFLIGHT_PASSED == 1 && CLEANUP_COMPLETE == 0)); then
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

PREFLIGHT_PASSED=1
[[ -e /sys/class/net/docker0 ]] || fail "Docker's default bridge docker0 is needed for one step"
ip route add unreachable "$PROXY_ADDRESS/32"
ROUTE_ACTIVE=1
if ! apparmor_profile_loaded; then
  APPARMOR_PROFILE_ACTIVE=1
fi

# The daemon keeps its events in memory, so they are collected before every
# Docker restart as well as at the end.
EVENTS_SINCE=$STARTED_EPOCH
record_image_events() {
  local now
  now=$(date +%s)
  docker events --since "$EVENTS_SINCE" --until "$now" --filter type=image \
    --format '{{.Action}} {{.Actor.ID}}' >> "$DIAGNOSTICS_DIR/image-events.txt"
  EVENTS_SINCE=$now
}

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

# Whether the application's network namespace can open a TCP connection.
app_connects() {
  docker exec "$APP_CONTAINER" python -c \
    'import socket, sys; socket.create_connection((sys.argv[1], int(sys.argv[2])), 3).close()' \
    "$1" "$2" >/dev/null 2>&1
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
options = [option.replace("=", ":", 1) for option in host.get("SecurityOpt") or []]
capabilities = {
    key: sorted(value.removeprefix("CAP_") for value in host.get(key) or [])
    for key in ("CapAdd", "CapDrop")
}
# Docker records a CPU limit as nano-CPUs or as a quota over a period.
cpus = host.get("NanoCpus", 0) / 1_000_000_000 or (
    host.get("CpuQuota", 0) / host["CpuPeriod"] if host.get("CpuPeriod") else 0
)
checks = {
    "apparmor_profile": container["AppArmorProfile"] == "scrapeyard-chromium",
    "seccomp_profile": any(o.startswith("seccomp:") and "unconfined" not in o for o in options),
    "no_new_privileges": "no-new-privileges:true" in options,
    "not_privileged": host.get("Privileged") is False,
    "read_only_root": host.get("ReadonlyRootfs") is True,
    "all_capabilities_dropped": capabilities["CapDrop"] == ["ALL"],
    "added_capabilities_as_declared": capabilities["CapAdd"] == sorted(declared.get("cap_add") or []),
    "init_process": host.get("Init") is True,
    "non_root_user": container["Config"]["User"] not in ("", "0", "root", "0:0"),
    "pids_limit_as_declared": host.get("PidsLimit") == declared["pids_limit"],
    "memory_limit_as_declared": host.get("Memory") == int(declared["mem_limit"]),
    "cpu_limit_as_declared": cpus == float(declared["cpus"]),
    "only_the_backend_network": list(container["NetworkSettings"]["Networks"]) == [f"{sys.argv[3]}_backend"],
    "no_docker_socket": not any(
        "docker.sock" in mount.get("Source", "") for mount in container.get("Mounts") or []
    ),
}
print(json.dumps(checks, indent=2, sort_keys=True))
sys.exit(0 if all(checks.values()) else 1)
PY

  # The kernel's view from inside the container.
  [[ "$(docker exec "$APP_CONTAINER" python -c '
from pathlib import Path
status = {}
for line in Path("/proc/self/status").read_text().splitlines():
    key, _, value = line.partition(":")
    status[key] = value.strip()
print(
    Path("/proc/self/attr/current").read_text().strip(),
    status["NoNewPrivs"], status["Seccomp"], status["CapEff"],
)')" == "scrapeyard-chromium (enforce) 1 2 0000000000000000" ]] || \
    fail "[$label] AppArmor, seccomp, no-new-privileges or capabilities are not in force"
  (($(docker top "$APP_CONTAINER" -eo pid,args | grep -F 'scrapeyard.main:app' | grep -c -v 'docker-init') == 1)) || \
    fail "[$label] expected exactly one application process"

  # The host policy, and that it holds from the application's network namespace.
  (($(policy_jumps | grep -c -F -- "-i $BRIDGE ") == 1 && $(policy_jumps | wc -l) == 1)) || \
    fail "[$label] expected one egress policy jump, on $BRIDGE"
  [[ "$(</proc/sys/net/bridge/bridge-nf-call-iptables)" == 1 ]] || \
    fail "[$label] bridge netfilter is off"
  app_connects "$PROBE_ADDRESS" 8081 || \
    fail "[$label] the probe's allowed liveness port is unreachable"
  if app_connects "$PROBE_ADDRESS" 8080; then
    fail "[$label] the egress policy let the application reach a private peer"
  fi
  if app_connects 169.254.169.254 80; then
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
# This container is replaced by the rollback, so its log is kept as evidence.
until docker logs --tail 200 "$APP_CONTAINER" > "$DIAGNOSTICS_DIR/unprotected-start.log" 2>&1 && \
  grep -q 'controlled non-public challenge was reachable' "$DIAGNOSTICS_DIR/unprotected-start.log"; do
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
docker stop -t 40 "$APP_CONTAINER" >/dev/null

echo "Deploying with the policy on another bridge; the start must fail and leave the application stopped..."
if deploy uncovered-bridge "$CANDIDATE" SCRAPEYARD_EGRESS_INTERFACE=docker0; then
  fail "the deployment succeeded although the egress policy did not cover its bridge"
fi
APP_CONTAINER="$(service_containers scrapeyard)"
docker logs --tail 200 "$APP_CONTAINER" > "$DIAGNOSTICS_DIR/uncovered-bridge-start.log" 2>&1
grep -q 'controlled non-public challenge was reachable' "$DIAGNOSTICS_DIR/uncovered-bridge-start.log" || \
  fail "the deployment failed for another reason than the uncovered bridge"
# Its container restarts by itself unless it was stopped, and a restarting
# container could come up later, outside any deployment.
for _ in 1 2 3 4 5; do
  [[ "$(docker inspect --format '{{.State.Status}}' "$APP_CONTAINER")" == "exited" ]] || \
    fail "a failed start left the application container restarting"
  sleep 1
done
remove_egress_policy
[[ -z "$(policy_jumps)" ]] || fail "the misplaced egress policy could not be removed"

echo "Rolling back to the previous release..."
deploy rollback "$PREVIOUS"
verify_runtime rollback "$PREVIOUS" previous
verify_api rollback --known "$WORK_DIR/known-run.json"

echo "Rolling forward to the candidate again..."
deploy roll-forward "$CANDIDATE"
verify_runtime roll-forward "$CANDIDATE" candidate
verify_api roll-forward --known "$WORK_DIR/known-run.json"

if ((BOOT_SERVICE == 1)); then
  current_release() {
    readlink -- "$RELEASES_ROOT/current" 2>/dev/null || true
  }
  invocation() {
    systemctl show --property InvocationID --value "$UNIT"
  }
  # A start is over when the unit is active under a new invocation.
  await_unit() {
    local label=$1 previous_invocation=$2 deadline=$((SECONDS + DEPLOY_TIMEOUT))
    until [[ "$(systemctl is-active "$UNIT" || true)" == "active" && \
             "$(invocation)" != "$previous_invocation" ]]; do
      ((SECONDS < deadline)) || fail "[$label] the boot service did not start within ${DEPLOY_TIMEOUT}s"
      sleep 2
    done
  }
  write_deploy_env() {
    # The form the deployment guide prescribes: one single-quoted value a line.
    (
      umask 077
      {
        printf "COMPOSE_PROJECT_NAME='%s'\n" "$PROJECT"
        printf "SCRAPEYARD_PROXY_URL='%s'\n" "$SCRAPEYARD_PROXY_URL"
        printf "SCRAPEYARD_EGRESS_POLICY_ID='%s'\n" "$POLICY_ID"
        printf "SCRAPEYARD_API_CREDENTIALS='%s'\n" "$SCRAPEYARD_API_CREDENTIALS"
        printf "SCRAPEYARD_ENCRYPTION_KEYS='%s'\n" "$SCRAPEYARD_ENCRYPTION_KEYS"
        printf "SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID='%s'\n" "$SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID"
        printf '%s\n' "$@"
      } > "$ENV_DIR/deploy.env.new"
    )
    mv -f -- "$ENV_DIR/deploy.env.new" "$ENV_DIR/deploy.env"
  }
  promote() {
    local label=$1 release=$2
    echo "[$label] promoting $(manifest_value "$release" revision)..."
    # The settings come from the file alone, as they do in an operator's shell.
    env -u SCRAPEYARD_API_CREDENTIALS -u SCRAPEYARD_ENCRYPTION_KEYS \
      -u SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID -u SCRAPEYARD_PROXY_URL \
      -u SCRAPEYARD_EGRESS_POLICY_ID -u COMPOSE_PROJECT_NAME \
      "$MANAGE" promote "$release" > "$DIAGNOSTICS_DIR/$label-promote.log" 2>&1
  }

  echo "Installing the boot service as the deployment guide does..."
  BOOT_SERVICE_ACTIVE=1
  # Root runs a release at every boot, so the service refuses one that another
  # account owns. An operator gets this from extracting the archive as root.
  RELEASE_OWNER="$(stat -c '%u:%g' "$CANDIDATE")"
  chown -R 0:0 "$CANDIDATE" "$PREVIOUS"
  if [[ ! -d "$RELEASES_ROOT" ]]; then
    mkdir -p -- "$RELEASES_ROOT"
    RELEASES_ROOT_CREATED=1
  fi
  install -d -m 700 "$ENV_DIR"
  write_deploy_env
  ln -s -- "$CANDIDATE" "$RELEASES_ROOT/current"
  install -m 755 "$CANDIDATE/source/security/manage-release.sh" "$MANAGE"
  install -m 644 "$CANDIDATE/source/security/systemd/$UNIT" "$UNIT_FILE"
  systemctl daemon-reload
  BEFORE="$(invocation)"
  systemctl enable --now "$UNIT" > "$DIAGNOSTICS_DIR/boot-service-enable.log" 2>&1
  await_unit boot-service-start "$BEFORE"
  verify_runtime boot-service-start "$CANDIDATE" candidate
  verify_api boot-service-start --known "$WORK_DIR/known-run.json"

  echo "Promoting a corrupted release; the link and the running release must not move..."
  CORRUPTED="$(dirname "$CANDIDATE")/.rehearsal-corrupted.$$"
  cp -al "$CANDIDATE" "$CORRUPTED"
  rm -f -- "$CORRUPTED/source/docker-compose.yml"
  cp -- "$CANDIDATE/source/docker-compose.yml" "$CORRUPTED/source/docker-compose.yml"
  echo "# changed after the release was retained" >> "$CORRUPTED/source/docker-compose.yml"
  RUNNING_BEFORE="$APP_CONTAINER"
  if promote promote-corrupted "$CORRUPTED"; then
    fail "a release whose files changed was promoted"
  fi
  [[ "$(current_release)" == "$CANDIDATE" ]] || fail "a refused promotion moved the release link"
  [[ "$(service_containers scrapeyard)" == "$RUNNING_BEFORE" ]] && app_is_running || \
    fail "a refused promotion disturbed the running application"
  rm -rf -- "$CORRUPTED"
  CORRUPTED=""

  echo "Promoting a release another account could change; it must be refused before anything is stopped..."
  chown 65534:65534 "$PREVIOUS/source/security/deploy-secure-compose.sh"
  if promote promote-foreign-owner "$PREVIOUS"; then
    fail "a release that another account owns was promoted"
  fi
  chown 0:0 "$PREVIOUS/source/security/deploy-secure-compose.sh"
  [[ "$(current_release)" == "$CANDIDATE" && "$(service_containers scrapeyard)" == "$RUNNING_BEFORE" ]] && \
    app_is_running || fail "refusing a release by its owner disturbed the deployment"

  echo "Stopping a promotion halfway; it must be reported as failed, with the link unmoved..."
  promote promote-interrupted "$PREVIOUS" &
  PROMOTION=$!
  INTERRUPT_DEADLINE=$((SECONDS + 60))
  until [[ "$(systemctl is-active "$PROMOTE_UNIT" || true)" == "activating" ]]; do
    ((SECONDS < INTERRUPT_DEADLINE)) || fail "the promotion unit did not start"
    sleep 0.2
  done
  sleep 3
  systemctl stop "$PROMOTE_UNIT"
  if wait "$PROMOTION"; then
    fail "a promotion that was stopped halfway was reported as successful"
  fi
  [[ "$(current_release)" == "$CANDIDATE" ]] || fail "a stopped promotion moved the release link"
  # Wherever it was stopped, the application is running the promoted release
  # or it is stopped; it must not be running the release that was not promoted.
  APP_CONTAINER="$(service_containers scrapeyard)"
  if app_is_running; then
    [[ "$(docker inspect --format '{{.Image}}' "$APP_CONTAINER")" == \
       "$(manifest_value "$CANDIDATE" images.scrapeyard)" ]] || \
      fail "a stopped promotion left the unpromoted release running"
  fi
  # The documented way back: promote a retained release again.
  promote promote-after-interruption "$CANDIDATE"
  verify_runtime promote-after-interruption "$CANDIDATE" candidate
  verify_api promote-after-interruption --known "$WORK_DIR/known-run.json"

  echo "Restarting Docker; the boot service must bring the release back..."
  record_image_events
  BEFORE="$(invocation)"
  systemctl restart docker.service
  await_unit docker-restart "$BEFORE"
  verify_runtime docker-restart "$CANDIDATE" candidate
  verify_api docker-restart --known "$WORK_DIR/known-run.json"

  echo "Killing the Docker daemon; it must come back without waiting for the boot service's stop..."
  record_image_events
  # Docker's own unit allows three starts a minute, and this run makes three.
  systemctl reset-failed docker.service
  BEFORE="$(invocation)"
  CRASHED_AT=$SECONDS
  systemctl kill --signal=KILL --kill-whom=main docker.service
  until [[ "$(systemctl is-active docker.service || true)" == "active" ]] && docker info >/dev/null 2>&1; do
    ((SECONDS < CRASHED_AT + 60)) || fail "Docker was held down for a minute after its crash"
    sleep 1
  done
  echo "docker_back_after_seconds=$((SECONDS - CRASHED_AT))" > "$DIAGNOSTICS_DIR/docker-crash.txt"
  await_unit docker-crash "$BEFORE"
  verify_runtime docker-crash "$CANDIDATE" candidate
  verify_api docker-crash --known "$WORK_DIR/known-run.json"

  echo "Stopping Docker and removing what a reboot loses: the policy, the bridge hook and the AppArmor profile..."
  record_image_events
  systemctl reset-failed docker.service
  BEFORE="$(invocation)"
  systemctl stop docker.socket docker.service
  [[ "$(systemctl is-active "$UNIT" || true)" == "inactive" ]] || \
    fail "stopping Docker did not stop the boot service"
  remove_egress_policy
  [[ -z "$(policy_jumps)" ]] || fail "the egress policy could not be removed"
  # Unload the module where the kernel allows it; otherwise switch the hook off.
  if modprobe -r br_netfilter >/dev/null 2>&1; then
    echo "bridge_netfilter=module unloaded" > "$DIAGNOSTICS_DIR/cold-start.txt"
  else
    echo 0 > /proc/sys/net/bridge/bridge-nf-call-iptables
    echo "bridge_netfilter=hook switched off" > "$DIAGNOSTICS_DIR/cold-start.txt"
  fi
  "$CANDIDATE/source/security/install-chromium-apparmor-profile.sh" remove >/dev/null
  apparmor_profile_loaded && fail "the AppArmor profile could not be unloaded"
  # As at boot, nothing starts the boot service but Docker's own start.
  systemctl start docker.service
  await_unit cold-start "$BEFORE"
  verify_runtime cold-start "$CANDIDATE" candidate
  verify_api cold-start --known "$WORK_DIR/known-run.json"

  echo "Recreating the deployment network; the policy must follow the new bridge..."
  systemctl stop "$UNIT"
  app_is_running && fail "stopping the boot service left the application running"
  OLD_BRIDGE="$BRIDGE"
  project_resources container | xargs -r docker rm -f >/dev/null
  project_resources network | xargs -r docker network rm >/dev/null
  BEFORE="$(invocation)"
  systemctl start "$UNIT"
  await_unit network-recreated "$BEFORE"
  verify_runtime network-recreated "$CANDIDATE" candidate
  [[ "$BRIDGE" != "$OLD_BRIDGE" ]] || fail "the deployment network was not recreated"
  verify_api network-recreated --known "$WORK_DIR/known-run.json"

  echo "Breaking a deployment setting; the boot service must keep retrying, serve nothing, and recover once it is repaired..."
  mkdir -p -- "$UNIT_DROP_IN"
  # Only the pauses between attempts are shortened.
  printf '[Service]\nRestartSec=3\nRestartMaxDelaySec=6\n' > "$UNIT_DROP_IN/rehearsal.conf"
  systemctl daemon-reload
  write_deploy_env "SCRAPEYARD_EGRESS_ALLOW_CIDRS='not-a-network'"
  systemctl restart "$UNIT" >/dev/null 2>&1 || true
  RETRY_DEADLINE=$((SECONDS + DEPLOY_TIMEOUT))
  until (($(systemctl show --property NRestarts --value "$UNIT") >= 2)); do
    ((SECONDS < RETRY_DEADLINE)) || fail "the boot service did not retry a failed start"
    app_is_running && fail "the application ran although the host policy could not be installed"
    sleep 1
  done
  app_is_running && fail "the application ran although the host policy could not be installed"
  # No operator command follows: the next retry must succeed by itself.
  write_deploy_env
  RECOVERY_DEADLINE=$((SECONDS + DEPLOY_TIMEOUT))
  until [[ "$(systemctl is-active "$UNIT" || true)" == "active" ]]; do
    ((SECONDS < RECOVERY_DEADLINE)) || fail "the boot service did not recover after its setting was repaired"
    sleep 2
  done
  echo "retries_before_recovery=$(systemctl show --property NRestarts --value "$UNIT")" \
    > "$DIAGNOSTICS_DIR/boot-service-retries.txt"
  rm -rf -- "$UNIT_DROP_IN"
  systemctl daemon-reload
  verify_runtime settings-repaired "$CANDIDATE" candidate
  verify_api settings-repaired --known "$WORK_DIR/known-run.json"

  echo "Promoting the previous release as a rollback..."
  promote promote-rollback "$PREVIOUS"
  [[ "$(current_release)" == "$PREVIOUS" ]] || fail "the rollback did not select the previous release"
  verify_runtime promote-rollback "$PREVIOUS" previous
  verify_api promote-rollback --known "$WORK_DIR/known-run.json"

  echo "Starting the rolled-back release through the boot service, as the next boot would..."
  # The previous release may predate the boot service; the installed script
  # must start it all the same.
  BEFORE="$(invocation)"
  systemctl restart "$UNIT"
  await_unit rollback-boot "$BEFORE"
  verify_runtime rollback-boot "$PREVIOUS" previous
  verify_api rollback-boot --known "$WORK_DIR/known-run.json"
fi

# Promotion and rollback must work from the retained archives alone.
record_image_events
if grep -E -q '^pull ' "$DIAGNOSTICS_DIR/image-events.txt"; then
  fail "an image was pulled during the rehearsal"
fi

capture_diagnostics
echo "Requesting graceful Scrapeyard shutdown..."
docker stop -t 40 "$APP_CONTAINER" >/dev/null
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
