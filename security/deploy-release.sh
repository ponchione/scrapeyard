#!/usr/bin/env bash
# Guarded promotion of a release archive, then its copy in the off-host store.
# Installed as /usr/local/sbin/scrapeyard-deploy; run as root, normally inside
# its own unit so that a closed connection cannot stop it half way:
#
#   systemd-run --unit scrapeyard-deploy --service-type=oneshot --wait --collect \
#     --property TimeoutStartSec=3h /usr/local/sbin/scrapeyard-deploy ARCHIVE SHA256
#
# ARCHIVE is scrapeyard-release-<commit>.tar.zst as the release workflow
# uploads it, and SHA256 the checksum taken from that workflow's run, not from
# the archive's own .sha256. In order, each step refusing before the next:
#   1. the host check passes as things are;
#   2. the archive has that checksum, holds only its release, and the running
#      release's verifier accepts it with a scan of every image;
#   3. the candidate's SQL is the running release's, so that rolling back to
#      the running release needs no data restore;
#   4. a backup completes and reaches the store;
#   5. scrapeyard-release promote;
#   6. the host check passes again, the public path through Cloudflare
#      included; otherwise the running release is promoted back;
#   7. both releases, the new one as the bytes that were verified, are in the
#      off-host store.
set -euo pipefail

RELEASES=/srv/scrapeyard-releases
CURRENT=/srv/scrapeyard-releases/current
WORK_ROOT=/var/backups/scrapeyard
LOCK_DIR=/run/scrapeyard
BACKUP_STATUS=/var/lib/scrapeyard/backup-status.json
HEALTH_STATUS=/var/lib/scrapeyard/health-status.json
RELEASE_TOOL=/usr/local/sbin/scrapeyard-release
UPLOAD_TOOL=/usr/local/sbin/scrapeyard-release-upload
HEALTH_ATTEMPTS=6
HEALTH_PAUSE=30

say() { echo "scrapeyard deploy: $*"; }
fail() {
  echo "scrapeyard deploy: $*" >&2
  exit "${2:-2}"
}

usage() {
  echo "Usage: scrapeyard-deploy scrapeyard-release-<commit>.tar.zst SHA256" >&2
  exit 2
}

# The UTC time a status file's field names, in seconds, or nothing.
status_field() {
  python3 -I -c '
import json, sys
try:
    print(json.load(open(sys.argv[1]))[sys.argv[2]])
except (OSError, ValueError, KeyError):
    pass
' "$1" "$2"
}

epoch() { date -u -d "$1" +%s 2>/dev/null || echo 0; }

# Runs the host check now and passes when that run found everything well.
host_check_passes() {
  local started
  started=$(date -u +%s)
  systemctl start scrapeyard-health.service || true
  (($(epoch "$(status_field "$HEALTH_STATUS" checked_at)") >= started)) &&
    [[ "$(status_field "$HEALTH_STATUS" status)" == ok ]]
}

host_check_settles() {
  local attempt
  for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
    host_check_passes && return 0
    ((attempt == HEALTH_ATTEMPTS)) || sleep "$HEALTH_PAUSE"
  done
  return 1
}

failing_checks() {
  python3 -I -c '
import json, sys
try:
    results = json.load(open(sys.argv[1])).get("results", [])
except (OSError, ValueError):
    results = []
print(", ".join(r["name"] for r in results if not r.get("ok") and not r.get("warning")) or "unknown")
' "$HEALTH_STATUS"
}

# After a promotion that failed, or a candidate the host check refused: put
# the release that ran before back, unless it is still running and well.
return_to_previous() {
  if [[ "$(readlink -- "$CURRENT")" == "$PREVIOUS" ]] && host_check_passes; then
    fail "$1; $PREVIOUS still runs and the host check passes" 1
  fi
  say "$1; promoting $PREVIOUS again"
  "$RELEASE_TOOL" promote "$PREVIOUS" ||
    fail "$1, and promoting $PREVIOUS again failed too: the application may be stopped" 1
  host_check_settles ||
    fail "$1; $PREVIOUS was promoted again, but the host check fails: $(failing_checks)" 1
  fail "$1; $PREVIOUS was promoted again and the host check passes" 1
}

