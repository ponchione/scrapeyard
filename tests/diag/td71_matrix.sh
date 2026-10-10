#!/usr/bin/env bash
# TD-71 diagnostic (throwaway): run the renderer-death probe in the production
# image under the Compose security options, lifting one option at a time.
# Meant for a GitHub runner; it loads the AppArmor profile on the host.

set -uo pipefail

GROUP=${1:?group}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
OUT="$ROOT/artifacts/td71/$GROUP"
mkdir -p "$OUT"
PROJECT=td71
SUMMARY="$OUT/summary.tsv"
: > "$SUMMARY"
STACKS_SHOWN=0

SCRAPEYARD_API_CREDENTIALS="$(printf '{"td71":{"secret":"td71-%s","scopes":["read"]}}' "$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')")"
SCRAPEYARD_ENCRYPTION_KEYS="$(printf '{"td71":"%s"}' "$(head -c 32 /dev/urandom | base64 | tr -d '\n')")"
export SCRAPEYARD_API_CREDENTIALS SCRAPEYARD_ENCRYPTION_KEYS
export SCRAPEYARD_ENCRYPTION_ACTIVE_KEY_ID=td71

cat > "$OUT/diag.yml" <<'YAML'
services:
  scrapeyard:
    command: ["sleep", "infinity"]
    healthcheck:
      disable: true
YAML

python3 - "$OUT/chromium-log.json" <<'PY'
import json, sys
profile = json.load(open("security/seccomp/chromium.json"))
profile["defaultAction"] = "SCMP_ACT_LOG"
json.dump(profile, open(sys.argv[1], "w"))
PY

SECCOMP="seccomp:$ROOT/security/seccomp/chromium.json"
override() {
  # override NAME: write the Compose override that lifts NAME, print its path.
  local name=$1 file="$OUT/override-$1.yml"
  case "$name" in
    control*) printf 'services: {}\n' > "$file" ;;
    seccomp-unconfined) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    security_opt: !override
      - "no-new-privileges:true"
      - "apparmor:scrapeyard-chromium"
      - "seccomp:unconfined"
YAML
    seccomp-log) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    security_opt: !override
      - "no-new-privileges:true"
      - "apparmor:scrapeyard-chromium"
      - "seccomp:$OUT/chromium-log.json"
YAML
    apparmor-unconfined) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    security_opt: !override
      - "no-new-privileges:true"
      - "apparmor:unconfined"
      - "$SECCOMP"
YAML
    apparmor-docker-default) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    security_opt: !override
      - "no-new-privileges:true"
      - "$SECCOMP"
YAML
    caps-default) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    cap_drop: !reset []
YAML
    caps-ptrace) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    cap_add: ["SYS_PTRACE"]
YAML
    pids-unlimited) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    pids_limit: -1
YAML
    no-nnp) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    security_opt: !override
      - "apparmor:scrapeyard-chromium"
      - "$SECCOMP"
YAML
    rw-root) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    read_only: false
YAML
    rw-root-no-tmpfs) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    read_only: false
    tmpfs: !reset []
YAML
    no-init) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    init: false
YAML
    all-lifted) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    security_opt: !override
      - "apparmor:unconfined"
      - "seccomp:unconfined"
    cap_drop: !reset []
    pids_limit: -1
    read_only: false
    tmpfs: !reset []
    init: false
YAML
    ulimit-core0) cat > "$file" <<YAML ;;
services:
  scrapeyard:
    ulimits:
      core:
        soft: 0
        hard: 0
YAML
    *) echo "unknown variant $name" >&2; exit 2 ;;
  esac
  echo "$file"
}

compose() {
  docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.smoke.yml -f "$OUT/diag.yml" -f "$OVERRIDE" "$@"
}

