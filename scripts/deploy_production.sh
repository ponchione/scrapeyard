#!/usr/bin/env bash
# Deploy the release a dispatched release workflow run built, rehearsed and
# uploaded, to a host with scrapeyard-deploy installed. Run from a checkout,
# on the operator's machine, with gh signed in to the repository:
#
#   scripts/deploy_production.sh RUN_ID SSH_DESTINATION
#
# The archive is taken only from a successful workflow_dispatch run of
# .github/workflows/release-rehearsal.yml on main, for a commit on
# origin/main, and only with the digest GitHub recorded for the artifact.
# The host is given the checksum the run recorded, not one read from the
# archive, and does the rest (security/deploy-release.sh).
set -euo pipefail

WORKFLOW=.github/workflows/release-rehearsal.yml
[[ $# -eq 2 ]] || { echo "Usage: $0 RUN_ID SSH_DESTINATION" >&2; exit 2; }
RUN=$1
HOST=$2
[[ "$RUN" =~ ^[0-9]+$ ]] || { echo "not a run id: $RUN" >&2; exit 2; }
fail() { echo "deploy: $*" >&2; exit 1; }

repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
read -r path event conclusion branch < <(gh api "repos/$repo/actions/runs/$RUN" \
  --jq '[.path, .event, .conclusion, .head_branch] | @tsv') || fail "run $RUN could not be read"
[[ "$path" == "$WORKFLOW" ]] || fail "run $RUN is not of $WORKFLOW"
[[ "$event" == workflow_dispatch && "$branch" == main ]] || fail "run $RUN was not dispatched on main"
[[ "$conclusion" == success ]] || fail "run $RUN did not succeed ($conclusion)"

read -r id name digest expired < <(gh api "repos/$repo/actions/runs/$RUN/artifacts" --jq \
  '[.artifacts[] | select(.name | test("^scrapeyard-release-[0-9a-f]{40}$"))] |
   if length == 1 then .[0] | [.id, .name, .digest, .expired] | @tsv else empty end') || true
[[ -n "${name:-}" ]] || fail "run $RUN has not exactly one release artifact"
[[ "$expired" == false ]] || fail "the artifact of run $RUN has expired"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "GitHub recorded no digest for the artifact"
commit=${name#scrapeyard-release-}
git fetch --quiet origin main
git merge-base --is-ancestor "$commit" origin/main 2>/dev/null || fail "$commit is not on origin/main"

work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
echo "Downloading $name from run $RUN..."
gh api "repos/$repo/actions/artifacts/$id/zip" > "$work/artifact.zip"
[[ "sha256:$(sha256sum "$work/artifact.zip" | cut -d' ' -f1)" == "$digest" ]] ||
  fail "the download does not have the digest GitHub recorded"
unzip -q "$work/artifact.zip" -d "$work/release"
archive=$work/release/$name.tar.zst
(cd "$work/release" && sha256sum -c --quiet "$name.tar.zst.sha256") || fail "the archive fails its checksum"
sum=$(cut -d' ' -f1 "$work/release/$name.tar.zst.sha256")

remote=/home/$(ssh "$HOST" id -un)/$name.tar.zst
echo "Copying it to $HOST..."
scp -q "$archive" "$HOST:$remote"
# The deployment runs in its own unit, so a dropped connection cannot stop it
# half way; its output is printed when it ends (journalctl -fu scrapeyard-deploy).
status=0
# shellcheck disable=SC2029  # $remote and $sum are expanded here on purpose
ssh "$HOST" "since=\$(date +%s); sudo systemd-run --quiet --unit scrapeyard-deploy --service-type=oneshot \
  --wait --collect --property TimeoutStartSec=3h /usr/local/sbin/scrapeyard-deploy '$remote' '$sum'; s=\$?; \
  sudo journalctl --no-pager -o cat -u scrapeyard-deploy --since @\$since; rm -f '$remote'; exit \$s" || status=$?
((status == 0)) || fail "the deployment on $HOST did not complete (exit $status); its output is above"
echo "Deployed $commit (archive SHA-256 $sum)"
