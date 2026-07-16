#!/usr/bin/env bash

set -Eeuo pipefail

ACTION="${1:-install}"
APP_SOURCE="${SCRAPEYARD_EGRESS_SOURCE:-172.30.0.250}"
APP_SOURCE_V6="${SCRAPEYARD_EGRESS_SOURCE_V6:-}"
REDIS_DESTINATION="${SCRAPEYARD_REDIS_DESTINATION:-172.30.0.249}"
PROBE_DESTINATION="${SCRAPEYARD_EGRESS_POLICY_PROBE_HOST:-172.30.0.248}"
PROBE_LIVENESS_PORT="${SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT:-8081}"
ALLOW_CIDRS="${SCRAPEYARD_EGRESS_ALLOW_CIDRS:-}"
NETWORK_INTERFACE="${SCRAPEYARD_EGRESS_INTERFACE:-}"
POLICY_ID="${SCRAPEYARD_EGRESS_POLICY_ID:-}"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
POLICY_RENDERER="${SCRAPEYARD_EGRESS_POLICY_RENDERER:-${SCRIPT_DIR}/render-egress-policy.py}"
LEGACY_CHAIN="SY-EGRESS-${POLICY_ID}"
CHAIN_A="${LEGACY_CHAIN}-A"
CHAIN_B="${LEGACY_CHAIN}-B"
POLICY_OUTPUT=""

fail() {
  echo "scrapeyard egress policy: $*" >&2
  exit 2
}

has_jump() {
  local tool=$1 chain=$2
  "$tool" -C DOCKER-USER -i "$NETWORK_INTERFACE" -j "$chain" >/dev/null 2>&1
}

chain_exists() {
  local tool=$1 chain=$2
  "$tool" -S "$chain" >/dev/null 2>&1
}

chain_is_referenced() {
  local tool=$1 chain=$2 rule
  while IFS= read -r rule; do
    if [[ " $rule " == *" -j $chain "* ]]; then
      return 0
    fi
  done < <("$tool" -S DOCKER-USER)
  return 1
}

remove_chain() {
  local tool=$1 chain=$2
  while has_jump "$tool" "$chain"; do
    "$tool" -D DOCKER-USER -i "$NETWORK_INTERFACE" -j "$chain"
  done
  "$tool" -F "$chain" >/dev/null 2>&1 || true
  "$tool" -X "$chain" >/dev/null 2>&1 || true
}

active_chain() {
  local tool=$1 active="" candidate count=0
  for candidate in "$LEGACY_CHAIN" "$CHAIN_A" "$CHAIN_B"; do
    if chain_is_referenced "$tool" "$candidate"; then
      has_jump "$tool" "$candidate" || \
        fail "$tool policy chain $candidate has an unexpected jump scope"
      active=$candidate
      count=$((count + 1))
    fi
  done
  [[ $count -le 1 ]] || fail "$tool has multiple active Scrapeyard policy jumps"
  if [[ -n "$active" ]] && ! chain_exists "$tool" "$active"; then
    fail "$tool active policy chain $active is missing"
  fi
  printf '%s' "$active"
}

staging_chain() {
  local active=$1
  if [[ "$active" == "$CHAIN_A" ]]; then
    printf '%s' "$CHAIN_B"
  else
    printf '%s' "$CHAIN_A"
  fi
}

[[ "$ACTION" == "install" || "$ACTION" == "remove" ]] || \
  fail "usage: $0 [install|remove]"
((EUID == 0)) || fail "host root privileges are required"
[[ "$NETWORK_INTERFACE" =~ ^[a-zA-Z0-9_.:-]{1,15}$ ]] || \
  fail "SCRAPEYARD_EGRESS_INTERFACE must name the deployment bridge (max 15 characters)"
[[ "$POLICY_ID" =~ ^[a-zA-Z0-9_-]{1,12}$ ]] || \
  fail "SCRAPEYARD_EGRESS_POLICY_ID must be 1-12 safe characters"
for command in iptables python3; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done

# Validate the host chains before any mutation. Docker owns DOCKER-USER; a
# missing prerequisite must never be "fixed" by temporarily removing policy.
iptables -S DOCKER-USER >/dev/null 2>&1 || fail "iptables DOCKER-USER chain is required"

if [[ "$ACTION" == "remove" ]]; then
  for chain in "$LEGACY_CHAIN" "$CHAIN_A" "$CHAIN_B"; do
    remove_chain iptables "$chain"
  done
  if command -v ip6tables >/dev/null 2>&1; then
    for chain in "$LEGACY_CHAIN" "$CHAIN_A" "$CHAIN_B"; do
      remove_chain ip6tables "$chain"
    done
  fi
  exit 0
fi

if [[ -n "$APP_SOURCE_V6" ]]; then
  command -v ip6tables >/dev/null 2>&1 || fail "ip6tables is required for IPv6 policy"
  ip6tables -S DOCKER-USER >/dev/null 2>&1 || fail "ip6tables DOCKER-USER chain is required"
