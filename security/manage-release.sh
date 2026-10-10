#!/usr/bin/env bash

set -euo pipefail

ENV_FILE=/etc/scrapeyard/deploy.env
CURRENT=/srv/scrapeyard-releases/current
PROMOTE_UNIT=scrapeyard-promote
TRUSTED_OWNER=0

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

# The settings hold credentials, so they are read here and never handed to
# systemd, which writes a line it cannot parse to the journal, value included.
# A line that is not NAME='value' is refused by its number alone.
load_settings() {
  local line name number=0
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "$ENV_FILE is missing or is a link"
  [[ "$(stat -c '%u %a' "$ENV_FILE")" == "$TRUSTED_OWNER 600" ]] || \
    fail "$ENV_FILE must be owned by root with mode 600"
  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))
    [[ -n "$line" && "$line" != \#* ]] || continue
    if [[ "$line" =~ ^((SCRAPEYARD|COMPOSE)_[A-Z0-9_]+)=\'([^\']*)\'$ ]]; then
      name=${BASH_REMATCH[1]}
      export "$name=${BASH_REMATCH[3]}"
    else
      fail "$ENV_FILE line $number is not a SCRAPEYARD_ or COMPOSE_ setting written as NAME='value'"
    fi
  done <"$ENV_FILE"
  [[ "${COMPOSE_PROJECT_NAME:-}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || \
    fail "$ENV_FILE must set COMPOSE_PROJECT_NAME to the deployment's Compose project"
}

# Root runs this tree at every boot, so no other account may be able to change it.
check_release() {
  local release=$1 offender
  [[ -f "$release/manifest.json" && -x "$release/source/security/deploy-secure-compose.sh" ]] || \
    fail "not a retained release: $release"
  offender=$(find "$release" \( ! -user "$TRUSTED_OWNER" -o -perm /022 \) -print -quit)
  [[ -z "$offender" ]] || \
    fail "$offender is not owned by root, or others can write to it; extract releases as root with tar --no-same-owner"
}

deploy() {
  "$1/source/security/deploy-secure-compose.sh" --release "$1"
}

application_containers() {
  docker ps -q \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" \
    --filter "label=com.docker.compose.service=scrapeyard"
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
    self=$(realpath -- "${BASH_SOURCE[0]}")
    # The deployment and the move of the link run as one systemd unit, so a
    # closed terminal or Ctrl-C cannot leave them half done. To stop a
    # promotion, stop the unit; that is reported here as a failure.
    trap '' INT HUP
    started=$(date +%s)
    echo "Deploying $release as $PROMOTE_UNIT.service (journalctl -fu $PROMOTE_UNIT to follow)..."
    status=0
    systemd-run --quiet --wait --collect --unit "$PROMOTE_UNIT" --service-type=oneshot \
      --property TimeoutStartSec=900 "$self" apply "$release" || status=$?
    journalctl --no-pager --output cat --unit "$PROMOTE_UNIT.service" --since "@$started" || true
    if ((status != 0)) || [[ "$(readlink -- "$CURRENT" 2>/dev/null)" != "$release" ]]; then
      if [[ -n "$(application_containers 2>/dev/null)" ]]; then
        state="the application is running"
      else
        state="the application is stopped; promote a retained release to start it"
      fi
      fail "$release was not promoted and the boot service still starts $(readlink -- "$CURRENT" 2>/dev/null || echo nothing); $state"
    fi
    echo "Promoted $release; the boot service starts it from now on"
    ;;
  apply)
    # Run by promote, inside its systemd unit.
    [[ $# -eq 1 ]] || usage
    release=$(realpath -- "$1")
    check_release "$release"
    load_settings
    deploy "$release"
    # Only a release that deployed becomes the one the boot service starts.
    ln -s -- "$release" "$CURRENT.new.$$"
    mv -T -- "$CURRENT.new.$$" "$CURRENT"
    ;;
  start)
    [[ $# -eq 0 ]] || usage
    [[ -L "$CURRENT" ]] || fail "$CURRENT is not a link to a promoted release"
    release=$(realpath -- "$CURRENT")
    check_release "$release"
    load_settings
    deploy "$release"
    ;;
  stop)
    [[ $# -eq 0 ]] || usage
    load_settings
    # After a Docker crash, systemd runs this before it starts Docker again.
    # The CLI would then wait on the socket for a daemon that cannot start
    # until this returns, and there is no running container to stop anyway.
    systemctl is-active --quiet docker.service || exit 0
    mapfile -t containers < <(application_containers)
    # Without --time, the container's own grace period applies.
    ((${#containers[@]} == 0)) || timeout 90 docker stop "${containers[@]}" >/dev/null
    ;;
  *) usage ;;
esac