host_facts() {
  echo "::group::Host facts"
  uname -a
  for key in kernel/core_pattern kernel/core_pipe_limit kernel/core_uses_pid fs/suid_dumpable \
      kernel/yama/ptrace_scope kernel/apparmor_restrict_unprivileged_userns kernel/unprivileged_userns_clone \
      kernel/seccomp/actions_logged kernel/printk_ratelimit; do
    printf '%s = %s\n' "$key" "$(cat /proc/sys/$key 2>&1)"
  done
  echo "host shell: ulimit -c soft=$(ulimit -Sc) hard=$(ulimit -Hc)"
  systemctl is-active apport.service apport-autoreport.path systemd-coredump.socket auditd.service 2>&1 | paste -sd' '
  systemctl is-enabled apport.service 2>&1 || true
  cat /etc/default/apport 2>&1 | grep -v '^#' || true
  ls -la /usr/share/apport/apport /usr/lib/systemd/systemd-coredump 2>&1 || true
  sudo ls -la /var/crash /var/lib/apport/coredump /var/lib/systemd/coredump 2>&1 || true
  docker version --format 'docker {{.Server.Version}}'; docker compose version
  docker info --format 'security={{.SecurityOptions}} cgroup={{.CgroupVersion}} driver={{.Driver}} ulimits={{.DefaultUlimits}}' 2>&1 || true
  systemctl show docker.service -p LimitCORE -p LimitNOFILE 2>&1 || true
  sudo cat /etc/docker/daemon.json 2>&1 || true
  df -h / /var/lib/docker 2>&1 | sed 's/^/df /'
  free -h | sed 's/^/mem /'
  echo "::endgroup::"
}

start_variant() {
  VARIANT=$1
  OVERRIDE="$(override "$VARIANT")"
  compose down -v --remove-orphans >/dev/null 2>&1
  if ! compose up -d --no-deps --no-build --force-recreate scrapeyard > "$OUT/up-$VARIANT.log" 2>&1; then
    echo "VARIANT $VARIANT: container did not start"; tail -n 20 "$OUT/up-$VARIANT.log"
    return 1
  fi
  local id; id="$(compose ps -q scrapeyard)"
  docker inspect --format \
    'variant-config security_opt={{.HostConfig.SecurityOpt}} cap_drop={{.HostConfig.CapDrop}} cap_add={{.HostConfig.CapAdd}} read_only={{.HostConfig.ReadonlyRootfs}} pids={{.HostConfig.PidsLimit}} init={{.HostConfig.Init}} ulimits={{.HostConfig.Ulimits}} tmpfs={{len .HostConfig.Tmpfs}} apparmor={{.AppArmorProfile}} nnp={{.HostConfig.SecurityOpt}}' \
    "$id" 2>&1 | sed -E 's/seccomp=\{[^]]{200,}/seccomp=<inline profile>/' | cut -c1-600 | sed "s/^/[$VARIANT] /"
}

probe() {
  # probe KIND STEALTH [PYFIX] [CRASH_WAIT] [OFFLINE_WAIT]
  local kind=$1 stealth=$2 pyfix=${3:-} crash_wait=${4:-12} offline_wait=${5:-5}
  local id="$VARIANT.$kind.s$stealth${pyfix:+.$pyfix}"
  if ! start_variant "$VARIANT"; then
    printf '%s\t%s\t%s\t%s\tNOT STARTED\n' "$VARIANT" "$kind" "$stealth" "${pyfix:--}" >> "$SUMMARY"
    return
  fi
  sudo dmesg -c > /dev/null 2>&1
  bash tests/diag/td71_host_sampler.sh "$OUT/$id.host.txt" &
  local sampler=$!
  echo "=== PROBE $id (crash_wait=${crash_wait}s) $(date -u +%H:%M:%S)"
  timeout $((crash_wait + offline_wait + 180)) docker compose -p "$PROJECT" \
    -f docker-compose.yml -f docker-compose.smoke.yml -f "$OUT/diag.yml" -f "$OVERRIDE" \
    exec -T --user scrapeyard \
    -e TD71_KIND="$kind" -e TD71_STEALTH="$stealth" -e TD71_PYFIX="$pyfix" \
    -e TD71_CRASH_WAIT="$crash_wait" -e TD71_OFFLINE_WAIT="$offline_wait" \
    -e TD71_LABEL="$VARIANT" -e DEBUG=pw:browser \
    scrapeyard python - < tests/diag/td71_probe.py > "$OUT/$id.json" 2> "$OUT/$id.stderr"
  local status=$?
  kill "$sampler" 2>/dev/null; wait "$sampler" 2>/dev/null
  sudo dmesg -c > "$OUT/$id.dmesg" 2>&1
  python3 tests/diag/td71_digest.py "$OUT/$id.json" "$SUMMARY" "$VARIANT" "$kind" "$stealth" "${pyfix:--}" "$status"
  local dumping helpers
  dumping=$(grep -c 'CoreDumping: 1' "$OUT/$id.host.txt" || true)
  helpers=$(grep -c ' helper pid=' "$OUT/$id.host.txt" || true)
  echo "   host: seconds a renderer was seen core-dumping=$dumping; helper sightings=$helpers"
  grep -m 2 ' helper pid=' "$OUT/$id.host.txt" | cut -c1-400 | sed 's/^/   host: /'
  if ((dumping > 0 && STACKS_SHOWN < 4)); then
    STACKS_SHOWN=$((STACKS_SHOWN + 1))
    echo "   host: first kernel stacks of the core-dumping renderer:"
    awk '/STACKS of core-dumping/ {show=1} show {print} /^[0-9.]+ (top|helper|renderer)/ && show && !/STACKS/ {exit}' \
      "$OUT/$id.host.txt" | grep -v -E '^      $' | awk 'length > 0' | head -n 70 | sed 's/^/     /'
  fi
  grep -E -i 'audit|apparmor|seccomp|coredump|core dump|segfault|traps:|trap |oom|killed process|RLIMIT_CORE|Aborting core' \
    "$OUT/$id.dmesg" | cut -c1-330 | sort | uniq -c | sort -rn | head -n 14 | sed 's/^/   dmesg: /'
  grep -o 'type=1326.*' "$OUT/$id.dmesg" | sed -E 's/.*comm="([^"]+)".* syscall=([0-9]+).*/seccomp comm=\1 syscall=\2/' \
    | sort | uniq -c | sort -rn | head -n 20 | sed 's/^/   dmesg seccomp: /'
  grep -o 'apparmor="[A-Z]*".*' "$OUT/$id.dmesg" | sed -E 's/ pid=[0-9]+//; s/ fsuid=[0-9]+//; s/ ouid=[0-9]+//' \
    | cut -c1-260 | sort | uniq -c | sort -rn | head -n 12 | sed 's/^/   dmesg apparmor: /'
  grep -c 'LOW DISK' "$OUT/$id.host.txt" | sed 's/^/   host: low-disk kills=/'
  grep -E 'pw:browser \[pid=[0-9]+\]\[err\]' "$OUT/$id.stderr" | grep -E -i 'signal|crash|dump|fatal|oom|check failed|sandbox' \
    | cut -c1-330 | head -n 8 | sed 's/^/   browser stderr: /'
}

