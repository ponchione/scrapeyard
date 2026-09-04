#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_SOURCE="$ROOT_DIR/security/apparmor/scrapeyard-chromium"
PROFILE_NAME="scrapeyard-chromium"
PROFILE_PATH="/etc/apparmor.d/$PROFILE_NAME"

usage() {
  echo "Usage: $0 {install|remove|status}" >&2
}

[[ $# -eq 1 ]] || { usage; exit 2; }
command -v apparmor_parser >/dev/null 2>&1 || {
  echo "apparmor_parser is required (install the host AppArmor utilities package)" >&2
  exit 69
}
[[ -d /sys/kernel/security/apparmor ]] || {
  echo "AppArmor is not enabled on this host" >&2
  exit 69
}

case "$1" in
  install)
    [[ ${EUID} -eq 0 ]] || {
      echo "install must run as root (for example: sudo $0 install)" >&2
      exit 77
    }
    apparmor_parser --skip-kernel-load --skip-cache "$PROFILE_SOURCE"
    install -o root -g root -m 0644 "$PROFILE_SOURCE" "$PROFILE_PATH"
    apparmor_parser --replace "$PROFILE_PATH"
    echo "Installed AppArmor profile $PROFILE_NAME"
    ;;
  remove)
    [[ ${EUID} -eq 0 ]] || {
      echo "remove must run as root (for example: sudo $0 remove)" >&2
      exit 77
    }
    if grep -q "^${PROFILE_NAME} " /sys/kernel/security/apparmor/profiles; then
      apparmor_parser --remove "$PROFILE_SOURCE"
      echo "Removed AppArmor profile $PROFILE_NAME"
    fi
    rm -f "$PROFILE_PATH"
    ;;
  status)
    if [[ -f "$PROFILE_PATH" ]] && cmp -s "$PROFILE_SOURCE" "$PROFILE_PATH" && \
      grep -q "^${PROFILE_NAME} " /sys/kernel/security/apparmor/profiles; then
      echo "$PROFILE_NAME is installed and loaded"
    else
      echo "$PROFILE_NAME is missing, stale, or not loaded" >&2
      exit 1
    fi
    ;;
  *) usage; exit 2 ;;
esac
