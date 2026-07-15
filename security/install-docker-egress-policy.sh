#!/usr/bin/env bash

set -euo pipefail

ACTION="${1:-install}"
APP_SOURCE="${SCRAPEYARD_EGRESS_SOURCE:-172.30.0.250}"
APP_SOURCE_V6="${SCRAPEYARD_EGRESS_SOURCE_V6:-}"
REDIS_DESTINATION="${SCRAPEYARD_REDIS_DESTINATION:-172.30.0.249}"
PROBE_DESTINATION="${SCRAPEYARD_EGRESS_POLICY_PROBE_HOST:-172.30.0.248}"
PROBE_LIVENESS_PORT="${SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT:-8081}"
ALLOW_CIDRS="${SCRAPEYARD_EGRESS_ALLOW_CIDRS:-}"
NETWORK_INTERFACE="${SCRAPEYARD_EGRESS_INTERFACE:-}"
POLICY_ID="${SCRAPEYARD_EGRESS_POLICY_ID:-}"
CHAIN="SY-EGRESS-${POLICY_ID}"

fail() {
  echo "scrapeyard egress policy: $*" >&2
  exit 2
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

remove_jump() {
  local tool=$1
  while "$tool" -C DOCKER-USER -i "$NETWORK_INTERFACE" -j "$CHAIN" >/dev/null 2>&1; do
    "$tool" -D DOCKER-USER -i "$NETWORK_INTERFACE" -j "$CHAIN"
  done
  "$tool" -F "$CHAIN" >/dev/null 2>&1 || true
  "$tool" -X "$CHAIN" >/dev/null 2>&1 || true
}

if [[ "$ACTION" == "remove" ]]; then
  remove_jump iptables
  command -v ip6tables >/dev/null 2>&1 && remove_jump ip6tables
  exit 0
fi

# Validate operator inputs before changing host policy.
python3 - "$APP_SOURCE" "$APP_SOURCE_V6" "$REDIS_DESTINATION" \
  "$PROBE_DESTINATION" "$PROBE_LIVENESS_PORT" "$ALLOW_CIDRS" <<'PY'
import ipaddress
import sys

ipaddress.ip_address(sys.argv[1])
if sys.argv[2]:
    ipaddress.ip_address(sys.argv[2])
ipaddress.ip_address(sys.argv[3])
ipaddress.ip_address(sys.argv[4])
port = int(sys.argv[5])
if not 1 <= port <= 65535:
    raise ValueError("probe liveness port is out of range")
for value in filter(None, sys.argv[6].split(",")):
    ipaddress.ip_network(value, strict=False)
PY

remove_jump iptables
iptables -N "$CHAIN"
if [[ -n "$APP_SOURCE_V6" ]]; then
  command -v ip6tables >/dev/null 2>&1 || fail "ip6tables is required for IPv6 policy"
  remove_jump ip6tables
  ip6tables -N "$CHAIN"
fi

# Permit only the declared Redis peer and explicit proxy/service destinations
# before rejecting reusable/private address space. Public internet remains
# reachable; a DNS-rebound destination is filtered by its connected IP.
iptables -A "$CHAIN" -s "$APP_SOURCE" -d "$REDIS_DESTINATION" \
  -p tcp --dport 6379 -j ACCEPT
# Permit only the controlled helper's liveness channel. Its challenge port
# remains subject to the private-address rejection below.
iptables -A "$CHAIN" -s "$APP_SOURCE" -d "$PROBE_DESTINATION" \
  -p tcp --dport "$PROBE_LIVENESS_PORT" -j ACCEPT
IFS=',' read -r -a allowed <<< "$ALLOW_CIDRS"
for cidr in "${allowed[@]}"; do
  [[ -n "$cidr" ]] || continue
  if [[ "$cidr" == *:* ]]; then
    [[ -n "$APP_SOURCE_V6" ]] || fail "IPv6 allow CIDR requires SCRAPEYARD_EGRESS_SOURCE_V6"
    ip6tables -A "$CHAIN" -s "$APP_SOURCE_V6" -d "$cidr" -j ACCEPT
  else
    iptables -A "$CHAIN" -s "$APP_SOURCE" -d "$cidr" -j ACCEPT
  fi
done

for cidr in \
  0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
  169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.168.0.0/16 \
  198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
  iptables -A "$CHAIN" -s "$APP_SOURCE" -d "$cidr" -j REJECT
done
iptables -A "$CHAIN" -j RETURN
iptables -I DOCKER-USER 1 -i "$NETWORK_INTERFACE" -j "$CHAIN"
if [[ -n "$APP_SOURCE_V6" ]]; then
  for cidr in ::1/128 fc00::/7 fe80::/10 ff00::/8; do
    ip6tables -A "$CHAIN" -s "$APP_SOURCE_V6" -d "$cidr" -j REJECT
  done
  ip6tables -A "$CHAIN" -j RETURN
  ip6tables -I DOCKER-USER 1 -i "$NETWORK_INTERFACE" -j "$CHAIN"
fi

echo "Installed $CHAIN for Scrapeyard source $APP_SOURCE on $NETWORK_INTERFACE"
