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

BRIDGE_HOOK_BEFORE=""
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
  for path in "$ENV_DIR" "$UNIT_FILE" "$UNIT_DROP_IN" "$MANAGE" "$RELEASES_ROOT/current" \
    "$RELEASES_ROOT/$(basename -- "$CANDIDATE")" "$RELEASES_ROOT/$(basename -- "$PREVIOUS")"; do
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
  # Read before anything is deployed: the first deployment switches the hook on.
  BRIDGE_HOOK_BEFORE="$(cat /proc/sys/net/bridge/bridge-nf-call-iptables 2>/dev/null || echo unloaded)"
fi

WORK_DIR=""
BOOT_SERVICE_ACTIVE=0
RELEASES_ROOT_CREATED=0
CANDIDATE_OWNER=""
PREVIOUS_OWNER=""
BOOT_CANDIDATE=""
BOOT_PREVIOUS=""
UNTRUSTED=""
PROMOTE_UNIT="scrapeyard-promote.service"
CORRUPTED=""
APP_CONTAINER=""
API_URL=""
BRIDGE=""
PROBE_ADDRESS=""
ROUTE_ACTIVE=0
HOST_LISTENER_PID=""
HOST_PROBE_PORT=18999
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

# The jumps that refuse the application's connections to the host itself.
host_policy_jumps() {
  iptables -S INPUT | grep -E -- " -j SY-HOST-$POLICY_ID-[AB]\$" || true
}

