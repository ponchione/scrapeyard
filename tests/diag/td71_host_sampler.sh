#!/usr/bin/env bash
# TD-71 diagnostic (throwaway): once a second, record from the runner host what
# every Chromium renderer and every core-dump helper is doing. Runs until killed.

set -u
OUT=$1
: > "$OUT"
declare -A STACKS_AT=()
HELPER=$(sed -n 's/^|\([^ ]*\).*/\1/p' /proc/sys/kernel/core_pattern)
HELPERS="apport|systemd-coredump${HELPER:+|$HELPER}"

task_stacks() {
  local pid=$1 shown=0 task state
  for task in /proc/"$pid"/task/*; do
    [[ -d "$task" ]] || continue
    state=$(sudo sed -E 's/^.*\) ([A-Za-z]).*$/\1/' "$task/stat" 2>/dev/null)
    echo "  --- task ${task##*/} comm=$(sudo cat "$task/comm" 2>/dev/null) state=$state"
    sudo head -n 24 "$task/stack" 2>/dev/null | sed 's/^/      /'
    shown=$((shown + 1))
    ((shown < 40)) || { echo "  --- (more tasks not shown)"; break; }
  done
}

while :; do
  now=$(date +%s)
  stamp=$(date +%s.%3N)
  for pid in $(pgrep -f -- '--type=renderer' 2>/dev/null); do
    status=$(sudo grep -E '^(State|CoreDumping|NSpid|Threads|VmSize|VmRSS):' "/proc/$pid/status" 2>/dev/null | tr -s '\t ' ' ' | tr '\n' ';')
    [[ -n "$status" ]] || continue
    echo "$stamp renderer pid=$pid $status"
    if [[ "$status" == *"CoreDumping: 1"* ]]; then
      last=${STACKS_AT[$pid]:-0}
      if ((now - last >= 10)); then
        STACKS_AT[$pid]=$now
        echo "$stamp STACKS of core-dumping renderer pid=$pid"
        task_stacks "$pid"
      fi
    fi
  done
  for pid in $(pgrep -f -- "$HELPERS" 2>/dev/null); do
    args=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | cut -c1-300)
    [[ -n "$args" && "$args" != *td71_host_sampler* && "$args" != *pgrep* ]] || continue
    echo "$stamp helper pid=$pid state=$(sudo awk '/^State:/ {print $2}' "/proc/$pid/status" 2>/dev/null) args=$args"
    last=${STACKS_AT[h$pid]:-0}
    if ((now - last >= 10)); then
      STACKS_AT[h$pid]=$now
      sudo head -n 16 "/proc/$pid/stack" 2>/dev/null | sed 's/^/      /'
      sudo ls -l "/proc/$pid/fd" 2>/dev/null | sed 's/^/      fd /' | head -n 12
    fi
  done
  top=$(ps -eo pid,pcpu,stat,comm --sort=-pcpu --no-headers 2>/dev/null | head -n 3 | tr -s ' ' | tr '\n' ';')
  avail=$(df --output=avail -k / | tail -n 1 | tr -d ' ')
  echo "$stamp top $top root_avail_kb=$avail"
  if ((avail < 12 * 1024 * 1024)); then
    # A core written out in full would take the runner down with it.
    echo "$stamp LOW DISK: killing every renderer"
    sudo pkill -9 -f -- '--type=renderer'
  fi
  sleep 1
done
