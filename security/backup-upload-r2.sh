#!/bin/bash
# /etc/scrapeyard/backup-upload: send one backup archive and its checksum to R2.
# Called by scrapeyard-backup as root with the archive's path. Never replaces
# an object: If-None-Match makes the store refuse a name it holds, and the
# bucket's 35-day lock refuses deletion and overwrite whatever the client
# sends (R2 answers 409 for a locked name; a store without a lock, 412).
# Retention is the bucket's lifecycle rule (36 days), not this hook's.
#
# Its store and key are in /etc/scrapeyard/backup-store.json (root, mode 600):
#   {"endpoint": "https://<account id>.r2.cloudflarestorage.com",
#    "bucket": "<bucket>", "access_key_id": "...", "secret_access_key": "..."}
# See docs/DEPLOYMENT.md, "The first production host's store".
set -euo pipefail
archive=$1
store=/etc/scrapeyard/backup-store.json
read_store() { python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$store" "$1"; }
endpoint=$(read_store endpoint)
bucket=$(read_store bucket)
for file in "$archive" "$archive.sha256"; do
  [[ -f "$file" ]] || { echo "missing $file" >&2; exit 1; }
  name=$(basename -- "$file")
  # The credential goes to curl on standard input, never on its command line.
  code=$(printf 'user = "%s:%s"\n' "$(read_store access_key_id)" "$(read_store secret_access_key)" |
    curl --config - --silent --show-error --output /dev/null --write-out '%{http_code}' \
      --max-time 600 --retry 3 --retry-all-errors --retry-delay 10 \
      --aws-sigv4 "aws:amz:auto:s3" -H 'If-None-Match: *' \
      --upload-file "$file" "$endpoint/$bucket/$name")
  case $code in
    200) echo "uploaded $name" ;;
    409|412) echo "$name is already in the store (HTTP $code); it is not replaced" >&2; exit 1 ;;
    *) echo "upload of $name failed: HTTP $code" >&2; exit 1 ;;
  esac
done
