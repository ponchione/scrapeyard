#!/usr/bin/env bash

set -euo pipefail

LOCK_DIR=/run/scrapeyard

fail() {
  echo "scrapeyard secure compose deployment: $*" >&2
  exit 2
}

[[ $# -eq 0 || ( $# -eq 2 && "$1" == "--release" ) ]] || \
  fail "usage: $0 [--release DIRECTORY]"

((EUID == 0)) || fail "run as root so the host egress policy can be installed"
command -v docker >/dev/null 2>&1 || fail "docker is required"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
# Compose defaults to untrusted submissions, which the application accepts
# only behind an operator proxy that filters destinations. A deployment whose
# only submitter is its operator's own service may set
# SCRAPEYARD_UNTRUSTED_SUBMISSIONS=false and fetch directly.
if [[ "${SCRAPEYARD_UNTRUSTED_SUBMISSIONS:-true}" != "false" ]]; then
  [[ -n "${SCRAPEYARD_PROXY_URL:-}" && "$SCRAPEYARD_PROXY_URL" != "direct" ]] || \
    fail "SCRAPEYARD_PROXY_URL must name an egress-filtering operator proxy, unless SCRAPEYARD_UNTRUSTED_SUBMISSIONS is false"
fi
# With or without a proxy, the host egress policy is the boundary for browser
# traffic. The application checks it at startup only while its probe is
# configured, and counts a challenge that times out as refused. The Compose
# file fixes the probe's address and ports, so a deployment has no reason to
# set them, and a timeout too short for a connection to complete would let
# the check pass with no policy at all.
for setting in SCRAPEYARD_EGRESS_POLICY_PROBE_HOST SCRAPEYARD_EGRESS_POLICY_PROBE_PORT \
  SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT; do
  [[ -z "${!setting:-}" ]] || fail "$setting is fixed by the Compose file; unset it"
done
[[ "${SCRAPEYARD_EGRESS_POLICY_PROBE_TIMEOUT_SECONDS:-1}" =~ ^[1-9][0-9]*(\.[0-9]+)?$ ]] || \
  fail "SCRAPEYARD_EGRESS_POLICY_PROBE_TIMEOUT_SECONDS must be a number of seconds, 1 or more"

# One deployment at a time. A promotion that overlaps the boot service's start
# would interleave their stop, policy and start steps.
install -d -m 700 "$LOCK_DIR"
exec 9>>"$LOCK_DIR/deploy.lock"
flock --wait 600 9 || fail "another deployment has held $LOCK_DIR/deploy.lock for ten minutes"

# Building cannot expose application traffic and makes the later application
# start independent of Compose's implicit missing-image behavior.
start_options=()
if [[ "${1:-}" == "--release" ]]; then
  [[ -n "${COMPOSE_PROJECT_NAME:-}" ]] || fail "set the existing COMPOSE_PROJECT_NAME to preserve volumes"
  # Host policy imports must leave the checksummed source tree unchanged.
  export PYTHONDONTWRITEBYTECODE=1
  release_dir=$(realpath -- "$2")
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  python3 "$script_dir/../scripts/release_artifact.py" load "$release_dir"
  cd "$release_dir/source"
  export COMPOSE_FILE="$release_dir/source/docker-compose.yml:$release_dir/images.json"
  # Retained releases must never download or rebuild during promotion/rollback.
  start_options=(--no-build --pull never)
  mapfile -t images < <(docker compose config --images)
  ((${#images[@]} > 0)) || fail "Compose declares no images"
  for image in "${images[@]}"; do
    docker image inspect "$image" >/dev/null 2>&1 || fail "required image is missing: $image"
  done
else
  docker compose build scrapeyard
fi

# Removes a container that Docker could not stop. A removal can fail the same
# way as the stop while a start is under way, so it is tried again; with the
# restart policy off, no further start follows it. A container that still
# cannot be removed gets its policy back: left without one, it would be
# started by a later deployment as a container that no longer restarts. For
# the same reason the policy is switched off only once it has been read, and
# the whole exchange ignores signals.
remove_container() {
  local container=$1
  (
    trap '' INT TERM HUP
    policy=$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$container" 2>/dev/null) || policy=""
    [[ -z "$policy" ]] || docker update --restart=no "$container" >/dev/null 2>&1 || true
    for _ in 1 2 3; do
      docker rm --force "$container" >/dev/null 2>&1 || true
      if remaining=$(docker ps --all --quiet --filter "id=$container") && [[ -z "$remaining" ]]; then
        exit 0
      fi
      sleep 1
    done
    if [[ -n "$policy" && "$policy" != "no" ]]; then
      docker update --restart="$policy" "$container" >/dev/null 2>&1 || \
        echo "scrapeyard secure compose deployment: the container's restart policy ($policy) could not be put back" >&2
    fi
    exit 1
  )
}

# Leaves no application container that can run. `docker compose stop` settles
# one that is running, or that exits by itself and is restarted. Docker cannot
# stop a container whose every start dies before it is up (2026-10-11, runc
# 1.3.3 under a seccomp profile it could not start with): the stop reports
# "did not receive an exit event" and the restarts go on. A container of the
# service that is not `exited` after the stop is therefore removed, and the
# next start creates it again. Its last output goes to a file only root can
# read, not to this script's output: a start that fails on a setting can print
# part of the setting. A container made with `docker compose run` is the
# operator's and is left alone, as the stop leaves it.
stop_application() {
  local containers container state failed=0
  docker compose stop scrapeyard >/dev/null 2>&1 || true
  containers=$(docker compose ps --all --quiet scrapeyard) || return 1
  for container in $containers; do
    state=$(docker inspect --format \
      '{{index .Config.Labels "com.docker.compose.oneoff"}} {{.State.Status}}' "$container" 2>/dev/null) || \
      state="False unknown"
    case "$state" in
      "False exited" | "True "*) continue ;;
    esac
    install -m 600 /dev/null "$LOCK_DIR/removed-container.log" && \
      docker logs --tail 50 "$container" 2>&1 | head -c 65536 >> "$LOCK_DIR/removed-container.log" || true
    echo "scrapeyard secure compose deployment: the application's container is ${state#* }, not stopped, and is being removed; its last output is in $LOCK_DIR/removed-container.log" >&2
    remove_container "$container" || failed=1
  done
  return "$failed"
}

# A redeploy must not leave an already-running untrusted-submission service
# online if host-policy replacement encounters an unexpected tool failure.
stop_application || fail "the application's container could not be stopped or removed"

# From here on the application is down, and whatever fails or interrupts the
# rest must leave it down. Its container restarts by itself (unless-stopped),
# so one left behind by a failed start could come up later, outside any
# deployment.
leave_stopped() {
  local status=$?
  # Stopping can take a minute and a half, and an interrupted stop leaves the
  # container restarting with nothing said.
  trap '' INT TERM HUP
  trap - EXIT
  if ((status != 0)); then
    if stop_application; then
      echo "scrapeyard secure compose deployment: failed; the application is left stopped" >&2
    else
      echo "scrapeyard secure compose deployment: failed, and the application's container could not be stopped or removed; see 'docker ps --all'" >&2
    fi
  fi
  exit "$status"
}
trap leave_stopped EXIT
trap 'exit 143' INT TERM HUP

# Start only dependencies first. The application is not allowed to start until
# the connected-IP policy exists on the deployment bridge.
docker compose up -d --wait "${start_options[@]}" egress-probe redis

probe_container=$(docker compose ps -q egress-probe)
[[ -n "$probe_container" ]] || fail "egress-probe container was not created"
network_id=$(docker inspect --format \
  '{{range .NetworkSettings.Networks}}{{.NetworkID}}{{end}}' \
  "$probe_container")
[[ "$network_id" =~ ^[a-f0-9]{12,64}$ ]] || fail "could not determine Compose network ID"
bridge_name=$(docker network inspect --format \
  '{{index .Options "com.docker.network.bridge.name"}}' \
  "$network_id")
bridge_name=${bridge_name:-br-${network_id:0:12}}

export SCRAPEYARD_EGRESS_INTERFACE=${SCRAPEYARD_EGRESS_INTERFACE:-$bridge_name}
export SCRAPEYARD_EGRESS_POLICY_ID=${SCRAPEYARD_EGRESS_POLICY_ID:-prod}
security/install-docker-egress-policy.sh install
security/install-chromium-apparmor-profile.sh install

# The app's lifespan verifies the narrowly allowed probe liveness channel
# before and after the host rule rejects its separate challenge listener.
docker compose up -d --wait "${start_options[@]}"

echo "Scrapeyard started with connected-IP egress policy attested"