# Removes the rehearsal's policy from every interface it is installed on. The
# interfaces come from the jumps themselves: the deployment bridge changes when
# the network is recreated, and one step installs the policy somewhere else.
remove_egress_policy() {
  local interface
  for interface in $({ policy_jumps; host_policy_jumps; } | \
    sed -n 's/^-A [A-Z-]* -i \([^ ]*\) -j .*/\1/p' | sort -u); do
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
    rm -rf -- "$UNIT_FILE" "$UNIT_DROP_IN" "$ENV_DIR" "$MANAGE" "$RELEASES_ROOT/current" \
      "$RELEASES_ROOT/current.new"
    systemctl daemon-reload >/dev/null 2>&1
    systemctl reset-failed "$UNIT" >/dev/null 2>&1
    # A step that stopped Docker may have failed before starting it again,
    # and nothing below can be removed without it.
    systemctl start docker.service >/dev/null 2>&1
    # The copies share their files with the releases, so the files' owners
    # go back to what each release had.
    [[ -z "$CORRUPTED" ]] || rm -rf -- "$CORRUPTED"
    [[ -z "$UNTRUSTED" ]] || rm -rf -- "$UNTRUSTED"
    [[ -z "$BOOT_CANDIDATE" ]] || rm -rf -- "$BOOT_CANDIDATE"
    [[ -z "$BOOT_PREVIOUS" ]] || rm -rf -- "$BOOT_PREVIOUS"
    [[ -z "$CANDIDATE_OWNER" ]] || chown -R "$CANDIDATE_OWNER" "$CANDIDATE" >/dev/null 2>&1
    [[ -z "$PREVIOUS_OWNER" ]] || chown -R "$PREVIOUS_OWNER" "$PREVIOUS" >/dev/null 2>&1
    # The simulated reboot unloads the bridge hook, and every deployment
    # switches it on. Put it back as it was before the run; a module the
    # kernel will not unload stays loaded.
    if [[ "$BRIDGE_HOOK_BEFORE" == "unloaded" ]]; then
      modprobe -r br_netfilter >/dev/null 2>&1
    elif [[ -n "$BRIDGE_HOOK_BEFORE" ]]; then
      modprobe br_netfilter >/dev/null 2>&1
      echo "$BRIDGE_HOOK_BEFORE" > /proc/sys/net/bridge/bridge-nf-call-iptables
    fi
    ((RELEASES_ROOT_CREATED == 0)) || rmdir -- "$RELEASES_ROOT" >/dev/null 2>&1
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
  [[ -z "$HOST_LISTENER_PID" ]] || kill "$HOST_LISTENER_PID" >/dev/null 2>&1
  HOST_LISTENER_PID=""
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
[[ -z "$(policy_jumps)$(host_policy_jumps)" ]] || \
  fail "a rehearsal egress policy is already installed on this host"
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
# A listener on every address of the host stands in for whatever a production
# host listens on. The application must not be able to reach it.
(exec 3<>"/dev/tcp/127.0.0.1/$HOST_PROBE_PORT") >/dev/null 2>&1 && \
  fail "port $HOST_PROBE_PORT is already in use"
python3 -c '
import socket, sys
server = socket.create_server(("0.0.0.0", int(sys.argv[1])))
while True:
    server.accept()[0].close()
' "$HOST_PROBE_PORT" &
HOST_LISTENER_PID=$!
HOST_ADDRESS="$(ip -4 route get 1.1.1.1 | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
[[ -n "$HOST_ADDRESS" ]] || fail "could not find the host's network address"
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

# Started by Docker with no host egress policy, the application must report
# the reachable challenge and never serve. Its container is left stopped. The
# container is replaced by the next deployment, so its log is kept as evidence.
expect_refusal_without_policy() {
  local label=$1 since deadline
  remove_egress_policy
  [[ -z "$(policy_jumps)$(host_policy_jumps)" ]] || fail "the egress policy could not be removed"
  since=$(date +%s)
  docker start "$APP_CONTAINER" >/dev/null
  deadline=$((SECONDS + 60))
  until docker logs --since "$since" "$APP_CONTAINER" > "$DIAGNOSTICS_DIR/$label.log" 2>&1 && \
    grep -q 'controlled non-public challenge was reachable' "$DIAGNOSTICS_DIR/$label.log"; do
    if curl --silent --output /dev/null --max-time 2 "$API_URL/health"; then
      fail "[$label] the application served without the host egress policy"
    fi
    ((SECONDS < deadline)) || fail "[$label] the application did not report the missing egress policy"
    sleep 1
  done
  # It is restarted by Docker until the policy is back; it must never serve.
  for _ in 1 2 3 4 5; do
    if curl --silent --output /dev/null --max-time 2 "$API_URL/health"; then
      fail "[$label] the application served without the host egress policy"
    fi
    sleep 1
  done
  docker stop -t 40 "$APP_CONTAINER" >/dev/null
}

# Everything a deployment must leave true, checked from outside the application.
verify_runtime() {
  local label=$1 release=$2 name=$3 service container network_id gateway address
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
  # A probe health check that needs most of its three seconds here misses
  # them on slower cores, and then nothing starts (2026-10-11). The previous
  # release may predate the cheaper check, so only the candidate is timed.
  if [[ "$name" == candidate ]]; then
    docker inspect --format '{{json .State.Health.Log}}' "$(service_containers egress-probe)" | \
      python3 tests/rehearsal/probe_health.py > "$DIAGNOSTICS_DIR/$label-probe-health-seconds" || \
      fail "[$label] the probe's health check is too slow for a slower host"
  fi
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
  # The host's own addresses are input to the host, which the forwarded
  # policy never sees. A release whose installer has host rules must refuse
  # them; an older one is not held to it.
  gateway="$(docker inspect --format \
    '{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}' "$APP_CONTAINER")"
  (exec 3<>"/dev/tcp/$gateway/$HOST_PROBE_PORT") >/dev/null 2>&1 || \
    fail "[$label] the host's probe listener is not answering on $gateway"
  if grep -q 'SY-HOST-' "$release/source/security/install-docker-egress-policy.sh"; then
    (($(host_policy_jumps | grep -c -F -- "-i $BRIDGE ") == 1 && $(host_policy_jumps | wc -l) == 1)) || \
      fail "[$label] expected one host policy jump, on $BRIDGE"
    for address in "$gateway" "$HOST_ADDRESS"; do
      if app_connects "$address" "$HOST_PROBE_PORT"; then
        fail "[$label] the application reached the host's own address $address"
      fi
    done
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

echo "Deploying without a proxy while submissions are untrusted; it must be refused before anything is stopped..."
RUNNING_BEFORE="$APP_CONTAINER"
if deploy no-proxy "$CANDIDATE" SCRAPEYARD_PROXY_URL=; then
  fail "a deployment of untrusted submissions without an operator proxy went ahead"
fi
grep -q -F 'must name an egress-filtering operator proxy' "$DIAGNOSTICS_DIR/no-proxy-deploy.log" || \
  fail "the deployment without a proxy was refused for another reason"
[[ "$(service_containers scrapeyard)" == "$RUNNING_BEFORE" ]] && app_is_running || \
  fail "refusing the deployment without a proxy disturbed the running application"

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
expect_refusal_without_policy unprotected-start

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
  running_application() {
    docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
      --filter "label=com.docker.compose.service=scrapeyard"
  }
  await_promotion_under_way() {
    local deadline=$((SECONDS + 60))
    until [[ "$(systemctl is-active "$PROMOTE_UNIT" || true)" == "activating" ]]; do
      ((SECONDS < deadline)) || fail "the promotion unit did not start"
      sleep 0.2
    done
  }
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
  # Most of this phase runs with an operator proxy set, because the previous
  # release may predate deployments without one. The last steps switch to none.
  EGRESS="proxy"
  write_deploy_env() {
    # The form the deployment guide prescribes: one single-quoted value a line.
    (
      umask 077
      {
        printf "COMPOSE_PROJECT_NAME='%s'\n" "$PROJECT"
        if [[ "$EGRESS" == "direct" ]]; then
          # No operator proxy: the one submitter is the operator's own service.
          printf "SCRAPEYARD_UNTRUSTED_SUBMISSIONS='false'\n"
        else
          printf "SCRAPEYARD_PROXY_URL='%s'\n" "$SCRAPEYARD_PROXY_URL"
        fi
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

  # With no proxy in the settings file, the running application must be the
  # one it describes, and must have checked the host egress policy itself.
  # A start that reuses its container leaves the earlier starts' lines in the
  # log, so a start is judged by what was logged since it began.
  verify_direct_egress() {
    local label=$1 since=${2:-0}
    python3 - "$WORK_DIR/app-inspect.json" <<'PY' || fail "[$label] the application does not run with the settings file's proxy and submission mode"
import json
import sys

env = dict(item.split("=", 1) for item in json.load(open(sys.argv[1], encoding="utf-8"))[0]["Config"]["Env"])
sys.exit(
    0 if env.get("SCRAPEYARD_UNTRUSTED_SUBMISSIONS") == "false" and not env.get("SCRAPEYARD_PROXY_URL") else 1
)
PY
    docker logs --since "$since" "$APP_CONTAINER" > "$WORK_DIR/attestation.log" 2>&1
    grep -q 'Connected-IP egress policy attested' "$WORK_DIR/attestation.log" || \
      fail "[$label] the application started without checking the host egress policy"
  }

  echo "Installing the boot service as the deployment guide does..."
  BOOT_SERVICE_ACTIVE=1
  if [[ ! -d "$RELEASES_ROOT" ]]; then
    install -d -m 755 -o 0 -g 0 "$RELEASES_ROOT"
    RELEASES_ROOT_CREATED=1
  fi
  # Root runs a release at every boot, so the service refuses one that another
  # account owns or could replace. An operator gets root's ownership from
  # extracting the archive as root; here the releases are copied to where
  # they would be extracted. The copies are hard links where that is possible.
  CANDIDATE_OWNER="$(stat -c '%u:%g' "$CANDIDATE")"
  PREVIOUS_OWNER="$(stat -c '%u:%g' "$PREVIOUS")"
  BOOT_CANDIDATE="$RELEASES_ROOT/$(basename -- "$CANDIDATE")"
  BOOT_PREVIOUS="$RELEASES_ROOT/$(basename -- "$PREVIOUS")"
  copy_release() {
    # A hard-link copy that fails across filesystems leaves a skeleton behind,
    # which a plain copy would then nest the release into.
    if ! cp -al "$1" "$2" 2>/dev/null; then
      rm -rf -- "$2"
      cp -a "$1" "$2"
    fi
  }
  copy_release "$CANDIDATE" "$BOOT_CANDIDATE"
  copy_release "$PREVIOUS" "$BOOT_PREVIOUS"
  chown -R 0:0 "$BOOT_CANDIDATE" "$BOOT_PREVIOUS"
  install -d -m 700 "$ENV_DIR"
  write_deploy_env
  ln -s -- "$BOOT_CANDIDATE" "$RELEASES_ROOT/current"
  install -m 755 "$BOOT_CANDIDATE/source/security/manage-release.sh" "$MANAGE"
  install -m 644 "$BOOT_CANDIDATE/source/security/systemd/$UNIT" "$UNIT_FILE"
  systemctl daemon-reload
  BEFORE="$(invocation)"
  systemctl enable --now "$UNIT" > "$DIAGNOSTICS_DIR/boot-service-enable.log" 2>&1
  await_unit boot-service-start "$BEFORE"
  verify_runtime boot-service-start "$BOOT_CANDIDATE" candidate
  verify_api boot-service-start --known "$WORK_DIR/known-run.json"

  echo "Promoting a corrupted release; the link and the running release must not move..."
  CORRUPTED="$(dirname "$BOOT_CANDIDATE")/.rehearsal-corrupted.$$"
  cp -al "$BOOT_CANDIDATE" "$CORRUPTED"
  rm -f -- "$CORRUPTED/source/docker-compose.yml"
  cp -- "$BOOT_CANDIDATE/source/docker-compose.yml" "$CORRUPTED/source/docker-compose.yml"
  echo "# changed after the release was retained" >> "$CORRUPTED/source/docker-compose.yml"
  RUNNING_BEFORE="$APP_CONTAINER"
  if promote promote-corrupted "$CORRUPTED"; then
    fail "a release whose files changed was promoted"
  fi
  [[ "$(current_release)" == "$BOOT_CANDIDATE" ]] || fail "a refused promotion moved the release link"
  [[ "$(service_containers scrapeyard)" == "$RUNNING_BEFORE" ]] && app_is_running || \
    fail "a refused promotion disturbed the running application"
  rm -rf -- "$CORRUPTED"
  CORRUPTED=""

  echo "Promoting a release another account could change; it must be refused before anything is stopped..."
  chown 65534:65534 "$BOOT_PREVIOUS/source/security/deploy-secure-compose.sh"
  if promote promote-foreign-owner "$BOOT_PREVIOUS"; then
    fail "a release that another account owns was promoted"
  fi
  chown 0:0 "$BOOT_PREVIOUS/source/security/deploy-secure-compose.sh"
  [[ "$(current_release)" == "$BOOT_CANDIDATE" && "$(service_containers scrapeyard)" == "$RUNNING_BEFORE" ]] && \
    app_is_running || fail "refusing a release by its owner disturbed the deployment"

  if [[ -n "$(find "$(dirname -- "$PREVIOUS")" -maxdepth 0 \( ! -user 0 -o -perm /022 \) -print)" ]]; then
    echo "Promoting a release from a directory another account can write to; it must be refused..."
    # Root owns every file and directory of this copy. Only the directory
    # that holds it is someone else's, and that someone could swap the copy.
    UNTRUSTED="$(dirname -- "$PREVIOUS")/.rehearsal-untrusted.$$"
    copy_release "$BOOT_PREVIOUS" "$UNTRUSTED"
    if promote promote-untrusted-directory "$UNTRUSTED"; then
      fail "a release that another account could replace was promoted"
    fi
    grep -q -F 'so what it holds cannot be trusted' \
      "$DIAGNOSTICS_DIR/promote-untrusted-directory-promote.log" || \
      fail "the release in an untrusted directory was refused for another reason"
    [[ "$(current_release)" == "$BOOT_CANDIDATE" ]] || fail "a refused promotion moved the release link"
    rm -rf -- "$UNTRUSTED"
    UNTRUSTED=""
  fi

  echo "Stopping a promotion halfway; it must be reported as failed, with the link unmoved..."
  promote promote-interrupted "$BOOT_PREVIOUS" &
  PROMOTION=$!
  await_promotion_under_way
  sleep 3
  systemctl stop "$PROMOTE_UNIT"
  if wait "$PROMOTION"; then
    fail "a promotion that was stopped halfway was reported as successful"
  fi
  [[ "$(current_release)" == "$BOOT_CANDIDATE" ]] || fail "a stopped promotion moved the release link"
  # Wherever it was stopped, the application is running the promoted release
  # or it is stopped; it must not be running the release that was not promoted.
  APP_CONTAINER="$(service_containers scrapeyard)"
  if app_is_running; then
    [[ "$(docker inspect --format '{{.Image}}' "$APP_CONTAINER")" == \
       "$(manifest_value "$BOOT_CANDIDATE" images.scrapeyard)" ]] || \
      fail "a stopped promotion left the unpromoted release running"
  fi
  # The documented way back: promote a retained release again.
  promote promote-after-interruption "$BOOT_CANDIDATE"
  verify_runtime promote-after-interruption "$BOOT_CANDIDATE" candidate
  verify_api promote-after-interruption --known "$WORK_DIR/known-run.json"

  echo "Stopping a promotion after it has stopped the application; it must end stopped and say so..."
  promote promote-interrupted-late "$BOOT_PREVIOUS" &
  PROMOTION=$!
  await_promotion_under_way
  LATE_DEADLINE=$((SECONDS + 120))
  while [[ -n "$(running_application)" ]]; do
    ((SECONDS < LATE_DEADLINE)) || fail "the promotion never stopped the application"
    sleep 0.2
  done
  systemctl stop "$PROMOTE_UNIT"
  if wait "$PROMOTION"; then
    fail "a promotion that was stopped halfway was reported as successful"
  fi
  [[ "$(current_release)" == "$BOOT_CANDIDATE" ]] || fail "a stopped promotion moved the release link"
  [[ -z "$(running_application)" ]] || fail "a promotion stopped after the application was stopped left it running"
  # systemd 255 hands control back while the interrupted deployment is still
  # stopping the application; the verdict must be the state after that.
  grep -q -F 'the application is stopped; promote a retained release to start it' \
    "$DIAGNOSTICS_DIR/promote-interrupted-late-promote.log" || \
    fail "the stopped promotion did not report that the application is stopped"
  # A new promotion must be possible at once.
  promote promote-after-late-interruption "$BOOT_CANDIDATE"
  verify_runtime promote-after-late-interruption "$BOOT_CANDIDATE" candidate
  verify_api promote-after-late-interruption --known "$WORK_DIR/known-run.json"

  echo "Starting the boot service while a promotion is under way; it must start what was promoted..."
  systemctl stop "$UNIT"
  promote promote-during-start "$BOOT_PREVIOUS" &
  PROMOTION=$!
  await_promotion_under_way
  sleep 1
  # This start waits for the promotion. It must then read the link again:
  # deploying the release the link named before the wait would bring the
  # candidate back after the promotion had reported the previous release.
  BEFORE="$(invocation)"
  systemctl start "$UNIT"
  wait "$PROMOTION" || fail "a promotion failed because a boot start was waiting for it"
  await_unit start-after-promotion "$BEFORE"
  [[ "$(current_release)" == "$BOOT_PREVIOUS" ]] || fail "the promotion did not select the previous release"
  verify_runtime start-after-promotion "$BOOT_PREVIOUS" previous
  verify_api start-after-promotion --known "$WORK_DIR/known-run.json"
  promote promote-candidate-again "$BOOT_CANDIDATE"
  verify_runtime promote-candidate-again "$BOOT_CANDIDATE" candidate
  verify_api promote-candidate-again --known "$WORK_DIR/known-run.json"

  echo "Restarting Docker; the boot service must bring the release back..."
  record_image_events
  BEFORE="$(invocation)"
  systemctl restart docker.service
  await_unit docker-restart "$BEFORE"
  verify_runtime docker-restart "$BOOT_CANDIDATE" candidate
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
  verify_runtime docker-crash "$BOOT_CANDIDATE" candidate
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
  "$BOOT_CANDIDATE/source/security/install-chromium-apparmor-profile.sh" remove >/dev/null
  apparmor_profile_loaded && fail "the AppArmor profile could not be unloaded"
  # As at boot, nothing starts the boot service but Docker's own start.
  systemctl start docker.service
  await_unit cold-start "$BEFORE"
  verify_runtime cold-start "$BOOT_CANDIDATE" candidate
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
  verify_runtime network-recreated "$BOOT_CANDIDATE" candidate
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
  verify_runtime settings-repaired "$BOOT_CANDIDATE" candidate
  verify_api settings-repaired --known "$WORK_DIR/known-run.json"

  echo "Promoting the previous release as a rollback..."
  promote promote-rollback "$BOOT_PREVIOUS"
  [[ "$(current_release)" == "$BOOT_PREVIOUS" ]] || fail "the rollback did not select the previous release"
  verify_runtime promote-rollback "$BOOT_PREVIOUS" previous
  verify_api promote-rollback --known "$WORK_DIR/known-run.json"

  echo "Starting the rolled-back release through the boot service, as the next boot would..."
  # The previous release may predate the boot service; the installed script
  # must start it all the same.
  BEFORE="$(invocation)"
  systemctl restart "$UNIT"
  await_unit rollback-boot "$BEFORE"
  verify_runtime rollback-boot "$BOOT_PREVIOUS" previous
  verify_api rollback-boot --known "$WORK_DIR/known-run.json"

  echo "Switching the settings to no operator proxy and promoting the candidate with them..."
  EGRESS="direct"
  write_deploy_env
  promote promote-no-proxy "$BOOT_CANDIDATE"
  [[ "$(current_release)" == "$BOOT_CANDIDATE" ]] || fail "the promotion without a proxy did not select the candidate"
  verify_runtime promote-no-proxy "$BOOT_CANDIDATE" candidate
  verify_direct_egress promote-no-proxy
  verify_api promote-no-proxy --known "$WORK_DIR/known-run.json"

  echo "Starting the application without the egress policy and without a proxy; it must still refuse to serve..."
  systemctl stop "$UNIT"
  expect_refusal_without_policy unprotected-start-no-proxy
  # The boot service then starts it as it would after a boot.
  BEFORE="$(invocation)"
  # Not STARTED_AT: that is the whole run's clock.
  BOOT_STARTED_EPOCH="$(date +%s)"
  systemctl start "$UNIT"
  await_unit boot-no-proxy "$BEFORE"
  verify_runtime boot-no-proxy "$BOOT_CANDIDATE" candidate
  verify_direct_egress boot-no-proxy "$BOOT_STARTED_EPOCH"
  verify_api boot-no-proxy --known "$WORK_DIR/known-run.json"
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
[[ -z "$(policy_jumps)$(host_policy_jumps)" ]] || fail "cleanup left the rehearsal egress policy installed"
rm -rf -- "$WORK_DIR"
WORK_DIR=""
trap - EXIT INT TERM HUP

echo "Deployment rehearsal passed in $((SECONDS - STARTED_AT))s: candidate deployed, corrupted release refused,"
echo "policy failures left nothing serving, and rollback and roll-forward kept the known run."
echo "Evidence: $DIAGNOSTICS_DIR"
