#!/usr/bin/env bash
set -euo pipefail

# Replaces this machine's Docker with the versions the deployment guide names
# for a host, from Docker's repository, and restarts it. The container lanes
# run it before they build anything, so a release meets the runtime production
# starts it with and not whatever the runner image carries. It is for a runner
# or a throwaway machine: never run it on a host that serves.
#
# Raise these together with "Container runtime versions" in
# docs/DEPLOYMENT.md, and raise the production host with them.
DOCKER_VERSION="29.9.0"
CONTAINERD_VERSION="2.4.1"
COMPOSE_VERSION="5.6.0"
# The runc inside that containerd.io package. It starts every container, and
# the seccomp profile has to let it through.
RUNC_VERSION="1.5.2"

REPOSITORY="https://download.docker.com/linux/ubuntu"
REPOSITORY_KEY_FINGERPRINT="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
APT_DIR="${SCRAPEYARD_RUNTIME_APT_DIR:-/etc/apt}"
DOCKER_DIR="${SCRAPEYARD_RUNTIME_DOCKER_DIR:-/etc/docker}"

usage() {
  cat <<'EOF'
Usage: sudo scripts/install_container_runtime.sh

Installs the pinned docker-ce, docker-ce-cli, containerd.io and
docker-compose-plugin from Docker's repository, selects Docker's classic
image store, restarts Docker, and fails unless the running engine,
containerd, runc, Compose and image store are the pinned ones.
EOF
}

fail() {
  echo "install_container_runtime: $*" >&2
  exit 1
}

case "${1:-}" in
  "") ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
[[ "$(id -u)" == "0" ]] || fail "run as root"

add_repository() {
  # The runner image may already list the repository; a second entry with
  # another key path makes apt refuse both.
  if grep -rqsF "${REPOSITORY#https://}" "$APT_DIR/sources.list" "$APT_DIR/sources.list.d"; then
    return
  fi
  local key codename
  key="$(mktemp)"
  curl -fsSL --retry 3 "$REPOSITORY/gpg" -o "$key"
  # The whole file becomes apt's keyring for the repository, so it must hold
  # Docker's key and no other: one primary key, with the pinned fingerprint.
  if [[ "$(gpg --show-keys --with-colons "$key" | awk -F: '
      $1 == "pub" { primary = 1; next }
      $1 == "fpr" && primary { print $10; primary = 0 }')" \
    != "$REPOSITORY_KEY_FINGERPRINT" ]]; then
    rm -f -- "$key"
    fail "the key served by $REPOSITORY is not Docker's release key"
  fi
  install -D -m 0644 "$key" "$APT_DIR/keyrings/docker.asc"
  rm -f -- "$key"
  codename="$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")"
  printf 'deb [arch=%s signed-by=%s] %s %s stable\n' \
    "$(dpkg --print-architecture)" "$APT_DIR/keyrings/docker.asc" "$REPOSITORY" "$codename" \
    > "$APT_DIR/sources.list.d/docker.list"
}

# The repository's package version for an upstream version: an epoch in front
# and a packaging revision behind, and the revision is not always 1.
package_version() {
  local package="$1" upstream="$2" version
  version="$(apt-cache madison "$package" | awk -F'|' -v want="$upstream" '
    {
      gsub(/[[:space:]]/, "", $2)
      bare = $2
      sub(/^[0-9]+:/, "", bare)
      if (index(bare, want "-") == 1) { print $2; exit }
    }')"
  [[ -n "$version" ]] || fail "Docker's repository does not offer $package $upstream for this release"
  printf '%s\n' "$version"
}

expect() {
  local what="$1" pinned="$2" found="$3"
  [[ "$found" == "$pinned" ]] || fail "$what is ${found:-missing}, not the pinned $pinned"
}

export DEBIAN_FRONTEND=noninteractive
add_repository
apt-get update -q -o Acquire::Retries=3
engine_package="$(package_version docker-ce "$DOCKER_VERSION")"
client_package="$(package_version docker-ce-cli "$DOCKER_VERSION")"
containerd_package="$(package_version containerd.io "$CONTAINERD_VERSION")"
compose_package="$(package_version docker-compose-plugin "$COMPOSE_VERSION")"
# A runner carries a newer or an older Docker, and the image may hold it.
apt-get install -y -q --no-install-recommends --allow-downgrades --allow-change-held-packages \
  -o Acquire::Retries=3 -o Dpkg::Options::=--force-confold \
  "docker-ce=$engine_package" "docker-ce-cli=$client_package" \
  "containerd.io=$containerd_package" "docker-compose-plugin=$compose_package"
# A release names its images by identifier, and an image has another
# identifier under containerd's image store than under Docker's classic one.
# A host keeps the classic store; a new installation of Docker 29 would
# choose the other, so say it and do not leave it to the runner image.
python3 - "$DOCKER_DIR/daemon.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
settings = json.loads(path.read_text()) if path.exists() else {}
settings.setdefault("features", {})["containerd-snapshotter"] = False
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(settings, indent=2) + "\n")
PY
systemctl restart containerd docker
for _ in $(seq 1 60); do
  docker version --format '{{.Server.Version}}' > /dev/null 2>&1 && break
  sleep 1
done

# Ask what runs, not what was installed: a copy the image put ahead of the
# packaged one is what the daemon and the client would use. The daemon reports
# its runc as v1.5.2-0-g<commit>.
engine="$(docker version --format '{{.Server.Version}}')"
client="$(docker version --format '{{.Client.Version}}')"
containerd="$(containerd --version | awk '{sub(/^v/, "", $3); print $3}')"
runc="$(docker info --format '{{.RuncCommit.ID}}')"
runc="${runc#v}"
runc="${runc%%-*}"
compose="$(docker compose version --short)"
image_store="$(docker info --format '{{.Driver}} {{.DriverStatus}}')"
expect "Docker Engine" "$DOCKER_VERSION" "$engine"
expect "the Docker client" "$DOCKER_VERSION" "$client"
expect "containerd" "$CONTAINERD_VERSION" "$containerd"
expect "runc" "$RUNC_VERSION" "$runc"
expect "Compose" "$COMPOSE_VERSION" "$compose"
[[ -n "$image_store" && "$image_store" != *io.containerd.snapshotter* ]] || \
  fail "Docker stores images with ${image_store:-nothing it reports}, not with its classic image store"

echo "Container runtime: Docker $engine, containerd $containerd, runc $runc, Compose $compose," \
  "image store ${image_store%% *}"
