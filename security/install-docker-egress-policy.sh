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
HOST_CHAIN_A="SY-HOST-${POLICY_ID}-A"
HOST_CHAIN_B="SY-HOST-${POLICY_ID}-B"
POLICY_OUTPUT=""
BRIDGE_NETFILTER_DIR=/proc/sys/net/bridge
NETWORK_INTERFACES_DIR=/sys/class/net
LOCK_DIR=/run/scrapeyard

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
  done < <("$tool" -S DOCKER-USER 2>/dev/null)
  return 1
}

# Removes this deployment's jump to a chain, and the chain once nothing jumps
# to it. A chain that another interface still jumps to keeps its rules:
# emptied, it would leave that jump filtering nothing.
remove_chain() {
  local tool=$1 chain=$2
  while has_jump "$tool" "$chain"; do
    "$tool" -D DOCKER-USER -i "$NETWORK_INTERFACE" -j "$chain"
  done
  ! chain_is_referenced "$tool" "$chain" || return 1
  "$tool" -F "$chain" >/dev/null 2>&1 || true
  "$tool" -X "$chain" >/dev/null 2>&1 || true
}

# Removing the deployment network and creating it again gives it a new bridge.
# The jump installed for the old bridge then names an interface that no longer
# exists: it matches no packet, but it keeps its chain referenced. Remove such
# a jump. One on an interface that still exists is left for active_chain to
# refuse, because something else is using this policy's chain. Nothing is
# removed unless the deployment bridge itself is visible, so a host view that
# shows no interfaces at all cannot make a live bridge's jump look stale.
drop_stale_jumps() {
  local tool=$1 chain=$2 rule interface
  local -a fields
  [[ -e "$NETWORK_INTERFACES_DIR/$NETWORK_INTERFACE" ]] || return 0
  while IFS= read -r rule; do
    [[ " $rule " == *" -j $chain "* ]] || continue
    read -r -a fields <<<"$rule"
    [[ ${#fields[@]} -eq 6 && "${fields[2]}" == "-i" ]] || continue
    interface=${fields[3]}
    [[ "$interface" =~ ^[a-zA-Z0-9_.:-]{1,15}$ && "$interface" != "$NETWORK_INTERFACE" ]] || continue
    [[ ! -e "$NETWORK_INTERFACES_DIR/$interface" ]] || continue
    "$tool" -D DOCKER-USER -i "$interface" -j "$chain"
    echo "Removed the $tool jump to $chain left for the removed interface $interface"
  done < <("$tool" -S DOCKER-USER 2>/dev/null)
}

# The policy in DOCKER-USER sees forwarded and same-bridge traffic only. A
# connection from the application to one of the host's own addresses (the
# bridge gateway, the host's network addresses) is input to the host and
# passes none of it. The host chains below refuse that input from the
# application's address, and admit only replies to connections the host
# itself opened to the application.

# Prints "interface chain" for each INPUT jump to one of this policy's host
# chains. A jump of any other shape is not listed, and its chain then cannot
# be deleted, which stops the run.
host_jumps() {
  local tool=$1 rule
  local -a fields
  while IFS= read -r rule; do
    read -r -a fields <<<"$rule"
    [[ ${#fields[@]} -eq 6 && "${fields[0]}" == "-A" && "${fields[1]}" == "INPUT" && \
       "${fields[2]}" == "-i" && "${fields[4]}" == "-j" ]] || continue
    [[ "${fields[5]}" == "$HOST_CHAIN_A" || "${fields[5]}" == "$HOST_CHAIN_B" ]] || continue
    printf '%s %s\n' "${fields[3]}" "${fields[5]}"
  done < <("$tool" -S INPUT 2>/dev/null)
}

# A host jump left for a bridge that no longer exists matches nothing, like
# the forwarded policy's. One on another interface that still exists belongs
# to something else. As above, a missing interface proves nothing unless the
# deployment bridge itself is in view.
host_jump_is_stale() {
  [[ "$1" != "$NETWORK_INTERFACE" && -e "$NETWORK_INTERFACES_DIR/$NETWORK_INTERFACE" && \
     ! -e "$NETWORK_INTERFACES_DIR/$1" ]]
}

check_host_jumps() {
  local tool=$1 interface chain
  while read -r interface chain; do
    [[ "$interface" == "$NETWORK_INTERFACE" ]] || host_jump_is_stale "$interface" || \
      fail "$tool host chain $chain has a jump on another interface ($interface)"
  done < <(host_jumps "$tool")
}

host_chain_is_referenced() {
  local tool=$1 chain=$2 rule
  while IFS= read -r rule; do
    [[ " $rule " != *" -j $chain "* ]] || return 0
  done < <("$tool" -S INPUT 2>/dev/null)
  return 1
}

# Builds the inactive host chain in full, switches INPUT to it, and only then
# removes what it replaces. It only ever adds a refusal, so it runs before the
# forwarded policy is touched and needs no rollback of its own.
install_host_rules() {
  local tool=$1 source=$2 interface chain old="" new other
  while read -r interface chain; do
    if host_jump_is_stale "$interface"; then
      "$tool" -D INPUT -i "$interface" -j "$chain"
      echo "Removed the $tool host jump to $chain left for the removed interface $interface"
    else
      old=$chain
    fi
  done < <(host_jumps "$tool")
  if [[ "$old" == "$HOST_CHAIN_A" ]]; then
    new=$HOST_CHAIN_B
    other=$HOST_CHAIN_A
  else
    new=$HOST_CHAIN_A
    other=$HOST_CHAIN_B
  fi
  ! host_chain_is_referenced "$tool" "$new" || fail "$tool host chain $new is unexpectedly active"
  "$tool" -F "$new" >/dev/null 2>&1 || true
  "$tool" -X "$new" >/dev/null 2>&1 || true
  "$tool" -N "$new"
  "$tool" -A "$new" -s "$source" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  "$tool" -A "$new" -s "$source" -j REJECT
  "$tool" -A "$new" -j RETURN
  "$tool" -I INPUT 1 -i "$NETWORK_INTERFACE" -j "$new"
  "$tool" -C INPUT -i "$NETWORK_INTERFACE" -j "$new"
  while "$tool" -C INPUT -i "$NETWORK_INTERFACE" -j "$other" >/dev/null 2>&1; do
    "$tool" -D INPUT -i "$NETWORK_INTERFACE" -j "$other"
  done
  if ! host_chain_is_referenced "$tool" "$other"; then
    "$tool" -F "$other" >/dev/null 2>&1 || true
    "$tool" -X "$other" >/dev/null 2>&1 || true
  fi
  echo "Installed $new for Scrapeyard source $source on $NETWORK_INTERFACE (host addresses)"
}

# Removes this deployment's host jumps and chains. Returns 1 when a chain is
# left because another live interface still jumps to it.
remove_host_rules() {
  local tool=$1 interface chain left=0
  while read -r interface chain; do
    if [[ "$interface" == "$NETWORK_INTERFACE" ]] || host_jump_is_stale "$interface"; then
      "$tool" -D INPUT -i "$interface" -j "$chain"
    fi
  done < <(host_jumps "$tool")
  for chain in "$HOST_CHAIN_A" "$HOST_CHAIN_B"; do
    if host_chain_is_referenced "$tool" "$chain"; then
      left=1
    else
      "$tool" -F "$chain" >/dev/null 2>&1 || true
      "$tool" -X "$chain" >/dev/null 2>&1 || true
    fi
  done
  return "$left"
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

# The application reaches Redis, the probe and every other peer on the
# deployment bridge at layer 2. Those frames pass iptables, and so this policy,
# only while the kernel's bridge netfilter hook is on. Docker Engine turned it
# on for every host until 27.4; since then it loads br_netfilter only for a
# network with inter-container communication disabled. With the hook off the
# chains install cleanly and filter nothing on the bridge.
require_bridge_netfilter() {
  local key=$1 setting="$BRIDGE_NETFILTER_DIR/$1"
  if [[ ! -e "$setting" ]]; then
    modprobe br_netfilter >/dev/null 2>&1 || true
  fi
  [[ -e "$setting" ]] || \
    fail "bridge netfilter (br_netfilter) is unavailable; same-bridge traffic would bypass the policy"
  if [[ "$(<"$setting")" != 1 ]]; then
    echo 1 >"$setting" 2>/dev/null || true
    [[ "$(<"$setting")" == 1 ]] || \
      fail "net.bridge.$key must be 1; same-bridge traffic would bypass the policy"
    echo "Enabled net.bridge.$key so the policy sees same-bridge traffic"
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

# Two runs that overlap can each undo the other's switch and leave no jump.
install -d -m 700 "$LOCK_DIR"
exec 8>>"$LOCK_DIR/egress-policy.lock"
flock --wait 120 8 || fail "another policy installation has held $LOCK_DIR/egress-policy.lock for two minutes"

if [[ "$ACTION" == "remove" ]]; then
  IN_USE=()
  for chain in "$LEGACY_CHAIN" "$CHAIN_A" "$CHAIN_B"; do
    drop_stale_jumps iptables "$chain"
    remove_chain iptables "$chain" || IN_USE+=("iptables $chain")
  done
  remove_host_rules iptables || IN_USE+=("iptables SY-HOST-$POLICY_ID")
  if command -v ip6tables >/dev/null 2>&1; then
    for chain in "$LEGACY_CHAIN" "$CHAIN_A" "$CHAIN_B"; do
      drop_stale_jumps ip6tables "$chain"
      remove_chain ip6tables "$chain" || IN_USE+=("ip6tables $chain")
    done
    remove_host_rules ip6tables || IN_USE+=("ip6tables SY-HOST-$POLICY_ID")
  fi
  [[ ${#IN_USE[@]} -eq 0 ]] || \
    fail "left in place, another interface still jumps to it: ${IN_USE[*]}; remove it with that interface"
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

require_bridge_netfilter bridge-nf-call-iptables
if [[ -n "$APP_SOURCE_V6" ]]; then
  require_bridge_netfilter bridge-nf-call-ip6tables
fi

# Refuse, before anything is changed, a host chain that another live
# interface uses.
check_host_jumps iptables
if [[ -n "$APP_SOURCE_V6" ]]; then
  check_host_jumps ip6tables
fi

for chain in "$LEGACY_CHAIN" "$CHAIN_A" "$CHAIN_B"; do
  drop_stale_jumps iptables "$chain"
  if [[ -n "$APP_SOURCE_V6" ]]; then
    drop_stale_jumps ip6tables "$chain"
  fi
done

V4_OLD=$(active_chain iptables)
V4_NEW=$(staging_chain "$V4_OLD")
V6_OLD=""
V6_NEW=""
if [[ -n "$APP_SOURCE_V6" ]]; then
  V6_OLD=$(active_chain ip6tables)
  V6_NEW=$(staging_chain "$V6_OLD")
fi

# Every refusal above has been made. The host rules go in first: they only add
# a refusal, and the forwarded policy keeps its own rollback below.
install_host_rules iptables "$APP_SOURCE"
if [[ -n "$APP_SOURCE_V6" ]]; then
  install_host_rules ip6tables "$APP_SOURCE_V6"
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
