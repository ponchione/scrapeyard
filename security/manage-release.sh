#!/usr/bin/env bash

set -euo pipefail

ENV_FILE=/etc/scrapeyard/deploy.env
CURRENT=/srv/scrapeyard-releases/current
LOCK_DIR=/run/scrapeyard
PROMOTE_UNIT=scrapeyard-promote
TRUSTED_OWNER=0
TRUSTED_ROOT=/

fail() {
  echo "scrapeyard release: $*" >&2
  exit 2
}

usage() {
  cat >&2 <<'USAGE'
Usage: scrapeyard-release promote RELEASE_DIRECTORY
       scrapeyard-release start | stop    (run by scrapeyard.service)

Installed as /usr/local/sbin/scrapeyard-release. promote deploys a retained
release with the settings in /etc/scrapeyard/deploy.env and, once it is
running, makes it the release the boot service starts.
USAGE
  exit 2
}

require_private_settings() {
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "$ENV_FILE is missing or is a link"
  [[ "$(stat -c '%u %a' "$ENV_FILE")" == "$TRUSTED_OWNER 600" ]] || \
    fail "$ENV_FILE must be owned by root with mode 600"
}

require_project_name() {
  [[ "${COMPOSE_PROJECT_NAME:-}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || \
    fail "$ENV_FILE must set COMPOSE_PROJECT_NAME to the deployment's Compose project"
}

# The settings hold credentials, so they are read here and never handed to
# systemd, which writes a line it cannot parse to the journal, value included.
# A line that is not NAME='value' is refused by its number alone. The only
# COMPOSE_ name accepted is the project: COMPOSE_ENV_FILES would have Compose
# read a second file and print what it cannot parse.
load_settings() {
  local line name number=0
  require_private_settings
  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))
    [[ -n "$line" && "$line" != \#* ]] || continue
    if [[ "$line" =~ ^(SCRAPEYARD_[A-Z0-9_]+|COMPOSE_PROJECT_NAME)=\'([^\']*)\'$ ]]; then
      name=${BASH_REMATCH[1]}
      export "$name=${BASH_REMATCH[2]}"
    else
      fail "$ENV_FILE line $number is not a SCRAPEYARD_ setting or COMPOSE_PROJECT_NAME written as NAME='value'"
    fi
  done <"$ENV_FILE"
  require_project_name
}

# Stopping needs the project name and nothing else. A mistyped line elsewhere
# in the file must not keep the application from being stopped.
load_project_name() {
  local line
  require_private_settings
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^COMPOSE_PROJECT_NAME=\'([^\']*)\'$ ]]; then
      COMPOSE_PROJECT_NAME=${BASH_REMATCH[1]}
    fi
  done <"$ENV_FILE"
  require_project_name
}

# Whoever can write to a directory can replace what is in it, so every
# directory above a path must be root's alone, up to the filesystem root.
# Both the path as it is written and the path it resolves to are walked: a
# directory that only holds a link on the way is as good as the link's target
# to whoever can replace the link.
check_ancestors() {
  local start path offender
  for start in "$(dirname -- "$1")" "$(realpath -- "$(dirname -- "$1")")"; do
    path=$start
    while :; do
      # -H: judge the directory a link on the way points to, not the link.
      offender=$(find -H "$path" -maxdepth 0 \( ! -user "$TRUSTED_OWNER" -o -perm /022 \) -print)
      [[ -z "$offender" ]] || \
        fail "$offender is not owned by root, or others can write to it, so what it holds cannot be trusted"
      [[ "$path" != "$TRUSTED_ROOT" && "$path" != / ]] || break
      path=$(dirname -- "$path")
    done
  done
}

# Root runs this tree at every boot, so no other account may be able to change it.
check_release() {
  local release=$1 offender
  [[ -f "$release/manifest.json" && -x "$release/source/security/deploy-secure-compose.sh" ]] || \
    fail "not a retained release: $release"
  offender=$(find "$release" \( ! -user "$TRUSTED_OWNER" -o -perm /022 \) -print -quit)
  [[ -z "$offender" ]] || \
    fail "$offender is not owned by root, or others can write to it; extract releases as root with tar --no-same-owner"
  check_ancestors "$release"
}

# One start or promotion at a time, from before the link is read until after
# it is moved. A start that had to wait must deploy the release the link names
# afterwards, not the one it named when the wait began.
take_release_lock() {
  install -d -m 700 "$LOCK_DIR"
  exec 7>>"$LOCK_DIR/release.lock"
  flock --wait 300 7 || fail "another start or promotion has held $LOCK_DIR/release.lock for five minutes"
}

deploy() {
  "$1/source/security/deploy-secure-compose.sh" --release "$1"
}

application_containers() {
  timeout 20 docker ps -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter "label=com.docker.compose.service=scrapeyard"
}

stop_application() {
  local containers
  containers=$(application_containers) || return 1
  # Without --time, the container's own grace period applies.
  # shellcheck disable=SC2086  # one container ID a word
  [[ -z "$containers" ]] || timeout 90 docker stop $containers >/dev/null
}

promotion_is_under_way() {
  case "$(systemctl is-active "$PROMOTE_UNIT.service" 2>/dev/null || true)" in
    active|activating|deactivating|reloading) return 0 ;;
    *) return 1 ;;
  esac
}

# What an operator has to know after a promotion that did not complete: what
# the boot service starts, and what is running now.
describe_outcome() {
  local release=$1 promoted containers image running
  promoted=$(readlink -- "$CURRENT" 2>/dev/null || true)
  if ! containers=$(application_containers 2>/dev/null); then
    running="Docker did not answer, so the application's state is unknown"
  elif [[ -z "$containers" ]]; then
    running="the application is stopped; promote a retained release to start it"
  else
    # shellcheck disable=SC2086
    image=$(docker inspect --format '{{.Image}}' $containers 2>/dev/null | head -n 1)
    if [[ -z "$image" ]] || ! grep -q -F -- "\"scrapeyard\": \"$image\"" "$release/manifest.json"; then
      running="the application is still running the earlier release"
    elif [[ "$promoted" == "$release" ]]; then
      running="the application is running it"
    else
      running="the application is running the release that was not promoted, which the next boot would replace; promote a retained release to settle it"
    fi
  fi
  if [[ "$promoted" == "$release" ]]; then
    echo "$release was already the release the boot service starts, and deploying it again did not complete; $running"
  else
    echo "$release was not promoted and the boot service still starts ${promoted:-nothing}; $running"
  fi
}

[[ $# -ge 1 ]] || usage
ACTION=$1
shift
((EUID == 0)) || fail "run as root so the host egress policy can be installed"

case "$ACTION" in
  promote)
    [[ $# -eq 1 ]] || usage
    for command in systemd-run journalctl; do
      command -v "$command" >/dev/null 2>&1 || fail "$command is required"
    done
    release=$(realpath -- "$1")
    check_release "$release"
    load_settings
    [[ ! -e "$CURRENT" || -L "$CURRENT" ]] || fail "$CURRENT exists and is not a link"
    check_ancestors "$CURRENT"
    ! promotion_is_under_way || \
      fail "a promotion is under way; wait for it, or stop it with systemctl stop $PROMOTE_UNIT.service"
    self=$(realpath -- "${BASH_SOURCE[0]}")
    # The deployment and the move of the link run as one systemd unit, so a
    # closed terminal or Ctrl-C cannot leave them half done. To stop a
    # promotion, stop the unit; that is reported here as a failure.
    trap '' INT HUP
    # Print this promotion's output only; an earlier one may have ended
    # within the same second.
    cursor=$({ journalctl --lines 0 --show-cursor 2>/dev/null || true; } | sed -n 's/^-- cursor: //p')
    output_since=(--since "@$(date +%s)")
    [[ -z "$cursor" ]] || output_since=(--after-cursor "$cursor")
    echo "Deploying $release as $PROMOTE_UNIT.service (journalctl -fu $PROMOTE_UNIT to follow)..."
    status=0
    # A stopped deployment may need a minute and a half to clear a container
    # Docker will not stop; systemd's default would kill it at 90 seconds.
    systemd-run --quiet --wait --collect --unit "$PROMOTE_UNIT" --service-type=oneshot \
      --property TimeoutStartSec=900 --property TimeoutStopSec=140 "$self" apply "$release" || status=$?
    # systemd 255 returns here as soon as a stopped unit's start job is
    # cancelled, while the deployment it interrupted is still stopping the
    # application. Judge the outcome only once the unit is gone.
    deadline=$((SECONDS + 150))
    while promotion_is_under_way && ((SECONDS < deadline)); do
      sleep 1
    done
    journalctl --no-pager --output cat --unit "$PROMOTE_UNIT.service" "${output_since[@]}" || true
    if ((status != 0)) || [[ "$(readlink -- "$CURRENT" 2>/dev/null)" != "$release" ]]; then
      fail "$(describe_outcome "$release")"
    fi
    echo "Promoted $release; the boot service starts it from now on"
    ;;
  apply)
    # Run by promote, inside its systemd unit.
    [[ $# -eq 1 ]] || usage
    take_release_lock
    release=$(realpath -- "$1")
    check_release "$release"
    load_settings
    [[ ! -e "$CURRENT" || -L "$CURRENT" ]] || fail "$CURRENT exists and is not a link"
    check_ancestors "$CURRENT"
    if [[ "$(readlink -- "$CURRENT" 2>/dev/null)" == "$release" ]]; then
      # Promoting the promoted release again deploys it; the link stays.
      deploy "$release"
      exit 0
    fi
    # Write the new link beside the old one before anything is stopped, so a
    # directory that cannot be written to refuses the promotion up front.
    [[ ! -e "$CURRENT.new" || -L "$CURRENT.new" ]] || fail "$CURRENT.new exists and is not a link"
    ln -sfn -- "$release" "$CURRENT.new"
    trap 'rm -f -- "$CURRENT.new"' EXIT
    deploy "$release"
    # Only a release that deployed becomes the one the boot service starts.
    if ! mv -T -- "$CURRENT.new" "$CURRENT"; then
      # It is running, and a reboot would bring back the other one. Leave
      # nothing running that the link does not name.
      stop_application || \
        fail "the release deployed but $CURRENT could not be moved, and the application could not be stopped"
      fail "the release deployed but $CURRENT could not be moved; the application was stopped"
    fi
    ;;
  start)
    [[ $# -eq 0 ]] || usage
    take_release_lock
    [[ -L "$CURRENT" ]] || fail "$CURRENT is not a link to a promoted release"
    check_ancestors "$CURRENT"
    release=$(realpath -- "$CURRENT")
    check_release "$release"
    load_settings
    deploy "$release"
    ;;
  stop)
    [[ $# -eq 0 ]] || usage
    load_project_name
    # After a Docker crash, systemd runs this before it starts Docker again.
    # The CLI would then wait on the socket for a daemon that cannot start
    # until this returns, and there is no running container to stop anyway.
    systemctl is-active --quiet docker.service || exit 0
    stop_application || fail "Docker did not list the application's containers; it may still be running"
    ;;
  *) usage ;;
esac