fi
[[ -f "$POLICY_RENDERER" ]] || fail "address policy renderer is missing"

# Reject wrong-family inputs and malformed derived values before touching the
# active chains. IPv6 allow entries require an explicit IPv6 application
# source so they cannot silently bypass source scoping.
python3 - "$APP_SOURCE" "$APP_SOURCE_V6" "$REDIS_DESTINATION" \
  "$PROBE_DESTINATION" "$PROBE_LIVENESS_PORT" "$ALLOW_CIDRS" <<'PY'
import ipaddress
import sys

def address(value: str, version: int, name: str) -> None:
    parsed = ipaddress.ip_address(value)
    if parsed.version != version:
        raise ValueError(f"{name} must be IPv{version}")

address(sys.argv[1], 4, "SCRAPEYARD_EGRESS_SOURCE")
if sys.argv[2]:
    address(sys.argv[2], 6, "SCRAPEYARD_EGRESS_SOURCE_V6")
address(sys.argv[3], 4, "SCRAPEYARD_REDIS_DESTINATION")
address(sys.argv[4], 4, "SCRAPEYARD_EGRESS_POLICY_PROBE_HOST")
port = int(sys.argv[5])
if not 1 <= port <= 65535:
    raise ValueError("probe liveness port is out of range")
for value in filter(None, sys.argv[6].split(",")):
    network = ipaddress.ip_network(value, strict=False)
    if network.version == 6 and not sys.argv[2]:
        raise ValueError("IPv6 allow CIDR requires SCRAPEYARD_EGRESS_SOURCE_V6")
PY

POLICY_OUTPUT=$(mktemp)
trap 'rm -f "$POLICY_OUTPUT"' EXIT
python3 "$POLICY_RENDERER" >"$POLICY_OUTPUT"
python3 - "$POLICY_OUTPUT" <<'PY'
import ipaddress
import pathlib
import sys

for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    fields = line.split("\t")
    if len(fields) != 3 or fields[0] not in {"4", "6"} or fields[1] not in {"allow", "deny"}:
        raise ValueError("address policy renderer returned a malformed row")
    network = ipaddress.ip_network(fields[2])
    if network.version != int(fields[0]):
        raise ValueError("address policy renderer returned a wrong-family CIDR")
PY
POLICY_V4_ALLOW=()
POLICY_V4_DENY=()
POLICY_V6_ALLOW=()
POLICY_V6_DENY=()
while IFS=$'\t' read -r family action cidr extra; do
  [[ -n "$family" && -n "$action" && -n "$cidr" && -z "${extra:-}" ]] || \
    fail "address policy renderer returned a malformed row"
  case "$family:$action" in
    4:allow) POLICY_V4_ALLOW+=("$cidr") ;;
    4:deny) POLICY_V4_DENY+=("$cidr") ;;
    6:allow) POLICY_V6_ALLOW+=("$cidr") ;;
    6:deny) POLICY_V6_DENY+=("$cidr") ;;
    *) fail "address policy renderer returned an unsupported row" ;;
  esac
