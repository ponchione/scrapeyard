#!/usr/bin/env bash

set -euo pipefail
# Everything this script writes is root's alone, except the status file.
umask 077

ENV_FILE=/etc/scrapeyard/deploy.env
CURRENT=/srv/scrapeyard-releases/current
LOCK_DIR=/run/scrapeyard
RECIPIENTS=/etc/scrapeyard/backup-recipients
UPLOAD_HOOK=/etc/scrapeyard/backup-upload
BACKUP_DIR=/var/backups/scrapeyard
STATUS_FILE=/var/lib/scrapeyard/backup-status.json
UNIT=scrapeyard.service
PROMOTE_UNIT=scrapeyard-promote
TRUSTED_OWNER=0
TRUSTED_ROOT=/
# The restore test's own network, as the first three numbers of a /24. The
# deployment's Compose file takes 172.30.0.0/24, and Docker refuses a network
# that overlaps another one it has.
RESTORE_NETWORK_PREFIX=172.30.1

KEEP=${SCRAPEYARD_BACKUP_KEEP:-7}
IDLE_TIMEOUT=${SCRAPEYARD_BACKUP_IDLE_TIMEOUT_SECONDS:-3600}
IDLE_POLL=${SCRAPEYARD_BACKUP_IDLE_POLL_SECONDS:-30}
MIN_FREE_MB=${SCRAPEYARD_BACKUP_MIN_FREE_MB:-1024}
SNAPSHOT_TIMEOUT=${SCRAPEYARD_BACKUP_SNAPSHOT_TIMEOUT_SECONDS:-900}
UPLOAD_TIMEOUT=${SCRAPEYARD_BACKUP_UPLOAD_TIMEOUT_SECONDS:-1800}
# The two waits of a run while the application is stopped. In 15 runs on the
# first production host (2026-10-14 to 21) the whole stop took 26.5 to 33.4 s:
# the copy a few seconds and the application's start 25 to 32 s. A copy or a
# start that takes longer is a fault, not a slow day; see docs/DEPLOYMENT.md.
COPY_TIMEOUT=${SCRAPEYARD_BACKUP_COPY_TIMEOUT_SECONDS:-120}
START_TIMEOUT=${SCRAPEYARD_BACKUP_START_TIMEOUT_SECONDS:-120}
READY_TIMEOUT=${SCRAPEYARD_BACKUP_READY_TIMEOUT_SECONDS:-300}

STOP_MARKER=$LOCK_DIR/backup-stopped-application
STAGE=""
PARTIAL=""
APPLICATION_STOPPED=0
PROJECT=""
RELEASE=""
COMPOSE_FILES=""
REDIS_KEYS=""
REDIS_LOADED=""
REDIS_EXPIRED=""
MANIFEST_SHA256=""
RESTORING=0
APP=""
REDIS=""
IMAGE=""
VOLUME=""
OWNER=""
UNPACKED_MB=0
LATE_JOBS=()

fail() {
  echo "scrapeyard backup: $*" >&2
  exit 2
}

usage() {
  cat >&2 <<'USAGE'
Usage: scrapeyard-backup run       (run by scrapeyard-backup.service)
       scrapeyard-backup resume    (run by that unit after every run)
       scrapeyard-backup restore-test ARCHIVE --identity FILE
       scrapeyard-backup restore ARCHIVE --identity FILE

Installed as /usr/local/sbin/scrapeyard-backup. run waits until the
application is idle, stops it through scrapeyard.service, copies its data and
Redis's, starts it again, and only then encrypts the copy to the recipients
in /etc/scrapeyard/backup-recipients. restore-test proves that an archive
restores, in a separate Compose project on a network nothing can leave.
restore fills the deployment's own volumes on a host that has none yet.
USAGE
  exit 2
}

require_commands() {
  local command
  for command in "$@"; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required"
  done
}

# The seven functions from here to promotion_is_under_way are the ones in
# manage-release.sh, word for word. Both scripts are installed copies, so
# neither can read the other, and a test holds the two texts together.
require_private_settings() {
  [[ -f "$ENV_FILE" && ! -L "$ENV_FILE" ]] || fail "$ENV_FILE is missing or is a link"
  [[ "$(stat -c '%u %a' "$ENV_FILE")" == "$TRUSTED_OWNER 600" ]] || \
    fail "$ENV_FILE must be owned by root with mode 600"
}

require_project_name() {
  [[ "${COMPOSE_PROJECT_NAME:-}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || \
    fail "$ENV_FILE must set COMPOSE_PROJECT_NAME to the deployment's Compose project"
}

# The settings hold credentials, so they are read here and never handed to
# systemd, which writes a line it cannot parse to the journal, value included.
# A line that is not NAME='value' is refused by its number alone. The only
# COMPOSE_ name accepted is the project: COMPOSE_ENV_FILES would have Compose
# read a second file and print what it cannot parse.
load_settings() {
  local line name number=0
  require_private_settings
  while IFS= read -r line || [[ -n "$line" ]]; do
    number=$((number + 1))
    [[ -n "$line" && "$line" != \#* ]] || continue
    if [[ "$line" =~ ^(SCRAPEYARD_[A-Z0-9_]+|COMPOSE_PROJECT_NAME)=\'([^\']*)\'$ ]]; then
      name=${BASH_REMATCH[1]}
      export "$name=${BASH_REMATCH[2]}"
    else
      fail "$ENV_FILE line $number is not a SCRAPEYARD_ setting or COMPOSE_PROJECT_NAME written as NAME='value'"
    fi
  done <"$ENV_FILE"
  require_project_name
}

# Stopping needs the project name and nothing else. A mistyped line elsewhere
# in the file must not keep the application from being stopped.
load_project_name() {
  local line
  require_private_settings
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^COMPOSE_PROJECT_NAME=\'([^\']*)\'$ ]]; then
      COMPOSE_PROJECT_NAME=${BASH_REMATCH[1]}
    fi
  done <"$ENV_FILE"
  require_project_name
}

# Whoever can write to a directory can replace what is in it, so every
# directory above a path must be root's alone, up to the filesystem root.
# Both the path as it is written and the path it resolves to are walked: a
# directory that only holds a link on the way is as good as the link's target
# to whoever can replace the link.
check_ancestors() {
  local start path offender
  for start in "$(dirname -- "$1")" "$(realpath -- "$(dirname -- "$1")")"; do
    path=$start
    while :; do
      # -H: judge the directory a link on the way points to, not the link.
      offender=$(find -H "$path" -maxdepth 0 \( ! -user "$TRUSTED_OWNER" -o -perm /022 \) -print)
      [[ -z "$offender" ]] || \
        fail "$offender is not owned by root, or others can write to it, so what it holds cannot be trusted"
      [[ "$path" != "$TRUSTED_ROOT" && "$path" != / ]] || break
      path=$(dirname -- "$path")
    done
  done
}

# Root runs this tree at every boot, so no other account may be able to change it.
check_release() {
  local release=$1 offender
  [[ -f "$release/manifest.json" && -x "$release/source/security/deploy-secure-compose.sh" ]] || \
    fail "not a retained release: $release"
  offender=$(find "$release" \( ! -user "$TRUSTED_OWNER" -o -perm /022 \) -print -quit)
  [[ -z "$offender" ]] || \
    fail "$offender is not owned by root, or others can write to it; extract releases as root with tar --no-same-owner"
  check_ancestors "$release"
}

promotion_is_under_way() {
  case "$(systemctl is-active "$PROMOTE_UNIT.service" 2>/dev/null || true)" in
    active|activating|deactivating|reloading) return 0 ;;
    *) return 1 ;;
  esac
}

# A file root reads or runs at every backup: a regular file of root's that
# nobody else can write, in directories that are root's alone.
require_root_file() {
  [[ -f "$1" && ! -L "$1" ]] || fail "$1 is missing, is a link, or is not a regular file"
  [[ -z "$(find "$1" -maxdepth 0 \( ! -user "$TRUSTED_OWNER" -o -perm /022 \) -print)" ]] || \
    fail "$1 is not owned by root, or others can write to it"
  check_ancestors "$1"
}

# The host must not be able to read its own backups, so this file holds public
# keys only. A private key in it is a mistake to stop at, not a setting,
# whatever else the file holds. What a recipient is, age decides: a line it
# does not accept would otherwise be found when the archive is encrypted,
# after the application has been stopped and started for nothing.
check_recipients() {
  require_root_file "$RECIPIENTS"
  ! grep -q 'AGE-SECRET-KEY' "$RECIPIENTS" || \
    fail "$RECIPIENTS holds a private key; it takes public keys only, and while that key is on this host the host can read its own backups"
  age --encrypt --recipients-file "$RECIPIENTS" --output /dev/null </dev/null || \
    fail "age does not accept $RECIPIENTS: every line must be a recipient (age1...) or a comment"
}

# A backup restarts the application, and the unit's start reads the whole
# settings file. A line it cannot read would be found only then, with the
# application already stopped and nothing to start it. The values are read in
# a subshell and go no further.
check_settings() {
  (load_settings) || \
    fail "the application could not be started again with $ENV_FILE as it is; nothing was stopped"
}

# Megabytes free on the filesystem a path is on.
free_mb() {
  df --output=avail -BM "$1" | tail -n 1 | tr -dc 0-9
}

# require_free PATH MEGABYTES WHAT
require_free() {
  local free
  free=$(free_mb "$1") || free=""
  [[ "$free" =~ ^[0-9]+$ ]] || fail "the free space of $1 could not be read"
  ((free >= $2)) || fail "$3 needs $2 MB free in $1, which has $free"
}

# No hook is not an error. One that exists is run as root with the archive,
# so it is held to what a release is held to.
upload_hook_installed() {
  [[ -e "$UPLOAD_HOOK" || -L "$UPLOAD_HOOK" ]] || return 1
  require_root_file "$UPLOAD_HOOK"
  [[ -x "$UPLOAD_HOOK" ]] || fail "$UPLOAD_HOOK is not executable"
}

# One backup, restore test or restore at a time: they share a work directory.
take_backup_lock() {
  install -d -m 700 "$LOCK_DIR"
  exec 8>>"$LOCK_DIR/backup.lock"
  flock --nonblock 8 || fail "another backup, restore test or restore is running"
}

# Plaintext that a killed run left behind. Call it only with the backup lock.
sweep_leftovers() {
  [[ -d "$BACKUP_DIR" ]] || return 0
  find "$BACKUP_DIR" -maxdepth 1 \( -name '.stage.*' -o -name '.*.partial' \) -exec rm -rf -- {} +
}

# A Docker daemon that does not answer must not hold the application down, so
# every call is given a time, and is killed if it ignores the end of it.
# --foreground keeps the call in this script's process group, so Ctrl-C at a
# terminal ends a copy that is running and not only the script waiting for it.
docker_within() {
  local seconds=$1
  shift
  timeout --foreground --kill-after=10 "$seconds" docker "$@"
}

running_containers() {
  docker_within 20 ps -q \
    --filter "label=com.docker.compose.project=$1" \
    --filter "label=com.docker.compose.service=$2"
}

# The one running container of a service. Anything else is refused.
service_container() {
  local found
  found=$(running_containers "$1" "$2") || fail "Docker did not list the $2 containers of $1"
  [[ "$found" =~ ^[a-f0-9]+$ ]] || fail "expected one running $2 container in $1, found ${found:-none}"
  echo "$found"
}

redis_cli() {
  local container=$1
  shift
  docker_within 60 exec "$container" redis-cli "$@" | tr -d '\r'
}

# Keys in every database. SCRAPEYARD_REDIS_DSN may name any of them, DBSIZE
# counts the first alone, and a Redis loading the dump counts them all.
redis_keys() {
  local info
  info=$(redis_cli "$1" INFO keyspace) || return 1
  # An error reply lists no database either, and is not "no keys".
  grep -q '^# Keyspace$' <<< "$info" || return 1
  awk -F '[:=,]' '/^db[0-9]+:keys=/ { keys += $3 } END { print keys + 0 }' <<< "$info"
}

release_image() {
  python3 - "$1/manifest.json" "$2" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1], encoding="utf-8"))["images"][sys.argv[2]])
PY
}

