#!/bin/sh
set -eu

umask 077

for path in /data /data/db /data/results /data/adaptive /data/logs; do
    if ! mkdir -p "${path}" 2>/dev/null || ! test -r "${path}" || ! test -w "${path}" || ! test -x "${path}"; then
        echo "scrapeyard: ${path} must be owned and writable by uid 10001; run the documented one-time volume migration" >&2
        exit 78
    fi
done

python -m scrapeyard.runtime.instance_guard -- "$@"

exec "$@"
