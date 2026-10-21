#!/bin/bash
# Keep a retained release off the host: put it, with its .sha256, under
# releases/ in the backup bucket. Run as root:
#   upload-release-r2.sh scrapeyard-release-<commit> [ARCHIVE]
# With ARCHIVE (the release workflow's scrapeyard-release-<commit>.tar.zst,
# already checked), those bytes are uploaded, so the store's checksum is the
# workflow's. Without it, /srv/scrapeyard-releases/<name> is packed as the
# workflow packs it (root-owned, zstd). A release already in the store is left
# as it is and counts as done.
# Uses the store and key of /etc/scrapeyard/backup-store.json, the same as
# the backup hook. Never replaces an object; the bucket's lock keeps it 35
# days, and no lifecycle rule deletes releases/ (see docs/DEPLOYMENT.md).
set -euo pipefail
name=$1
given=${2:-}
[[ $name =~ ^scrapeyard-release-[0-9a-f]{40}$ ]] || { echo "not a release name: $name" >&2; exit 2; }
releases=/srv/scrapeyard-releases
store=/etc/scrapeyard/backup-store.json
read_store() { python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$store" "$1"; }
# s3 KEY [curl options...]: the HTTP status of a signed request for KEY.
s3() {
  local key=$1
  shift
  printf 'user = "%s:%s"\n' "$(read_store access_key_id)" "$(read_store secret_access_key)" |
    curl --config - --silent --show-error --output /dev/null --write-out '%{http_code}' \
      --aws-sigv4 "aws:amz:auto:s3" "$@" "$(read_store endpoint)/$(read_store bucket)/$key"
}
present=0
for suffix in .tar.zst .tar.zst.sha256; do
  code=$(s3 "releases/$name$suffix" --head --max-time 60 --retry 3 --retry-all-errors --retry-delay 10)
  case $code in
    200) present=$((present + 1)) ;;
    404) ;;
    *) echo "asking the store for releases/$name$suffix failed: HTTP $code" >&2; exit 1 ;;
  esac
done
if ((present == 2)); then
  echo "releases/$name.tar.zst is already in the store; it is not replaced"
  exit 0
fi
work=$(mktemp -d /var/backups/scrapeyard/.release-upload.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
archive=$work/$name.tar.zst
if [[ -n $given ]]; then
  [[ $(basename -- "$given") == "$name.tar.zst" && -f $given ]] || { echo "not $name's archive: $given" >&2; exit 2; }
  install -m 600 -- "$given" "$archive"
else
  [[ -d $releases/$name ]] || { echo "no retained release $releases/$name" >&2; exit 2; }
  # Low priority: the host keeps serving while this runs.
  nice -n 19 ionice -c 3 tar -C "$releases" --owner=0 --group=0 --numeric-owner \
    --use-compress-program 'zstd -T2 -10' -cf "$archive" "$name"
fi
(cd "$work" && sha256sum "$name.tar.zst" > "$name.tar.zst.sha256")
size=$(stat -c %s "$archive")
((size < 5000000000)) || { echo "$archive is $size bytes, past a single upload's limit" >&2; exit 1; }
for file in "$archive" "$archive.sha256"; do
  key=releases/$(basename -- "$file")
  code=$(s3 "$key" --max-time 3600 --retry 3 --retry-all-errors --retry-delay 30 \
    -H 'If-None-Match: *' --upload-file "$file")
  case $code in
    200) echo "uploaded $key ($(stat -c %s "$file") bytes)" ;;
    409|412) echo "$key is already in the store (HTTP $code); it is not replaced" >&2; exit 1 ;;
    *) echo "upload of $key failed: HTTP $code" >&2; exit 1 ;;
  esac
done
cat "$archive.sha256"