[[ $# -eq 2 ]] || usage
((EUID == 0)) || fail "run as root"
ARCHIVE=$1
EXPECTED=$2
[[ "$EXPECTED" =~ ^[0-9a-f]{64}$ ]] || fail "the checksum must be 64 lowercase hexadecimal digits"
FILE=$(basename -- "$ARCHIVE")
[[ "$FILE" =~ ^(scrapeyard-release-([0-9a-f]{40}))\.tar\.zst$ ]] || fail "not a release archive: $FILE"
NAME=${BASH_REMATCH[1]}
COMMIT=${BASH_REMATCH[2]}
TARGET=$RELEASES/$NAME
[[ -f "$ARCHIVE" ]] || fail "no file $ARCHIVE"

# Not deploy.lock: the release wrapper takes that one to start the
# application, which the backup and the promotion below both do.
install -d -m 700 "$LOCK_DIR"
exec 8>>"$LOCK_DIR/guarded-deploy.lock"
flock -n 8 || fail "another guarded deployment is under way"

[[ -L "$CURRENT" ]] || fail "$CURRENT is not a link to a promoted release"
PREVIOUS=$(readlink -- "$CURRENT")
[[ -f "$PREVIOUS/manifest.json" ]] || fail "the promoted release $PREVIOUS is missing"
[[ "$PREVIOUS" != "$TARGET" ]] || fail "$NAME is already the promoted release"

# 1. A host that is already failing is no place to judge a new release.
host_check_passes || fail "the host check fails before anything was changed: $(failing_checks)"
say "host check passes; running $(basename -- "$PREVIOUS")"

# 2. A private copy, so the file checked is the file extracted and uploaded.
WORK=$(mktemp -d "$WORK_ROOT/.deploy.XXXXXX")
INCOMING=""
trap 'rm -rf -- "$WORK" ${INCOMING:+"$INCOMING"}' EXIT
install -m 600 -- "$ARCHIVE" "$WORK/$FILE"
[[ "$(sha256sum -- "$WORK/$FILE" | cut -d' ' -f1)" == "$EXPECTED" ]] ||
  fail "$FILE does not have the checksum given; nothing was changed"
# Every member under the release's own directory, no way out of it.
tar --zstd -tf "$WORK/$FILE" > "$WORK/members" || fail "$FILE cannot be read; nothing was changed"
if grep -v -q -E "^$NAME(/|\$)" "$WORK/members" || grep -q -E '(^|/)\.\.(/|$)' "$WORK/members"; then
  fail "$FILE holds something outside $NAME/; nothing was changed"
fi
INCOMING=$(mktemp -d "$RELEASES/.incoming.XXXXXX")
tar --zstd -xpf "$WORK/$FILE" --no-same-owner -C "$INCOMING"
if [[ -e "$TARGET" ]]; then
  cmp -s -- "$TARGET/manifest.json" "$INCOMING/$NAME/manifest.json" ||
    fail "$TARGET is retained already and differs from the archive; nothing was changed"
  say "$NAME is retained already, with the archive's manifest"
else
  mv -T -- "$INCOMING/$NAME" "$TARGET"
fi
rm -rf -- "$INCOMING"
INCOMING=""
# The verifier of the release that runs now, not the candidate's own.
python3 -I "$PREVIOUS/source/scripts/release_artifact.py" verify --scanned "$TARGET" ||
  fail "$NAME fails verification; it is retained but was not promoted"
[[ "$(status_field "$TARGET/manifest.json" revision)" == "$COMMIT" ]] ||
  fail "$NAME's manifest names another revision; it was not promoted"

# 3. Rolling back is only automatic between releases with the same schema.
diff -r -q -- "$PREVIOUS/source/sql" "$TARGET/source/sql" >/dev/null ||
  fail "$NAME changes the SQL of $(basename -- "$PREVIOUS"), so rolling back to it is not proven; promote it by hand (docs/DEPLOYMENT.md)"

# 4. A backup of what runs now, in the store, before anything is replaced.
started=$(date -u +%s)
systemctl start scrapeyard-backup.service || fail "the backup failed; nothing was promoted"
(($(epoch "$(status_field "$BACKUP_STATUS" completed_at)") >= started)) ||
  fail "no backup completed; nothing was promoted"
[[ "$(status_field "$BACKUP_STATUS" uploaded)" == True ]] ||
  fail "the backup did not reach the store; nothing was promoted"
[[ "$(status_field "$BACKUP_STATUS" release)" == "$(basename -- "$PREVIOUS")" ]] ||
  fail "the backup names another release than the one running; nothing was promoted"
say "backup $(basename -- "$(status_field "$BACKUP_STATUS" archive)") is in the store"

# 5 and 6.
"$RELEASE_TOOL" promote "$TARGET" || return_to_previous "promoting $NAME failed"
host_check_settles || return_to_previous "after promoting $NAME the host check fails: $(failing_checks)"
say "promoted $NAME; the host check passes, the public path included"

# 7. The release that runs and the one to roll back to, both off the host.
"$UPLOAD_TOOL" "$NAME" "$WORK/$FILE" ||
  fail "$NAME runs, but it did not reach the store; run scrapeyard-release-upload $NAME" 3
"$UPLOAD_TOOL" "$(basename -- "$PREVIOUS")" ||
  fail "$NAME runs, but the rollback release did not reach the store; run scrapeyard-release-upload $(basename -- "$PREVIOUS")" 3
say "$NAME and its rollback $(basename -- "$PREVIOUS") are in the store"
for release in "$RELEASES"/scrapeyard-release-*; do
  [[ "$release" == "$TARGET" || "$release" == "$PREVIOUS" ]] ||
    say "also retained, and neither running nor the rollback: $(basename -- "$release")"
done
