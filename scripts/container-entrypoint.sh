#!/bin/sh
set -eu

umask 077

if [ "${1:-}" = "healthcheck" ]; then
    exec python -c '
import os
import urllib.request

key = os.environ.get("SCRAPEYARD_HEALTH_PROBE_API_KEY", "")
request = urllib.request.Request(
    "http://127.0.0.1:8420/health/ready",
    headers={"X-API-Key": key},
)
with urllib.request.urlopen(request, timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
'
fi

for path in /data /data/db /data/results /data/adaptive /data/logs; do
    if ! mkdir -p "${path}" 2>/dev/null || ! test -r "${path}" || ! test -w "${path}" || ! test -x "${path}"; then
        echo "scrapeyard: ${path} must be owned and writable by uid 10001; run the documented one-time volume migration" >&2
        exit 78
    fi
done

python -m scrapeyard.runtime.instance_guard -- "$@"

exec "$@"
