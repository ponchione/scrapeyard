#!/bin/bash
# Keep a retained release off the host: pack /srv/scrapeyard-releases/<name>
# as the release workflow packs it (root-owned, zstd) and put it, with its
# .sha256, under releases/ in the backup bucket. Run as root:
#   upload-release-r2.sh scrapeyard-release-<commit>
# Uses the store and key of /etc/scrapeyard/backup-store.json, the same as
# the backup hook. Never replaces an object; the bucket's lock keeps it 35
# days, and no lifecycle rule deletes releases/ (see docs/DEPLOYMENT.md).
set -euo pipefail
name=$1
[[ $name =~ ^scrapeyard-release-[0-9a-f]{40}$ ]] || { echo "not a release name: $name" >&2; exit 2; }
releases=/srv/scrapeyard-releases
[[ -d $releases/$name ]] || { echo "no retained release $releases/$name" >&2; exit 2; }
store=/etc/scrapeyard/backup-store.json
read_store() { python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$store" "$1"; }
work=$(mktemp -d /var/backups/scrapeyard/.release-upload.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
archive=$work/$name.tar.zst
# Low priority: the host keeps serving while this runs.
nice -n 19 ionice -c 3 tar -C "$releases" --owner=0 --group=0 --numeric-owner \
  --use-compress-program 'zstd -T2 -10' -cf "$archive" "$name"
(cd "$work" && sha256sum "$name.tar.zst" > "$name.tar.zst.sha256")
size=$(stat -c %s "$archive")
((size < 5000000000)) || { echo "$archive is $size bytes, past a single upload's limit" >&2; exit 1; }
for file in "$archive" "$archive.sha256"; do
  key=releases/$(basename -- "$file")
  code=$(printf 'user = "%s:%s"\n' "$(read_store access_key_id)" "$(read_store secret_access_key)" |
    curl --config - --silent --show-error --output /dev/null --write-out '%{http_code}' \
      --max-time 3600 --retry 3 --retry-all-errors --retry-delay 30 \
      --aws-sigv4 "aws:amz:auto:s3" -H 'If-None-Match: *' \
      --upload-file "$file" "$(read_store endpoint)/$(read_store bucket)/$key")
  case $code in
    200) echo "uploaded $key ($(stat -c %s "$file") bytes)" ;;
    409|412) echo "$key is already in the store (HTTP $code); it is not replaced" >&2; exit 1 ;;
    *) echo "upload of $key failed: HTTP $code" >&2; exit 1 ;;
  esac
done
cat "$archive.sha256"