echo "Group $GROUP"
host_facts
sudo security/install-chromium-apparmor-profile.sh install

case "$GROUP" in
  restrictions)
    for VARIANT in control-a seccomp-unconfined seccomp-log apparmor-unconfined apparmor-docker-default \
        caps-default caps-ptrace pids-unlimited no-nnp rw-root rw-root-no-tmpfs no-init all-lifted control-b; do
      for stealth in 0 1; do probe page_crash "$stealth"; done
    done
    ;;
  handling)
    VARIANT=control
    for pyfix in "" rlimit0 rlimit1 filter0 rlimit0+filter0; do
      for stealth in 0 1; do probe page_crash "$stealth" "$pyfix"; done
    done
    VARIANT=ulimit-core0
    for stealth in 0 1; do probe page_crash "$stealth"; done
    # How long does the unhandled case take to end by itself?
    VARIANT=control-long
    for stealth in 0 1; do probe page_crash "$stealth" "" 420 5; done
    ;;
  kinds)
    VARIANT=control
    for kind in sigkill kill_url sigsegv sigabrt nullcrash checkcrash; do
      for stealth in 0 1; do probe "$kind" "$stealth"; done
    done
    for kind in js_heap_oom js_buffer_oom; do
      for stealth in 0 1; do probe "$kind" "$stealth" "" 120 5; done
    done
    VARIANT=control-handled
    for kind in nullcrash checkcrash sigabrt; do
      for stealth in 0 1; do probe "$kind" "$stealth" rlimit0+filter0; done
    done
    for stealth in 0 1; do probe js_heap_oom "$stealth" rlimit0+filter0 120 5; done
    ;;
  *) echo "unknown group $GROUP" >&2; exit 2 ;;
esac

OVERRIDE="$(override control)"
compose down -v --remove-orphans >/dev/null 2>&1
sudo security/install-chromium-apparmor-profile.sh remove || true

echo
echo "=== SUMMARY ($GROUP): variant, kind, stealth, handling, crash event (s), set_offline, fetch (s), note"
column -t -s $'\t' "$SUMMARY" || cat "$SUMMARY"
echo "::group::What the host kept from the crashes"
sudo ls -la /var/crash /var/lib/apport/coredump /var/lib/systemd/coredump 2>&1 || true
sudo tail -n 40 /var/log/apport.log 2>&1 || true
df -h / 2>&1 | sed 's/^/df /'
echo "::endgroup::"