done <"$POLICY_OUTPUT"
[[ ${#POLICY_V4_DENY[@]} -gt 0 && ${#POLICY_V6_DENY[@]} -gt 0 ]] || \
  fail "address policy renderer returned an empty deny policy"

V4_OLD=$(active_chain iptables)
V4_NEW=$(staging_chain "$V4_OLD")
V6_OLD=""
V6_NEW=""
if [[ -n "$APP_SOURCE_V6" ]]; then
  V6_OLD=$(active_chain ip6tables)
  V6_NEW=$(staging_chain "$V6_OLD")
fi

V4_SWITCHED=0
V6_SWITCHED=0
rollback() {
  local status=$?
  trap - ERR INT TERM
  set +e
  if [[ $V6_SWITCHED -eq 1 ]]; then
    if [[ -n "$V6_OLD" ]] && ! has_jump ip6tables "$V6_OLD"; then
      ip6tables -I DOCKER-USER 1 -i "$NETWORK_INTERFACE" -j "$V6_OLD"
    fi
    while has_jump ip6tables "$V6_NEW"; do
      ip6tables -D DOCKER-USER -i "$NETWORK_INTERFACE" -j "$V6_NEW"
    done
  fi
  if [[ $V4_SWITCHED -eq 1 ]]; then
    if [[ -n "$V4_OLD" ]] && ! has_jump iptables "$V4_OLD"; then
      iptables -I DOCKER-USER 1 -i "$NETWORK_INTERFACE" -j "$V4_OLD"
    fi
    while has_jump iptables "$V4_NEW"; do
      iptables -D DOCKER-USER -i "$NETWORK_INTERFACE" -j "$V4_NEW"
    done
  fi
  rm -f "$POLICY_OUTPUT"
  echo "scrapeyard egress policy: replacement failed; prior policy restored" >&2
  exit "$status"
}
trap rollback ERR INT TERM

prepare_chain() {
  local tool=$1 chain=$2
  if chain_exists "$tool" "$chain"; then
    ! chain_is_referenced "$tool" "$chain" || \
      fail "$tool staging chain $chain is unexpectedly active"
    remove_chain "$tool" "$chain"
  fi
  "$tool" -N "$chain"
}

append_v4_policy() {
  local chain=$1 cidr
  # With bridge netfilter enabled, replies from the application traverse this
  # source-scoped chain too. Admit only packets belonging to connections whose
  # NEW packet already passed the destination policy below.
  iptables -A "$chain" -s "$APP_SOURCE" -m conntrack \
    --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A "$chain" -s "$APP_SOURCE" -d "$REDIS_DESTINATION" \
    -p tcp --dport 6379 -j ACCEPT
  iptables -A "$chain" -s "$APP_SOURCE" -d "$PROBE_DESTINATION" \
    -p tcp --dport "$PROBE_LIVENESS_PORT" -j ACCEPT
  IFS=',' read -r -a allowed <<<"$ALLOW_CIDRS"
  for cidr in "${allowed[@]}"; do
    [[ -n "$cidr" && "$cidr" != *:* ]] || continue
    iptables -A "$chain" -s "$APP_SOURCE" -d "$cidr" -j ACCEPT
  done
  for cidr in "${POLICY_V4_ALLOW[@]}"; do
    iptables -A "$chain" -s "$APP_SOURCE" -d "$cidr" -j ACCEPT
  done
  for cidr in "${POLICY_V4_DENY[@]}"; do
    iptables -A "$chain" -s "$APP_SOURCE" -d "$cidr" -j REJECT
  done
  iptables -A "$chain" -j RETURN
}

append_v6_policy() {
  local chain=$1 cidr
  ip6tables -A "$chain" -s "$APP_SOURCE_V6" -m conntrack \
    --ctstate ESTABLISHED,RELATED -j ACCEPT
  IFS=',' read -r -a allowed <<<"$ALLOW_CIDRS"
  for cidr in "${allowed[@]}"; do
    [[ -n "$cidr" && "$cidr" == *:* ]] || continue
    ip6tables -A "$chain" -s "$APP_SOURCE_V6" -d "$cidr" -j ACCEPT
  done
  for cidr in "${POLICY_V6_ALLOW[@]}"; do
    ip6tables -A "$chain" -s "$APP_SOURCE_V6" -d "$cidr" -j ACCEPT
  done
  for cidr in "${POLICY_V6_DENY[@]}"; do
    ip6tables -A "$chain" -s "$APP_SOURCE_V6" -d "$cidr" -j REJECT
  done
  ip6tables -A "$chain" -j RETURN
}

# Build complete inactive chains while the prior jumps remain enforced.
prepare_chain iptables "$V4_NEW"
append_v4_policy "$V4_NEW"
if [[ -n "$APP_SOURCE_V6" ]]; then
  prepare_chain ip6tables "$V6_NEW"
  append_v6_policy "$V6_NEW"
fi

# Insert and verify each replacement before removing its prior jump. If any
# later command fails, the trap reinserts the old jump before removing new.
iptables -I DOCKER-USER 1 -i "$NETWORK_INTERFACE" -j "$V4_NEW"
V4_SWITCHED=1
has_jump iptables "$V4_NEW"
if [[ -n "$V4_OLD" ]]; then
  iptables -D DOCKER-USER -i "$NETWORK_INTERFACE" -j "$V4_OLD"
fi
if [[ -n "$APP_SOURCE_V6" ]]; then
  ip6tables -I DOCKER-USER 1 -i "$NETWORK_INTERFACE" -j "$V6_NEW"
  V6_SWITCHED=1
  has_jump ip6tables "$V6_NEW"
  if [[ -n "$V6_OLD" ]]; then
    ip6tables -D DOCKER-USER -i "$NETWORK_INTERFACE" -j "$V6_OLD"
  fi
fi

trap - ERR INT TERM
rm -f "$POLICY_OUTPUT"
# Inactive old chains are harmless. Cleanup is best-effort so a host-tool
# cleanup error cannot roll back or invalidate the already verified policy.
if [[ -n "$V4_OLD" ]]; then
  if ! chain_is_referenced iptables "$V4_OLD"; then
    iptables -F "$V4_OLD" >/dev/null 2>&1 || true
    iptables -X "$V4_OLD" >/dev/null 2>&1 || true
  fi
fi
if [[ -n "$V6_OLD" ]]; then
  if ! chain_is_referenced ip6tables "$V6_OLD"; then
    ip6tables -F "$V6_OLD" >/dev/null 2>&1 || true
    ip6tables -X "$V6_OLD" >/dev/null 2>&1 || true
  fi
fi

echo "Installed $V4_NEW for Scrapeyard source $APP_SOURCE on $NETWORK_INTERFACE"