# The account the application's image runs as, which owns its data.
image_user() {
  local user
  user=$(docker_within 20 image inspect --format '{{.Config.User}}' "$1") || \
    fail "the application's image is not loaded: $1"
  [[ "$user" =~ ^[0-9]+:[0-9]+$ ]] || fail "the application's image does not run as a numbered user and group"
  echo "$user"
}

# What is deployed: the promoted release, the two running containers, the
# image and the account the application runs as, and the volume its /data is.
# Anything unexpected is refused. It is called before anything is stopped.
describe_deployment() {
  local project=$1 mount
  check_ancestors "$CURRENT"
  RELEASE=$(realpath -- "$CURRENT")
  check_release "$RELEASE"
  [[ -f "$RELEASE/source/scripts/qualification_backup.py" && -d "$RELEASE/source/sql" ]] || \
    fail "the promoted release has no backup helper; nothing was stopped"
  APP=$(service_container "$project" scrapeyard)
  REDIS=$(service_container "$project" redis)
  IMAGE=$(docker_within 20 inspect --format '{{.Image}}' "$APP") || \
    fail "Docker did not describe the application; nothing was stopped"
  [[ "$IMAGE" == "$(release_image "$RELEASE" scrapeyard)" ]] || \
    fail "the application does not run the promoted release's image; nothing was stopped"
  mount=$(docker_within 20 inspect --format \
    '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Type}}:{{.Name}}{{end}}{{end}}' "$APP") || \
    fail "Docker did not describe the application; nothing was stopped"
  [[ "$mount" =~ ^volume:[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || \
    fail "the application's /data is not a named volume; nothing was stopped"
  VOLUME=${mount#volume:}
  OWNER=$(image_user "$IMAGE")
}

# The megabytes a copy will read, measured while the application runs: the
# volume is mounted read-only into a container that has no network.
data_mb() {
  docker_within 300 run --rm --init --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --pids-limit 64 --user "$OWNER" --entrypoint du \
    --mount "type=volume,source=$VOLUME,target=/source,readonly" \
    "$IMAGE" -sm /source/db /source/results /source/adaptive | awk '{sum += $1} END {print sum + 0}'
}

# Seconds since a `date +%s%N` reading, to one decimal place.
elapsed_since() {
  local tenths=$((($(date +%s%N) - $1) / 100000000))
  echo "$((tenths / 10)).$((tenths % 10))"
}

# Asks the application from inside its own container, so that no credential
# is read on the host or shows in a process list. It ends with 3 for "not
# now" (busy, or not ready yet), with 2 for what waiting cannot cure, and
# with 4 for anything else, of which only the kind is printed.
#   ready           /health/ready answers 200
#   witness         ready and idle; prints what a restore is compared with
#   restored JOB... ready; prints the same of a restored copy, leaving out
#                   the jobs named, which were accepted after the witness
#   envelope JOB    the job's stored configuration is an encryption envelope;
#                   prints its key ID
#   no-route        the container has no route off the host
ask_application() {
  local container=$1
  shift
  docker_within 120 exec -i "$container" python - "$@" <<'PY'
import errno
import hashlib
import json
import os
import socket
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "http://127.0.0.1:8420"
NEVER, NOT_NOW, FAILED = 2, 3, 4
# The request is to this container itself, whatever proxy its environment names.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Refusal(Exception):
    """Its text names no response body and no setting, so it can be printed."""


def get(path, secret):
    request = urllib.request.Request(API + path, headers={"X-API-Key": secret})
    try:
        with OPENER.open(request, timeout=30) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def require_ready(credentials):
    monitors = [c["secret"] for c in credentials.values() if "health-detail" in c["scopes"]]
    if not monitors:
        print("no credential in SCRAPEYARD_API_CREDENTIALS holds the health-detail scope", file=sys.stderr)
        sys.exit(NEVER)
    status, body, _headers = get("/health/ready", monitors[0])
    if status == 503:
        # Not backed up, idle or not: a backup would stop it and start it.
        print("unhealthy: /health/ready answered HTTP 503", file=sys.stderr)
        sys.exit(NOT_NOW)
    if status != 200:
        print(f"not ready: /health/ready answered HTTP {status}", file=sys.stderr)
        sys.exit(NOT_NOW)
    return json.loads(body)


def require_idle(credentials):
    ready = require_ready(credentials)
    workers = ready["workers"]
    depths = list(workers["queue_depths"].values())
    if ready["status"] != "ok" or workers["active_tasks"] != 0 or any(depth != 0 for depth in depths):
        queued = "an unknown number" if None in depths else sum(depths)
        print(
            f"not idle: status {ready['status']}, {workers['active_tasks']} running, {queued} queued",
            file=sys.stderr,
        )
        sys.exit(NOT_NOW)


def list_jobs(credentials):
    """Every job some credential may read, a credential that reads it, and the
    projects those credentials cover (None when one of them covers all)."""
    jobs, readers, projects, everything = {}, {}, set(), False
    # The requests are made with the consumer's own credential and count
    # towards its rate limit, so each asks for the largest page the API allows.
    largest = os.environ.get("SCRAPEYARD_ADMIN_READ_MAX_LIMIT", "")
    page = f"limit={int(largest)}&" if largest.isdigit() and int(largest) > 0 else ""
    for credential in credentials.values():
        if "read" not in credential["scopes"]:
            continue
        everything = everything or not credential.get("projects")
        # A credential limited to projects must name one in every list request.
        for project in credential.get("projects") or [None]:
            projects.add(project)
            offset = 0
            while True:
                query = f"/jobs?{page}offset={offset}"
                if project is not None:
                    query += "&project=" + urllib.parse.quote(project, safe="")
                status, body, headers = get(query, credential["secret"])
                if status != 200:
                    raise Refusal(f"the job list answered HTTP {status}")
                for job in json.loads(body):
                    jobs.setdefault(job["job_id"], job)
                    readers.setdefault(job["job_id"], credential["secret"])
                if headers.get("X-Scrapeyard-Has-More") != "true":
                    break
                offset = int(headers["X-Scrapeyard-Next-Offset"])
    return jobs, readers, None if everything else sorted(projects)


def sample(job_id, secret):
    """What the API returns for one job and its result, as digests."""
    status, body, _headers = get("/jobs/" + urllib.parse.quote(job_id, safe=""), secret)
    if status != 200:
        raise Refusal(f"a job read answered HTTP {status}")
    detail = json.loads(body)
    # The next run is worked out from the clock, not read from what is stored.
    detail.pop("next_run_at", None)
    status, body, _headers = get("/results/" + urllib.parse.quote(job_id, safe=""), secret)
    # 404 is the API's answer for a job that has no result. Any other answer
    # (a rate limit, an error) is not the job's result, and a witness that
    # held it would have a restore compare one refusal with another.
    if status not in (200, 404):
        raise Refusal(f"a result read answered HTTP {status}")
    result = canonical(json.loads(body)) if status == 200 else b""
    return {
        "job_id": job_id,
        "detail_sha256": digest(canonical(detail)),
        # The configuration is stored encrypted and returned decrypted.
        "config_sha256": digest(detail["config_yaml"].encode()),
        "result_status": status,
        "result_bytes": len(result),
        "result_sha256": digest(result),
    }


def witness(credentials, restored=False, left_out=()):
    if not any("read" in c["scopes"] for c in credentials.values()):
        # With no job list the witness holds nothing, and every restore test
        # would compare nothing with nothing and pass.
        print("no credential in SCRAPEYARD_API_CREDENTIALS holds the read scope", file=sys.stderr)
        sys.exit(NEVER)
    # The original is asked while it is idle, so that what it reports is what
    # the copy holds. A restored copy is asked as soon as it is ready: a job
    # accepted after the witness is queued in it, and it runs that job.
    settled = require_ready if restored else require_idle
    settled(credentials)
    jobs, readers, projects = list_jobs(credentials)
    for job_id in left_out:
        jobs.pop(job_id, None)
    samples = {}
    for status in ("complete", "failed"):
        matching = [job for job in jobs.values() if job["status"] == status]
        newest = max(
            matching,
            key=lambda job: (job["last_run_at"] or job["created_at"], job["job_id"]),
            default=None,
        )
        samples[status] = newest and sample(newest["job_id"], readers[newest["job_id"]])
    # A job accepted while the lists were read would be in the data and not here.
    settled(credentials)
    document = {
        "format": "scrapeyard-backup-witness-v1",
        # The projects the job list covers; null when it covers all of them.
        "projects": projects,
        "jobs": [jobs[job_id] for job_id in sorted(jobs)],
        "samples": samples,
    }
    json.dump(document, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


def envelope(job_id):
    path = os.path.join(os.environ.get("SCRAPEYARD_DB_DIR", "/data/db"), "jobs.db")
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = database.execute("SELECT config_yaml FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    finally:
        database.close()
    parts = str(row[0]).split(":", 3) if row else []
    if len(parts) != 4 or parts[:2] != ["syenc", "v1"]:
        raise Refusal("the job's stored configuration is not an encryption envelope")
    print(parts[2])


def no_route():
    # 192.0.2.0/24 is reserved for documentation, so nothing answers there. A
    # container on an internal network has no route to it at all, which the
    # kernel reports at once; with a route, the attempt would time out instead.
    try:
        socket.create_connection(("192.0.2.1", 9), timeout=5).close()
    except OSError as error:
        if error.errno == errno.ENETUNREACH:
            return
        kind = errno.errorcode.get(error.errno or 0, type(error).__name__)
        raise Refusal(f"a connection off the host ended with {kind}, not with 'network is unreachable'")
    raise Refusal("a connection off the host was accepted")


try:
    settings = json.loads(os.environ.get("SCRAPEYARD_API_CREDENTIALS") or "{}")
    mode = sys.argv[1]
    if mode == "ready":
        require_ready(settings)
    elif mode == "witness":
        witness(settings)
    elif mode == "restored":
        witness(settings, restored=True, left_out=sys.argv[2:])
    elif mode == "envelope":
        envelope(sys.argv[2])
    elif mode == "no-route":
        no_route()
    else:
        raise Refusal(f"unknown question: {mode}")
except Refusal as refusal:
    print(refusal, file=sys.stderr)
    sys.exit(FAILED)
except Exception as error:
    # Only its kind: an error's text can quote a response or a setting.
    print(f"could not ask the application: {type(error).__name__}", file=sys.stderr)
    sys.exit(FAILED)
PY
}

# Returns with the application idle, its witness written, and the release
# lock held, so that no start or promotion can begin between "idle" and the
# stop. The lock is the one scrapeyard-release takes. It is held for each
# question only, a second or two, so a promotion never waits for this wait.
wait_until_idle() {
  local project=$1 deadline=$((SECONDS + IDLE_TIMEOUT)) app answer last="" said=""
  install -d -m 700 "$LOCK_DIR"
  exec 7>>"$LOCK_DIR/release.lock"
  while :; do
    ! promotion_is_under_way || fail "a promotion is under way; nothing was stopped"
    if flock --nonblock 7; then
      [[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" == active ]] || \
        fail "$UNIT is no longer active; nothing was stopped"
      app=$(running_containers "$project" scrapeyard) || app=""
      answer=1
      last="no application container is running"
      if [[ "$app" =~ ^[a-f0-9]+$ ]]; then
        answer=0
        ask_application "$app" witness > "$STAGE/archive/witness.json" 2> "$STAGE/asked.log" || answer=$?
        last=$(tail -n 1 "$STAGE/asked.log")
      fi
      case "$answer" in
        0) return 0 ;;
        2) fail "the application cannot be asked whether it is idle (${last:-no reason given}); nothing was stopped" ;;
      esac
      flock --unlock 7
    else
      last="a start, a promotion or the policy watch holds the release lock"
    fi
    # Why it waits is said when the reason changes, and again if the wait ends.
    [[ "$last" == "$said" ]] || echo "scrapeyard backup: waiting: ${last:-no reason given}" >&2
    said=$last
    ((SECONDS < deadline)) || \
      fail "the application was not idle within ${IDLE_TIMEOUT}s (its last answer: ${last:-none}); nothing was stopped"
    sleep "$IDLE_POLL"
  done
}

# Starts the application the only way it may be started: through its unit,
# which installs the egress policy and has the application attest it. The
# unit's start takes the release lock itself, so the lock is given up first.
# A start that fails is retried by the unit, not here. Extra arguments go to
# systemctl.
start_application() {
  local status=0
  exec 7>&-
  APPLICATION_STOPPED=0
  # A start waited for is bounded. systemctl's own wait would last as long as
  # the unit's TimeoutStartSec (600 s), and a start held up by a lock waits
  # that long and is then retried (2026-10-21: 342.8 s down).
  timeout --kill-after=10 "$START_TIMEOUT" systemctl start "$@" "$UNIT" || status=$?
  if ((status == 124 || status == 137)); then
    # Ending systemctl leaves its start job behind. A restart replaces that
    # job, stopping whatever of the start is stuck, and starts the promoted
    # release again, as an operator would.
    echo "scrapeyard backup: $UNIT did not start within ${START_TIMEOUT}s; restarting it" >&2
    systemctl restart --no-block "$UNIT" || \
      echo "scrapeyard backup: $UNIT could not be restarted; see systemctl status $UNIT" >&2
    return 1
  elif ((status != 0)); then
    echo "scrapeyard backup: $UNIT did not start; it retries by itself, see systemctl status $UNIT" >&2
    return 1
  fi
  rm -f -- "$STOP_MARKER"
}

# After a start, asks the application itself until it reports ready.
application_is_ready() {
  local project=$1 deadline=$((SECONDS + 120)) app
  while :; do
    app=$(running_containers "$project" scrapeyard 2>/dev/null) || app=""
    if [[ "$app" =~ ^[a-f0-9]+$ ]] && ask_application "$app" ready 2>/dev/null; then
      return 0
    fi
    ((SECONDS < deadline)) || return 1
    sleep 2
  done
}

# Whatever ends a run after the application was stopped (a failed copy, a
# signal, a timeout) comes through here and starts it again. A run that is
# killed outright cannot; scrapeyard-backup.service then runs `resume`.
finish_run() {
  local status=$?
  trap '' INT TERM HUP
  trap - EXIT
  if ((APPLICATION_STOPPED == 1)); then
    # Handed to systemd and not waited for. When this run ends because its
    # own unit is being stopped, systemd holds the start back until the stop
    # is over, and a wait here would be a wait for this script's own end.
    echo "scrapeyard backup: starting the application again through $UNIT" >&2
    start_application --no-block || true
  fi
  [[ -z "$STAGE" ]] || rm -rf -- "$STAGE"
  [[ -z "$PARTIAL" ]] || rm -f -- "$PARTIAL"
  exit "$status"
}

# Copies Redis's data and the application's while the application is stopped.
snapshot() {
  local redis=$1 release=$2 image=$3 volume=$4 owner=$5 before after
  # The application is the only writer, so while it is stopped only expiry
  # changes Redis. The same count before and after SAVE is therefore the
  # number of keys in the dump, which a restore compares with what it loads.
  for _ in 1 2 3 4 5; do
    before=$(redis_keys "$redis") || fail "Redis did not say how many keys it holds"
    if [[ "$(redis_cli "$redis" SAVE)" == OK ]]; then
      after=$(redis_keys "$redis") || fail "Redis did not say how many keys it holds"
      if [[ "$before" == "$after" && "$after" =~ ^[0-9]+$ ]]; then
        REDIS_KEYS=$after
        break
      fi
    fi
    sleep 2
  done
  [[ -n "$REDIS_KEYS" ]] || fail "Redis did not save a settled copy of its data"
  docker_within 120 cp "$redis:/data/dump.rdb" "$STAGE/archive/redis/dump.rdb" || \
    fail "Redis's dump could not be copied"

  # The helper is the promoted release's own, with that release's SQL, so the
  # set is checked against the schema that wrote it. It runs in the
  # application's image, as the data's owner, without a network, and sees the
  # data volume read-only. SQLite cannot open a database in WAL mode from a
  # read-only directory, so the helper reads a copy of the files, made here
  # and mounted at /data, the path the stored result metadata names.
  install -d -m 700 -o "${owner%:*}" -g "${owner#*:}" "$STAGE/copy" "$STAGE/out"
  docker_within "$COPY_TIMEOUT" run --rm --init --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 \
    --user "$owner" --entrypoint sh \
    --mount "type=volume,source=$volume,target=/source,readonly" \
    --mount "type=bind,source=$STAGE/copy,target=/data" \
    --mount "type=bind,source=$STAGE/out,target=/backup" \
    --mount "type=bind,source=$release/source/scripts/qualification_backup.py,target=/release/scripts/qualification_backup.py,readonly" \
    --mount "type=bind,source=$release/source/sql,target=/release/sql,readonly" \
    "$image" -c 'cp -a /source/db /source/results /source/adaptive /data/ &&
      exec python /release/scripts/qualification_backup.py create --quiesced \
        --data-root /data --output /backup/set --sql-dir /release/sql' \
    > "$STAGE/create.json" || fail "the backup set could not be created"
  [[ -f "$STAGE/out/set/manifest.json" ]] || fail "the helper reported success and wrote no backup set"
  MANIFEST_SHA256=$(sha256sum < "$STAGE/out/set/manifest.json" | cut -d' ' -f1)
}

# One archive: the set, Redis's dump, the witness and a description. It goes
# from tar straight into age, so the plaintext is never one file on disk.
package() {
  local name=$1 created=$2 release=$3 image=$4 project=$5
  # The lock keeps starts and promotions out between the witness and the
  # stop. It does not keep submissions out: a job accepted in that second is
  # in the copied database and not in the witness. Such jobs are named in the
  # description, so that a restore test leaves out exactly those and still
  # fails for any other difference. The database is opened immutable: the
  # set's manifest lists its files, and a reader that left one would spoil it.
  python3 - "$created" "$(basename -- "$release")" "$image" "$project" "$REDIS_KEYS" "$MANIFEST_SHA256" \
    "$STAGE/archive/witness.json" "$STAGE/out/set/payload/db/jobs.db" \
    > "$STAGE/archive/metadata.json" <<'PY' || fail "the copied jobs.db could not be compared with the witness"
import json
import sqlite3
import sys
import urllib.parse

created, release, image, project, keys, manifest, witness_path, database_path = sys.argv[1:]
witness = json.load(open(witness_path, encoding="utf-8"))
witnessed = {job["job_id"] for job in witness["jobs"]}
covered = witness["projects"]
database = sqlite3.connect(f"file:{urllib.parse.quote(database_path)}?mode=ro&immutable=1", uri=True)
try:
    stored = database.execute("SELECT job_id, project FROM jobs").fetchall()
finally:
    database.close()
late = sorted(
    job_id
    for job_id, job_project in stored
    if job_id not in witnessed and (covered is None or job_project in covered)
)
# A restore passes the names on as arguments, so an archive names at most
# this many; the count says whether it named them all.
LIMIT = 1000
if late:
    print(
        f"scrapeyard backup: {len(late)} job(s) were accepted after the witness was taken; "
        "they are in the archive and named in its metadata.json"
        + (
            f" (the first {LIMIT} of them: its restore test cannot compare the copy)"
            if len(late) > LIMIT
            else ""
        ),
        file=sys.stderr,
    )
description = {
    "format": "scrapeyard-host-backup-v1",
    "created_at": created,
    "release": release,
    "application_image": image,
    "compose_project": project,
    "redis_keys": int(keys),
    "manifest_sha256": manifest,
    "accepted_after_witness": late[:LIMIT],
    "accepted_after_witness_count": len(late),
}
print(json.dumps(description, indent=2, sort_keys=True))
PY
  mv -T -- "$STAGE/out/set" "$STAGE/archive/set"
  rm -rf -- "$STAGE/copy" "$STAGE/out" "$STAGE/create.json" "$STAGE/asked.log"
  PARTIAL="$BACKUP_DIR/.$name.partial"
  tar --zstd -C "$STAGE/archive" --owner=0 --group=0 --numeric-owner -cf - . | \
    age --encrypt --recipients-file "$RECIPIENTS" --output "$PARTIAL" || \
    fail "the archive could not be encrypted"
  [[ -s "$PARTIAL" ]] || fail "age wrote an empty archive"
  rm -rf -- "$STAGE"
  STAGE=""
  [[ ! -e "$BACKUP_DIR/$name" ]] || fail "$BACKUP_DIR/$name exists already; nothing is overwritten"
  mv -T -- "$PARTIAL" "$BACKUP_DIR/$name"
  PARTIAL=""
}

# Keeps the newest archives by name, which carries the UTC time of the copy.
# Only files this script names are counted or removed, and never the archive
# just written, whatever the clock said when it was named.
prune() {
  local newest=$1 name
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    -name 'scrapeyard-backup-????????T??????Z.tar.zst.age' -printf '%f\n' | sort -r | \
    tail -n "+$((KEEP + 1))" | while IFS= read -r name; do
    [[ "$name" != "$newest" ]] || continue
    rm -f -- "$BACKUP_DIR/$name" "$BACKUP_DIR/$name.sha256"
  done
}

# What a monitoring check reads for a backup-age alert. It holds no secret.
write_status() {
  [[ -d "$(dirname -- "$STATUS_FILE")" ]] || install -d -m 755 "$(dirname -- "$STATUS_FILE")"
  python3 - "$STATUS_FILE" "$@" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, archive, size, sha256, uploaded, down, waited, release = sys.argv[1:]
status = {
    "format": "scrapeyard-backup-status-v1",
    "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "archive": archive,
    "bytes": int(size),
    "sha256": sha256,
    "uploaded": uploaded == "yes",
    "application_down_seconds": float(down),
    "waited_for_idle_seconds": int(waited),
    "release": release,
}
with open(path + ".new", "w", encoding="utf-8") as handle:
    json.dump(status, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.chmod(path + ".new", 0o644)
os.replace(path + ".new", path)
PY
}

# Compose as the release's own wrapper runs it: in the release's source
# directory, with the release's files. The project is always named here, and
# no command is waited for without end.
compose() {
  (cd "$RELEASE/source" && COMPOSE_FILE="$COMPOSE_FILES" \
    docker_within "$SNAPSHOT_TIMEOUT" compose --progress quiet --project-name "$PROJECT" "$@")
}

# What exists of the restore test's project. A listing that fails is a
# failure, not an empty list: "removed" is said of what was looked for.
isolated_resources() {
  docker_within 60 ps -aq --filter "label=com.docker.compose.project=$PROJECT" &&
    docker_within 60 volume ls -q --filter "label=com.docker.compose.project=$PROJECT" &&
    docker_within 60 network ls -q --filter "label=com.docker.compose.project=$PROJECT"
}

# Removes every container, volume and network of the restore test's project.
# The name is checked here so that this can never be pointed at the deployment.
remove_isolated_project() {
  local ids
  [[ "$PROJECT" == *-restore-test ]] || return 1
  ids=$(docker_within 60 ps -aq --filter "label=com.docker.compose.project=$PROJECT") || ids=""
  # shellcheck disable=SC2086  # one ID a word
  [[ -z "$ids" ]] || docker_within 120 rm -f -v $ids >/dev/null 2>&1 || true
  ids=$(docker_within 60 volume ls -q --filter "label=com.docker.compose.project=$PROJECT") || ids=""
  # shellcheck disable=SC2086
  [[ -z "$ids" ]] || docker_within 120 volume rm $ids >/dev/null 2>&1 || true
  ids=$(docker_within 60 network ls -q --filter "label=com.docker.compose.project=$PROJECT") || ids=""
  # shellcheck disable=SC2086
  [[ -z "$ids" ]] || docker_within 120 network rm $ids >/dev/null 2>&1 || true
}

# Two things, asked before the application is started on the network. That
# Docker holds it to be internal: nothing on it is routed off the host. And
# that its bridge has no address of this host's: nothing on it reaches the
# host's own services either, sshd among them. The second is asked of the
# kernel. Docker 28 and later leave the bridge without an address when asked;
# 27.5.1 refuses the option, and 26.1.4 takes it and gives the bridge an
# address all the same (tried on a runner, 2026-10-13).
require_isolated_network() {
  local described bridge addresses
  described=$(docker_within 20 network inspect --format '{{.Internal}} {{.Id}}' "${PROJECT}_backend" 2>/dev/null) || \
    described=""
  [[ "$described" =~ ^true\ [a-f0-9]{64}$ ]] || \
    fail "the restore test's network is not internal; the application was not started on it"
  bridge="br-${described:5:12}"
  addresses=$(ip -4 -o address show dev "$bridge") || \
    fail "the restore test's bridge $bridge could not be read; the application was not started on its network"
  [[ -z "$addresses" ]] || \
    fail "the restore test's bridge has an address of this host's, so the copy could reach the host's own services; the application was not started on it"
}

# The end of a restore test, however it ends: nothing of it stays on the
# host, and if that cannot be confirmed it is said, and the test has failed.
finish_restore_test() {
  local status=$? left
  trap '' INT TERM HUP
  trap - EXIT
  set +e
  remove_isolated_project
  [[ -z "$STAGE" ]] || rm -rf -- "$STAGE"
  if ! left=$(isolated_resources) || [[ -n "$left" ]]; then
    echo "scrapeyard backup: $PROJECT could not be removed, or Docker did not say what is left of it; see docker ps -a, docker volume ls and docker network ls" >&2
    ((status != 0)) || status=2
  fi
  exit "$status"
}

# The end of a restore. What it put into the deployment's volumes is left for
# the operator to judge: this script removes nothing from that project.
finish_restore() {
  local status=$?
  trap '' INT TERM HUP
  trap - EXIT
  [[ -z "$STAGE" ]] || rm -rf -- "$STAGE"
  if ((status != 0 && RESTORING == 1)); then
    echo "scrapeyard backup: the restore did not complete; remove the containers and volumes of $PROJECT it created before trying again" >&2
  fi
  exit "$status"
}

# Decrypts and unpacks an archive into the work directory, checks what came
# out of it, and selects the retained release that made it: a set validates
# against the SQL of the release that wrote it, and no other.
unpack_archive() {
  local archive=$1 identity=$2 unexpected part name created keys manifest unnamed bytes
  [[ -f "$archive" ]] || fail "no such archive: $archive"
  [[ -n "$(find "$identity" -maxdepth 0 -type f -user "$TRUSTED_OWNER" ! -perm /077 -print 2>/dev/null)" ]] || \
    fail "the identity must be a regular file of root's that nobody else can read (mode 600)"
  install -d -m 700 "$BACKUP_DIR"
  # What the archive unpacks to is measured before any of it is written: the
  # disk a test would fill is the disk the deployment writes to.
  bytes=$(age --decrypt --identity "$identity" "$archive" | zstd -dc | wc -c) || \
    fail "the archive could not be decrypted"
  UNPACKED_MB=$((bytes / 1048576 + 1))
  require_free "$BACKUP_DIR" $((UNPACKED_MB + MIN_FREE_MB)) "unpacking the archive ($UNPACKED_MB MB, and $MIN_FREE_MB MB beside)"
  STAGE=$(mktemp -d "$BACKUP_DIR/.stage.XXXXXX")
  age --decrypt --identity "$identity" "$archive" | \
    tar --zstd -x -C "$STAGE" --no-same-owner --no-same-permissions -f - || \
    fail "the archive could not be decrypted and unpacked"
  # age encrypts to a public key and signs nothing, so an archive says nothing
  # about who made it. Nothing in it is trusted before it has been checked.
  unexpected=$(find "$STAGE" ! -type f ! -type d -print -quit)
  [[ -z "$unexpected" ]] || fail "the archive holds something that is neither a file nor a directory"
  for part in metadata.json witness.json redis/dump.rdb set/manifest.json; do
    [[ -f "$STAGE/$part" ]] || fail "the archive has no $part"
  done
  {
    read -r name created keys manifest unnamed
    read -r -a LATE_JOBS
  } < <(python3 - "$STAGE/metadata.json" <<'PY'
import json
import re
import sys

try:
    described = json.load(open(sys.argv[1], encoding="utf-8"))
    fields = (
        described["release"],
        described["created_at"],
        str(described["redis_keys"]),
        described["manifest_sha256"],
    )
    patterns = (r"[A-Za-z0-9][A-Za-z0-9._-]*", r"[0-9TZ:-]+", r"0|[1-9][0-9]{0,11}", r"[a-f0-9]{64}")
    # An archive from before 2026-10-14 names no jobs accepted after its
    # witness: an empty list stands for it, and "old" says it is one.
    late = described.get("accepted_after_witness", [])
    count = described.get("accepted_after_witness_count", len(late))
    if (
        described["format"] != "scrapeyard-host-backup-v1"
        or not all(
            isinstance(field, str) and re.fullmatch(pattern, field)
            for field, pattern in zip(fields, patterns)
        )
        or not isinstance(late, list)
        or len(late) > 1000
        # They are passed on as arguments and printed, and they come from the archive.
        or not all(isinstance(job, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", job) for job in late)
        or type(count) is not int
        or not len(late) <= count <= 10**12
    ):
        raise ValueError
except Exception:
    sys.exit(1)
unnamed = "old" if "accepted_after_witness" not in described else str(count - len(late))
print(*fields, unnamed)
print(*late)
PY
  ) || true
  [[ -n "${manifest:-}" ]] || fail "the archive's metadata.json is not one this script writes"
  if [[ "$ACTION" == restore-test && "$unnamed" == old ]]; then
    fail "the archive names no jobs accepted after its witness, as backup scripts did before 2026-10-14 (commit fe6c01c), so a restore test cannot tell them from a difference; restore it instead, or test it with the backup script that wrote it"
  fi
  if [[ "$ACTION" == restore-test && "$unnamed" != 0 ]]; then
    fail "$((unnamed + ${#LATE_JOBS[@]})) jobs were accepted after the witness was taken, more than the ${#LATE_JOBS[@]} an archive names, so a restore test cannot tell them from a difference; restore it instead"
  fi
  [[ "$(sha256sum < "$STAGE/set/manifest.json" | cut -d' ' -f1)" == "$manifest" ]] || \
    fail "the backup set's manifest is not the one the archive was made with"
  RELEASE="$(dirname -- "$CURRENT")/$name"
  [[ -d "$RELEASE" ]] || \
    fail "the archive was made by $name, which is not retained in $(dirname -- "$CURRENT"); extract that release there first"
  RELEASE=$(realpath -- "$RELEASE")
  check_release "$RELEASE"
  REDIS_KEYS=$keys
  MANIFEST_SHA256=$manifest
  echo "Archive made $created by $name: unpacked $(du -sb "$STAGE" | cut -f1) bytes"
}

# The helper's own check of the set: every file's size and checksum, SQLite's
# integrity check, and the migration ledger against the release's SQL.
validate_set() {
  local image=$1 owner=$2
  chown -R "$owner" "$STAGE/set"
  docker_within "$SNAPSHOT_TIMEOUT" run --rm --init --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 64 \
    --user "$owner" --entrypoint python \
    --mount "type=bind,source=$STAGE/set,target=/backup/set,readonly" \
    --mount "type=bind,source=$RELEASE/source/scripts/qualification_backup.py,target=/release/scripts/qualification_backup.py,readonly" \
    --mount "type=bind,source=$RELEASE/source/sql,target=/release/sql,readonly" \
    "$image" /release/scripts/qualification_backup.py validate \
    --backup /backup/set --sql-dir /release/sql > "$STAGE/validate.json" || \
    fail "the backup set in the archive is not valid"
}

# Redis with --appendonly yes, as the Compose file starts it, ignores a bare
# dump.rdb and starts empty (7.4.5, tried). So the dump is loaded by a Redis
# started without the append-only file, which is then switched on and written
# before that Redis is shut down. The service's own start finds it afterwards.
# Prints the keys loaded and the keys found expired.
# shellcheck disable=SC2016  # a script for the container's shell
REDIS_LOAD='set -eu
cp /dump/dump.rdb /data/dump.rdb
redis-server --appendonly no --bind 127.0.0.1 >/dev/null &
field() { redis-cli info persistence | tr -d "\r" | sed -n "s/^$1://p"; }
tries=0
until [ "$(redis-cli ping 2>/dev/null)" = PONG ]; do
  tries=$((tries + 1)); [ "$tries" -lt 300 ] || exit 1
  sleep 0.2
done
loaded=$(field rdb_last_load_keys_loaded)
expired=$(field rdb_last_load_keys_expired)
[ "$(redis-cli config set appendonly yes)" = OK ]
tries=0
until [ "$(field aof_enabled)$(field aof_rewrite_in_progress)$(field aof_rewrite_scheduled)$(field aof_last_bgrewrite_status)" = 100ok ] &&
  [ -s /data/appendonlydir/appendonly.aof.manifest ]; do
  tries=$((tries + 1)); [ "$tries" -lt 300 ] || exit 1
  sleep 0.2
done
redis-cli shutdown >/dev/null 2>&1 || true
wait || true
echo "$loaded $expired"'

# Fills the project's two volumes from the unpacked archive. Compose creates
# them, so they are the volumes its services mount afterwards.
restore_volumes() {
  local owner=$1 loaded="" expired="" docker_root
  docker_root=$(docker_within 20 info --format '{{.DockerRootDir}}') || \
    fail "Docker did not say where it keeps its volumes"
  require_free "$docker_root" $((UNPACKED_MB + MIN_FREE_MB)) "restoring into fresh volumes ($UNPACKED_MB MB, and $MIN_FREE_MB MB beside)"
  compose run --rm --no-deps -T --entrypoint python \
    -v "$STAGE/set:/backup/set:ro" \
    -v "$RELEASE/source/scripts/qualification_backup.py:/release/scripts/qualification_backup.py:ro" \
    -v "$RELEASE/source/sql:/release/sql:ro" \
    scrapeyard /release/scripts/qualification_backup.py restore \
    --backup /backup/set --data-root /data --sql-dir /release/sql > "$STAGE/restore.json" || \
    fail "the backup set could not be restored into a fresh data volume"
  # Inside the private work directory; Redis's own account has to read it.
  chmod 755 "$STAGE/redis"
  chmod 644 "$STAGE/redis/dump.rdb"
  read -r loaded expired < <(compose run --rm --no-deps -T --entrypoint sh \
    -v "$STAGE/redis:/dump:ro" redis -c "$REDIS_LOAD") || true
  [[ "$loaded" =~ ^[0-9]+$ && "$expired" =~ ^[0-9]+$ ]] || fail "Redis did not load the dump"
  # Keys whose time ran out since the backup are counted, not loaded.
  ((loaded + expired == REDIS_KEYS)) || \
    fail "the dump was made with $REDIS_KEYS keys, but Redis loaded $loaded and found $expired expired"
  REDIS_LOADED=$loaded
  REDIS_EXPIRED=$expired
}

[[ $# -ge 1 ]] || usage
ACTION=$1
shift
((EUID == 0)) || fail "run as root"

case "$ACTION" in
  run)
    [[ $# -eq 0 ]] || usage
    require_commands docker systemctl flock timeout age tar zstd sha256sum python3 df awk
    for setting in KEEP IDLE_TIMEOUT IDLE_POLL MIN_FREE_MB SNAPSHOT_TIMEOUT UPLOAD_TIMEOUT COPY_TIMEOUT START_TIMEOUT; do
      [[ "${!setting}" =~ ^(0|[1-9][0-9]{0,6})$ ]] || fail "a SCRAPEYARD_BACKUP_ setting is not a whole number: $setting"
    done
    ((KEEP >= 1 && IDLE_POLL >= 1 && SNAPSHOT_TIMEOUT >= 1 && UPLOAD_TIMEOUT >= 1 && COPY_TIMEOUT >= 1 && START_TIMEOUT >= 1)) || \
      fail "SCRAPEYARD_BACKUP_KEEP, the poll interval and the timeouts must be 1 or more"
    load_project_name
    project=$COMPOSE_PROJECT_NAME
    take_backup_lock
    if [[ -e "$STOP_MARKER" ]]; then
      echo "An earlier backup stopped the application and did not start it again; starting $UNIT first..."
      systemctl start "$UNIT" || fail "$UNIT did not start"
      rm -f -- "$STOP_MARKER"
    fi
    sweep_leftovers

    # Everything that can be refused is refused here, before anything is stopped.
    check_recipients
    uploading=no
    ! upload_hook_installed || uploading=yes
    [[ -L "$CURRENT" ]] || fail "$CURRENT is not a link to a promoted release"
    install -d -m 700 "$BACKUP_DIR"
    check_ancestors "$BACKUP_DIR/archive"
    check_settings
    ! promotion_is_under_way || fail "a promotion is under way; nothing was stopped"
    [[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" == active ]] || \
      fail "$UNIT is not active, so no application is running to back up; nothing was stopped"
    describe_deployment "$project"
    # The copy of the files and the set made from it are on the disk together
    # for a moment, and a copy that runs out of room is found only with the
    # application stopped.
    data=$(data_mb) || fail "the application's data could not be measured; nothing was stopped"
    require_free "$BACKUP_DIR" $((2 * data + MIN_FREE_MB)) \
      "nothing was stopped: a backup of $data MB of data (twice that, and $MIN_FREE_MB MB beside)"

    STAGE=$(mktemp -d "$BACKUP_DIR/.stage.XXXXXX")
    mkdir -p "$STAGE/archive/redis"
    trap finish_run EXIT
    trap 'exit 143' INT TERM HUP
    echo "Waiting until the application is idle, for at most ${IDLE_TIMEOUT}s..."
    waiting_since=$SECONDS
    wait_until_idle "$project"
    waited=$((SECONDS - waiting_since))

    # The lock is held from here. A promotion may have moved the link during
    # the wait, and the settings file may have been edited, so what is
    # deployed and what a start would read are both looked at again.
    describe_deployment "$project"
    check_settings

    echo "The application is idle; stopping it through $UNIT..."
    created=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    name="scrapeyard-backup-$(date -u -d "$created" +%Y%m%dT%H%M%SZ).tar.zst.age"
    stopped_at=$(date +%s%N)
    # Written before the stop: from here on the application must be started
    # again, by this run or, if it is killed, by `resume`.
    : > "$STOP_MARKER"
    APPLICATION_STOPPED=1
    timeout --kill-after=10 180 systemctl stop "$UNIT" || fail "$UNIT could not be stopped"
    # systemctl reports success for a stop whose own command failed, and a
    # Docker that does not answer says nothing about what is running.
    left=$(running_containers "$project" scrapeyard) || \
      fail "Docker did not say whether the application has stopped"
    [[ -z "$left" ]] || fail "the application is still running after $UNIT was stopped"
    snapshot "$REDIS" "$RELEASE" "$IMAGE" "$VOLUME" "$OWNER"

    # The application comes back before anything is compressed or encrypted.
    back=yes
    start_application || back=no
    down=$(elapsed_since "$stopped_at")
    if [[ "$back" == yes ]] && ! application_is_ready "$project"; then
      echo "scrapeyard backup: $UNIT started, but the application does not report ready" >&2
      back=no
    fi
    [[ "$back" == no ]] || echo "The application is back and ready; it was down for ${down}s"

    package "$name" "$created" "$RELEASE" "$IMAGE" "$project"
    archive="$BACKUP_DIR/$name"
    sum=$(sha256sum < "$archive" | cut -d' ' -f1)
    size=$(stat -c %s "$archive")
    printf '%s  %s\n' "$sum" "$name" > "$archive.sha256"
    echo "Wrote $archive: $size bytes, sha256 $sum"
    prune "$name"

    if [[ "$uploading" == yes ]]; then
      timeout --kill-after=10 "$UPLOAD_TIMEOUT" "$UPLOAD_HOOK" "$archive" || \
        fail "$UPLOAD_HOOK failed; the archive is on this host only"
      echo "$UPLOAD_HOOK took the archive"
    else
      echo "No $UPLOAD_HOOK is installed: the archive is kept on the host only"
    fi
    [[ "$back" == yes ]] || \
      fail "the archive was written, but the application did not come back ready; see systemctl status $UNIT"
    write_status "$archive" "$size" "$sum" "$uploading" "$down" "$waited" "$(basename -- "$RELEASE")"
    echo "Backup complete"
    ;;
  resume)
    # Run by the unit after every run, however it ended. It starts the
    # application if a run stopped it and was killed before starting it again,
    # and removes the plaintext such a run left.
    [[ $# -eq 0 ]] || usage
    install -d -m 700 "$LOCK_DIR"
    if [[ -e "$STOP_MARKER" ]]; then
      echo "scrapeyard backup: a backup stopped the application and did not start it again; starting $UNIT" >&2
      # Not waited for: while the backup unit is still stopping, systemd holds
      # this start back, and waiting for it here would wait for itself.
      systemctl start --no-block "$UNIT"
      rm -f -- "$STOP_MARKER"
    fi
    exec 8>>"$LOCK_DIR/backup.lock"
    ! flock --nonblock 8 || sweep_leftovers
    ;;
  restore-test|restore)
    [[ $# -eq 3 && "$2" == "--identity" ]] || usage
    require_commands docker systemctl flock timeout age tar zstd sha256sum python3 cmp du df wc ip
    docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
    [[ "$READY_TIMEOUT" =~ ^[1-9][0-9]{0,6}$ && "$SNAPSHOT_TIMEOUT" =~ ^[1-9][0-9]{0,6}$ ]] || \
      fail "a SCRAPEYARD_BACKUP_ timeout is not a whole number above zero"
    [[ "$MIN_FREE_MB" =~ ^(0|[1-9][0-9]{0,6})$ ]] || fail "SCRAPEYARD_BACKUP_MIN_FREE_MB is not a whole number"
    started_at=$(date +%s%N)
    take_backup_lock
    sweep_leftovers
    load_settings
    deployment=$COMPOSE_PROJECT_NAME
    # Compose reads this name from the environment. It is taken out so that no
    # command below can reach the deployment's project by default.
    unset COMPOSE_PROJECT_NAME

    if [[ "$ACTION" == restore ]]; then
      PROJECT=$deployment
      case "$(systemctl is-active "$UNIT" 2>/dev/null || true)" in
        active|activating|deactivating|reloading)
          fail "$UNIT is running; restore fills the volumes of a host that has none and replaces nothing" ;;
      esac
      containers=$(docker_within 60 ps -aq --filter "label=com.docker.compose.project=$PROJECT") || \
        fail "Docker did not list the containers of $PROJECT"
      volumes=$(docker_within 60 volume ls -q --filter "label=com.docker.compose.project=$PROJECT") || \
        fail "Docker did not list the volumes of $PROJECT"
      [[ -z "$containers$volumes" ]] || \
        fail "the Compose project $PROJECT has containers or volumes already; restore replaces nothing"
      trap finish_restore EXIT
      trap 'exit 143' INT TERM HUP
      unpack_archive "$(realpath -- "$1")" "$3"
      # A rebuilt host has not loaded the release yet, and Compose starts the
      # application's image under its AppArmor profile even to copy files.
      PYTHONDONTWRITEBYTECODE=1 python3 "$RELEASE/source/scripts/release_artifact.py" load "$RELEASE"
      "$RELEASE/source/security/install-chromium-apparmor-profile.sh" install
      image=$(release_image "$RELEASE" scrapeyard)
      owner=$(image_user "$image")
      validate_set "$image" "$owner"
      COMPOSE_FILES="$RELEASE/source/docker-compose.yml:$RELEASE/images.json"
      RESTORING=1
      restore_volumes "$owner"
      RESTORING=0
      echo "Restored into the volumes of $PROJECT: the backup set, and $REDIS_LOADED Redis keys ($REDIS_EXPIRED had expired since the backup)"
      echo "Start it as a first release is started: link $CURRENT to $RELEASE, then systemctl enable --now $UNIT"
      exit 0
    fi

    PROJECT="$deployment-restore-test"
    trap finish_restore_test EXIT
    trap 'exit 143' INT TERM HUP
    left=$(isolated_resources) || fail "Docker did not list what an earlier restore test may have left of $PROJECT"
    if [[ -n "$left" ]]; then
      echo "Removing what an earlier restore test left of $PROJECT..."
      remove_isolated_project
    fi
    unpack_archive "$(realpath -- "$1")" "$3"
    image=$(release_image "$RELEASE" scrapeyard)
    for service in scrapeyard redis; do
      docker_within 20 image inspect "$(release_image "$RELEASE" "$service")" >/dev/null 2>&1 || \
        fail "the $service image of $RELEASE is not loaded on this host"
    done
    owner=$(image_user "$image")
    validate_set "$image" "$owner"
    echo "Decrypted, unpacked and validated in $(elapsed_since "$started_at")s"

    # Created here and not by Compose, so that what isolates it is in plain
    # sight. Internal: nothing on it can leave the host, no retailer request
    # and no webhook, whatever the restored jobs and deliveries ask for. And
    # with no address of the host's on its bridge: the copy cannot reach the
    # host's own services, which the deployment's egress policy would refuse
    # it and which no policy covers here.
    docker_within 60 network create --internal --subnet "$RESTORE_NETWORK_PREFIX.0/24" \
      --opt com.docker.network.bridge.gateway_mode_ipv4=isolated \
      --label "com.docker.compose.project=$PROJECT" "${PROJECT}_backend" >/dev/null || \
      fail "the restore test's network could not be created; is $RESTORE_NETWORK_PREFIX.0/24 in use on this host, or is Docker older than 28.0?"
    require_isolated_network
    # The release's Compose file gives the application its confinement. This
    # file changes only what an isolated copy needs: its own network, no
    # restarts, no egress probe and no proxy. The application checks the host
    # egress policy at startup only while its probe is configured, and a
    # production host is not given a second firewall policy for a test. A
    # deployment's proxy is of no use to a copy that can reach nothing, and
    # one named by a host name would keep it from starting: the application
    # resolves that name at its start. The copy also never removes old
    # history, so it can be compared with the original.
    cat > "$STAGE/isolated.yml" <<YAML
services:
  scrapeyard:
    restart: "no"
    environment:
      SCRAPEYARD_UNTRUSTED_SUBMISSIONS: "false"
      SCRAPEYARD_PROXY_URL: ""
      SCRAPEYARD_EGRESS_POLICY_PROBE_HOST: ""
      SCRAPEYARD_EGRESS_POLICY_PROBE_PORT: "0"
      SCRAPEYARD_EGRESS_POLICY_PROBE_LIVENESS_PORT: "0"
      SCRAPEYARD_STORAGE_RETENTION_DAYS: "36500"
      SCRAPEYARD_HISTORY_ADHOC_JOB_RETENTION_DAYS: "36500"
      SCRAPEYARD_HISTORY_SCHEDULED_RUN_RETENTION_DAYS: "36500"
      SCRAPEYARD_HISTORY_ERROR_RETENTION_DAYS: "36500"
      SCRAPEYARD_HISTORY_WEBHOOK_TOMBSTONE_RETENTION_DAYS: "36500"
      SCRAPEYARD_WEBHOOK_DELIVERED_RETENTION_DAYS: "36500"
      SCRAPEYARD_WEBHOOK_FAILED_RETENTION_DAYS: "36500"
    networks:
      backend:
        ipv4_address: $RESTORE_NETWORK_PREFIX.250
  redis:
    restart: "no"
    networks:
      backend:
        ipv4_address: $RESTORE_NETWORK_PREFIX.249
networks:
  backend: !override
    name: ${PROJECT}_backend
    external: true
YAML
    COMPOSE_FILES="$RELEASE/source/docker-compose.yml:$RELEASE/images.json:$STAGE/isolated.yml"
    restore_volumes "$owner"
    compose up -d --no-deps --no-build --pull never --wait redis || fail "the isolated Redis did not start"
    isolated_redis=$(service_container "$PROJECT" redis)
    present=$(redis_keys "$isolated_redis") || \
      fail "the isolated Redis did not say how many keys it holds"
    [[ "$(redis_cli "$isolated_redis" INFO persistence | sed -n 's/^aof_enabled://p')" == 1 ]] || \
      fail "the isolated Redis does not run with its append-only file"
    # Keys go on expiring, so fewer may be left; all of them gone is the
    # empty start a bare dump gives.
    if ! [[ "$present" =~ ^[0-9]+$ ]] || ((present > REDIS_LOADED || (REDIS_LOADED > 0 && present == 0))); then
      fail "the isolated Redis holds $present keys after loading $REDIS_LOADED"
    fi

    require_isolated_network
    compose up -d --no-deps --no-build --pull never scrapeyard || fail "the isolated application could not be started"
    isolated_app=$(service_container "$PROJECT" scrapeyard)
    deadline=$((SECONDS + READY_TIMEOUT))
    until ask_application "$isolated_app" restored "${LATE_JOBS[@]}" > "$STAGE/restored.json" 2> "$STAGE/asked.log"; do
      if [[ -z "$(docker_within 20 ps -q --filter "id=$isolated_app")" ]]; then
        # Kept where only root can read it: a start that fails on a setting
        # can print part of the setting.
        install -m 600 /dev/null "$LOCK_DIR/restore-test-application.log"
        docker_within 60 logs --tail 50 "$isolated_app" 2>&1 | head -c 65536 \
          >> "$LOCK_DIR/restore-test-application.log" || true
        fail "the isolated application stopped; its last output is in $LOCK_DIR/restore-test-application.log"
      fi
      ((SECONDS < deadline)) || \
        fail "the isolated application was not ready within ${READY_TIMEOUT}s: $(tail -n 1 "$STAGE/asked.log")"
      sleep 2
    done
    ready_after=$(elapsed_since "$started_at")

    # What the kernel reports for the container's first process, as the
    # deployment's own wrapper reads it. `docker inspect` repeats what was
    # asked for, and Docker starts a container unconfined where it cannot use
    # AppArmor.
    confinement=$(docker_within 20 exec "$isolated_app" cat /proc/1/attr/current 2>/dev/null) || confinement=""
    [[ "$confinement" == "scrapeyard-chromium (enforce)" ]] || \
      fail "the isolated application runs as '${confinement:-unknown}', not under the scrapeyard-chromium AppArmor profile"
    ask_application "$isolated_app" no-route || fail "the isolated application has a route off the host"
    if ! cmp -s "$STAGE/witness.json" "$STAGE/restored.json"; then
      python3 - "$STAGE/witness.json" "$STAGE/restored.json" >&2 <<'PY'
import json
import sys

before, after = (json.load(open(path, encoding="utf-8")) for path in sys.argv[1:])
jobs_before = {job["job_id"]: job for job in before["jobs"]}
jobs_after = {job["job_id"]: job for job in after["jobs"]}
changed = [key for key in jobs_before.keys() & jobs_after.keys() if jobs_before[key] != jobs_after[key]]
print(
    f"job list: {len(jobs_before)} in the witness, {len(jobs_after)} restored, "
    f"{len(jobs_before.keys() - jobs_after.keys())} missing, "
    f"{len(jobs_after.keys() - jobs_before.keys())} unexpected, {len(changed)} changed"
)
for status in ("complete", "failed"):
    if before["samples"][status] != after["samples"][status]:
        differing = [
            key for key in (before["samples"][status] or {})
            if (after["samples"][status] or {}).get(key) != before["samples"][status][key]
        ]
        print(f"newest {status} job: differs in {', '.join(differing) or 'whether there is one'}")
PY
      fail "the restored application does not read back what the witness recorded"
    fi
    read -r jobs complete failed < <(python3 - "$STAGE/witness.json" <<'PY'
import json
import re
import sys

witness = json.load(open(sys.argv[1], encoding="utf-8"))
names = [(witness["samples"][status] or {}).get("job_id", "-") for status in ("complete", "failed")]
# They are printed, and they come from the archive.
if not all(re.fullmatch(r"[A-Za-z0-9._-]+", name) for name in names):
    sys.exit(1)
print(len(witness["jobs"]), *names)
PY
    ) || true
    [[ -n "${failed:-}" ]] || fail "the witness names a job this script would not print"
    key_id=""
    for job in "$complete" "$failed"; do
      [[ "$job" != - ]] || continue
      key_id=$(ask_application "$isolated_app" envelope "$job") || \
        fail "a restored job's configuration is not stored as an encryption envelope"
    done

    remove_isolated_project
    rm -rf -- "$STAGE"
    STAGE=""
    left=$(isolated_resources) || \
      fail "Docker did not say what is left of $PROJECT; see docker ps -a, docker volume ls and docker network ls"
    [[ -z "$left" ]] || fail "the restore test could not remove all of $PROJECT; see docker ps -a, docker volume ls and docker network ls"
    echo "Isolated application: confined as $confinement by the kernel's label, on ${PROJECT}_backend (internal, no address of the host's on its bridge), no route off the host"
    echo "Ready ${ready_after}s after the start of the test"
    echo "Redis: $REDIS_KEYS keys in the dump, $REDIS_LOADED loaded and $REDIS_EXPIRED expired since the backup; $present present under the append-only file"
    echo "Job list: $jobs jobs, identical to the witness"
    if ((${#LATE_JOBS[@]} > 0)); then
      echo "Accepted after the witness was taken, in the archive and not compared: ${LATE_JOBS[*]}"
    fi
    echo "Newest complete job: $complete; newest failed job: $failed (job, decrypted configuration and result identical where there is one)"
    if [[ -n "$key_id" ]]; then
      echo "Stored configurations are encryption envelopes under key $key_id, and were read back decrypted"
    else
      echo "The witness names no complete or failed job, so no stored configuration was compared"
    fi
    echo "Restore test passed in $(elapsed_since "$started_at")s; the project, its volumes, its network and the plaintext are removed"
    ;;
  *) usage ;;
esac
