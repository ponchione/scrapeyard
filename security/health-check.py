#!/usr/bin/python3 -I
"""Check a Scrapeyard host, and report what it found through a hook (TD-20).

Installed as /usr/local/sbin/scrapeyard-health and run every five minutes by
scrapeyard-health.timer. Each run checks the application's readiness, the
free space and inodes where the host keeps its data, the age of the last
backup, the policy watch's timer and record, the host's failed units, the
restarts of the application's containers and units, the tunnel's
connector, given --edge-url the application's /health asked through
Cloudflare, Access and the tunnel as a consumer would, and given --expires
how long the host's credentials have left. It prints one line for each, writes them to
/var/lib/scrapeyard/health-status.json, and hands the same report to:

- /etc/scrapeyard/health-ping, root's alone (mode 600): one line for each
  service to tell, `healthchecks URL` (every run: the URL when all is well,
  URL/fail when not, the report as the body) or `ntfy URL` (when the state
  changes, and every six hours while it is failing);
- /etc/scrapeyard/health-hook, an executable of root's that no one else can
  change: run every time as `health-hook ok|fail changed|unchanged`, with the
  report on its standard input.

With neither installed it says what it would have sent. Nothing it prints or
sends holds a credential: it reads the settings file for the Compose project
alone, and the application is asked for its readiness from inside its own
container, with the credential the container's health check uses. The report
calls the host by --label, never by its host name.

`--dry-run` runs the checks and prints what each hook would be sent, sending
nothing and leaving the status file as it was.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

SETTINGS_FILE = Path("/etc/scrapeyard/deploy.env")
PING_FILE = Path("/etc/scrapeyard/health-ping")
HOOK_FILE = Path("/etc/scrapeyard/health-hook")
# A Cloudflare Access service token, {"client_id": ..., "client_secret": ...},
# root's alone, for --edge-url.
EDGE_ACCESS_FILE = Path("/etc/scrapeyard/edge-access.json")
STATE_FILE = Path("/var/lib/scrapeyard/health-status.json")
BACKUP_STATUS = Path("/var/lib/scrapeyard/backup-status.json")
POLICY_RECORD = Path("/var/lib/scrapeyard/policy-restored.log")
BACKUP_DIR = Path("/var/backups/scrapeyard")
DISK_PATHS = (Path("/"), Path("/var/lib/docker"), BACKUP_DIR)
TRUSTED_OWNER = 0
TRUSTED_ROOT = Path("/")
PING_SCHEMES = frozenset({"https"})

OWN_UNIT = "scrapeyard-health.service"
APP_UNIT = "scrapeyard.service"
DOCKER_UNIT = "docker.service"
# Docker's states in which asking it starts nothing: its socket starts a
# stopped Docker for the first client, and the boot unit, which Docker wants,
# starts the application with it.
DOCKER_ASKABLE = frozenset({"active", "activating", "reloading"})
PROMOTE_UNIT = "scrapeyard-promote.service"
# Written by a backup just before it stops the application, and removed once
# the application is started again.
BACKUP_STOP_MARKER = Path("/run/scrapeyard/backup-stopped-application")
# What the boot unit is in while its start or stop runs; not between retries.
STARTING_OR_STOPPING = frozenset({
    "start-pre", "start", "start-post", "stop", "stop-sigterm", "stop-sigkill",
    "stop-post", "final-sigterm", "final-sigkill",
})
# A planned stop longer than this is judged all the same, and a failure then
# says the stop is overdue. Measured on the first production host
# (2026-10-14 to 21): a backup's stop 26.5 to 33.4 s, the boot unit's start
# 25 to 53 s, a promotion 32 to 64 s. Until 2026-10-21 this was 600 s, which
# left a 342.8 s outage that day unjudged (docs/MONITORING.md).
PLANNED_WINDOW_SECONDS = 180
WATCH_TIMER = "scrapeyard-policy-watch.timer"
BACKUP_TIMER = "scrapeyard-backup.timer"
TUNNEL_UNIT = "cloudflared-scrapeyard.service"
SERVICES = ("scrapeyard", "redis", "egress-probe")

COMMAND_SECONDS = 30
SEND_SECONDS = 10
REPORT_LIMIT = 10_000
NTFY_REMINDER_SECONDS = 6 * 3600
STATE_FORMAT = "scrapeyard-health-status-v1"
# A first run, or one after the status file was lost, cannot know what of the
# policy record is new: it takes lines written in the last 15 minutes as new.
RECENT_RECORD_SECONDS = 15 * 60
RECORD_READ_LIMIT = 1024 * 1024

# Run inside the application's container with its own Python. It asks the
# application's readiness on loopback with the credential the container's
# health check takes, and prints the verdict and the names of what failed.
READINESS_PROBE = r"""
import json, os, urllib.error, urllib.request
try:
    from scrapeyard.runtime.healthcheck import readiness_key
except ImportError:
    # A release from before 2026-10-13 has no such check: liveness only.
    key = None
else:
    key = readiness_key(os.environ)
path = "/health/ready" if key else "/health/live"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request(
    "http://127.0.0.1:8420" + path, headers={"X-API-Key": key} if key else {}
)
try:
    with opener.open(request, timeout=10) as response:
        code, body = response.status, response.read()
except urllib.error.HTTPError as error:
    # Closed, as from Python 3.14 an unclosed one warns when collected.
    with error:
        code, body = error.code, error.read()
except OSError as error:
    print(json.dumps({"error": type(error).__name__}))
    raise SystemExit(0)
try:
    data = json.loads(body)
except ValueError:
    data = {}
data = data if isinstance(data, dict) else {}
failing = sorted(
    name
    for group in ("dependencies", "background_tasks")
    for name, probe in (data.get(group) or {}).items()
    if isinstance(probe, dict) and not probe.get("ok")
)
workers = data.get("workers") if isinstance(data.get("workers"), dict) else {}
depths = workers.get("queue_depths") if isinstance(workers.get("queue_depths"), dict) else {}
print(json.dumps({
    "code": code,
    "asked": path,
    "status": data.get("status"),
    "failing": failing,
    "active": workers.get("active_tasks"),
    "max": workers.get("max_concurrent"),
    "queued": sum(value for value in depths.values() if isinstance(value, int)),
}))
"""


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    # A warning is said and does not make the host's state failing.
    warning: bool = False


class Unusable(Exception):
    """A hook file that root must not trust; its text says why, with no content."""


def run(command: list[str], *, stdin: str | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=COMMAND_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return -1, type(error).__name__
    return done.returncode, done.stdout


def project_name() -> str:
    # The settings file holds credentials: only the project's line is matched,
    # and nothing of any other line is kept or printed.
    pattern = re.compile(r"COMPOSE_PROJECT_NAME='([a-z0-9][a-z0-9_-]*)'")
    with SETTINGS_FILE.open(encoding="utf-8") as settings:
        for line in settings:
            match = pattern.fullmatch(line.rstrip("\n"))
            if match:
                return match.group(1)
    raise ValueError(f"{SETTINGS_FILE} names no Compose project")


def containers(project: str) -> dict[str, dict[str, Any]]:
    """The deployment's containers by Compose service, as Docker describes them."""

    code, out = run([
        "docker", "ps", "--all", "--quiet",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.oneoff=False",
    ])
    if code != 0:
        raise RuntimeError("Docker did not list the deployment's containers")
    found: dict[str, dict[str, Any]] = {}
    ids = out.split()
    if not ids:
        return found
    code, out = run(["docker", "inspect", *ids])
    if code != 0:
        raise RuntimeError("Docker did not describe the deployment's containers")
    def rank(container: dict[str, Any]) -> tuple[bool, str]:
        state = container.get("State", {})
        return bool(state.get("Running")), str(state.get("StartedAt") or "")

    for container in json.loads(out):
        service = (container.get("Config", {}).get("Labels") or {}).get("com.docker.compose.service")
        # While Compose replaces one, a service briefly has two: the one
        # running, and of those the latest started, is the one to judge.
        if service in SERVICES and (service not in found or rank(container) > rank(found[service])):
            found[service] = container
    return found


def _show(unit: str, prop: str) -> str:
    _code, out = run(["systemctl", "show", f"--property={prop}", "--value", unit])
    return out.strip()


def _seconds_in_state(unit: str) -> float | None:
    try:
        changed = int(_show(unit, "StateChangeTimestampMonotonic")) / 1e6
    except ValueError:
        return None
    return time.monotonic() - changed if changed > 0 else None


def maintenance() -> tuple[str, float] | None:
    """What has the application stopped or starting on purpose, and for how many seconds."""

    try:
        return "a backup has it stopped", time.time() - BACKUP_STOP_MARKER.stat().st_mtime
    except FileNotFoundError:
        pass
    if _show(APP_UNIT, "SubState") in STARTING_OR_STOPPING:
        age = _seconds_in_state(APP_UNIT)
        if age is not None:
            return f"{APP_UNIT} is starting or stopping it", age
    if unit_state(PROMOTE_UNIT) in {"active", "activating"}:
        age = _seconds_in_state(PROMOTE_UNIT)
        if age is not None:
            return "a promotion is under way", age
    return None


def planned_stop() -> str | None:
    """Maintenance still inside its window, which is not judged."""

    found = maintenance()
    if found and found[1] <= PLANNED_WINDOW_SECONDS:
        return found[0]
    return None


def overdue(result: Result) -> Result:
    """A failure during maintenance past its window says so."""

    found = maintenance()
    if result.ok or not found:
        return result
    return Result(
        result.name,
        False,
        f"maintenance overdue: {found[0]} for {found[1]:.0f}s, past the {PLANNED_WINDOW_SECONDS}s allowed; "
        + result.detail,
    )


def check_application(found: dict[str, dict[str, Any]]) -> Result:
    planned = planned_stop()
    if planned:
        return Result("application", True, f"not judged: {planned}")
    container = found.get("scrapeyard")
    if container is None:
        return Result("application", False, "no application container")
    state = container.get("State", {})
    if state.get("Status") != "running":
        return Result("application", False, f"its container is {state.get('Status')}")
    health = (state.get("Health") or {}).get("Status", "none")
    code, out = run(["docker", "exec", "-i", container["Id"], "python", "-"], stdin=READINESS_PROBE)
    try:
        answer = json.loads(out.strip().splitlines()[-1]) if code == 0 and out.strip() else {}
    except ValueError:
        answer = {}
    if "code" not in answer:
        reason = answer.get("error") or "no answer"
        return Result("application", False, f"readiness could not be asked ({reason}); Docker says {health}")
    if answer["code"] != 200:
        failing = ", ".join(answer.get("failing") or []) or "nothing named"
        return Result(
            "application", False,
            f"not ready: HTTP {answer['code']}, failing: {failing}; Docker says {health}",
        )
    busy = f"{answer.get('active')} of {answer.get('max')} workers busy, {answer.get('queued')} queued"
    verdict = (
        answer.get("status")
        if answer.get("asked") == "/health/ready"
        else "live (readiness not asked: no health-detail credential, or an older release)"
    )
    return Result("application", True, f"{verdict}, {busy}")


def check_disk(min_percent: float, min_gib: float) -> Result:
    seen: set[int] = set()
    parts: list[str] = []
    ok = True
    for path in DISK_PATHS:
        try:
            device = path.stat().st_dev
            usage = os.statvfs(path)
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        free = usage.f_bavail * usage.f_frsize
        total = usage.f_blocks * usage.f_frsize or 1
        percent = 100 * free / total
        inodes = 100 * usage.f_favail / usage.f_files if usage.f_files else 100.0
        low = percent < min_percent or free < min_gib * 2**30 or inodes < min_percent
        ok = ok and not low
        parts.append(
            f"{path} {free / 2**30:.1f} GiB free ({percent:.0f}%), inodes {inodes:.0f}% free"
            + (" LOW" if low else "")
        )
    if not parts:
        return Result("disk", False, "no filesystem could be read")
    return Result("disk", ok, "; ".join(parts))


def unit_state(unit: str) -> str:
    _code, out = run(["systemctl", "is-active", unit])
    return out.strip() or "unknown"


def check_backup(
    max_age_hours: float, now: float, state: dict[str, Any] | None = None, new_state: dict[str, Any] | None = None
) -> Result:
    timer = unit_state(BACKUP_TIMER)
    try:
        status = json.loads(BACKUP_STATUS.read_text(encoding="utf-8"))
        completed = datetime.datetime.strptime(status["completed_at"], "%Y-%m-%dT%H:%M:%SZ")
        completed_at = completed.replace(tzinfo=datetime.timezone.utc).timestamp()
    except FileNotFoundError:
        return Result("backup", False, f"no backup has completed; its timer is {timer}")
    except (OSError, ValueError, KeyError, TypeError):
        return Result("backup", False, f"{BACKUP_STATUS} could not be read; its timer is {timer}")
    age = (now - completed_at) / 3600
    if age < -0.25:
        # A run made while the clock was ahead would read as fresh until the
        # clock passed it, however many runs failed in between.
        return Result("backup", False, f"last completed {-age:.1f} h in the future; timer {timer}")
    where = "uploaded" if status.get("uploaded") is True else "kept on this host only"
    detail = f"last completed {age:.1f} h ago, {int(status.get('bytes') or 0) / 1e6:.1f} MB, {where}; timer {timer}"
    ok = age <= max_age_hours and timer == "active"
    # A stop past the window that ended between two runs is never judged by
    # `application`; the backup records how long it was, and that is said once.
    down = status.get("application_down_seconds")
    if new_state is not None:
        new_state["long_stop_seen"] = (state or {}).get("long_stop_seen")
        if isinstance(down, (int, float)) and down > PLANNED_WINDOW_SECONDS and new_state["long_stop_seen"] != status["completed_at"]:
            new_state["long_stop_seen"] = status["completed_at"]
            return Result(
                "backup",
                False,
                f"the backup completed at {status['completed_at']} had the application stopped {down:.0f}s, "
                f"past the {PLANNED_WINDOW_SECONDS}s allowed (once); {detail}",
            )
    return Result("backup", ok, detail)


def check_policy_watch(state: dict[str, Any], new_state: dict[str, Any], now: float) -> Result:
    timer = unit_state(WATCH_TIMER)
    previous = state.get("policy_record")
    try:
        info = POLICY_RECORD.stat()
    except FileNotFoundError:
        # The watch makes the record when it first puts something back, so
        # its absence is remembered: everything in it later is new.
        new_state["policy_record"] = {"absent": True}
        return Result("policy-watch", timer == "active", f"timer {timer}; nothing recorded")
    if isinstance(previous, dict) and previous.get("inode") == info.st_ino and previous.get("size", 0) <= info.st_size:
        start = previous["size"]
    else:
        start = 0
    # The newest lines matter most: of more than the limit, the last is read.
    more = info.st_size - start > RECORD_READ_LIMIT
    with POLICY_RECORD.open("rb") as record:
        record.seek(info.st_size - RECORD_READ_LIMIT if more else start)
        data = record.read(RECORD_READ_LIMIT)
    end = (info.st_size - RECORD_READ_LIMIT if more else start) + len(data)
    new_state["policy_record"] = {"inode": info.st_ino, "size": end}
    added = data.decode("utf-8", "replace").splitlines()
    if more and added:
        added = added[1:]  # the first may be the end of a line
    if not isinstance(previous, dict):
        # No state to compare with: lines that say they are recent are new.
        added = [line for line in added if _recent(line, now)]
    if added or more:
        shown = "; ".join(line[:200] for line in added[-3:])
        extra = " (more than 1 MiB was added; only its last 1 MiB is read)" if more else ""
        return Result("policy-watch", False, f"timer {timer}; {len(added)} new record line(s){extra}: {shown}")
    return Result("policy-watch", timer == "active", f"timer {timer}; no new record line")


def _recent(line: str, now: float) -> bool:
    try:
        written = datetime.datetime.strptime(line[:20], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return now - written.replace(tzinfo=datetime.timezone.utc).timestamp() <= RECENT_RECORD_SECONDS


def check_units() -> Result:
    code, out = run(["systemctl", "list-units", "--failed", "--plain", "--no-legend", "--no-pager"])
    if code != 0:
        return Result("units", False, "systemd did not list failed units")
    failed = [line.split()[0] for line in out.splitlines() if line.split() and line.split()[0] != OWN_UNIT]
    return Result("units", not failed, f"failed: {', '.join(failed)}" if failed else "none failed")


def check_restarts(
    found: dict[str, dict[str, Any]], state: dict[str, Any], new_state: dict[str, Any]
) -> Result:
    previous = state.get("restarts") or {}
    current: dict[str, Any] = {}
    changes: list[str] = []
    for service, container in sorted(found.items()):
        entry = {"id": container.get("Id"), "count": int(container.get("RestartCount") or 0)}
        current[service] = entry
        before = previous.get(service)
        if before and before.get("id") == entry["id"] and entry["count"] > before.get("count", 0):
            changes.append(f"{service} restarted {entry['count'] - before['count']} time(s)")
    for unit in (APP_UNIT, TUNNEL_UNIT):
        _code, out = run(["systemctl", "show", "--property=NRestarts", "--value", unit])
        count = int(out.strip()) if out.strip().isdigit() else 0
        current[unit] = {"count": count}
        before = previous.get(unit)
        if before and count > before.get("count", 0):
            changes.append(f"{unit} restarted {count - before['count']} time(s)")
    new_state["restarts"] = current
    if not previous:
        return Result("restarts", True, "first run; counts recorded")
    return Result("restarts", not changes, "; ".join(changes) if changes else "none since the last check")


def check_tunnel(ready_url: str | None) -> Result:
    _code, out = run(["systemctl", "show", "--property=LoadState", "--value", TUNNEL_UNIT])
    if out.strip() != "loaded":
        return Result("tunnel", True, f"no {TUNNEL_UNIT} on this host")
    active = unit_state(TUNNEL_UNIT)
    if active != "active":
        return Result("tunnel", False, f"connector {active}")
    if not ready_url:
        return Result("tunnel", True, "connector active; its own readiness is not read (no --tunnel-ready-url)")
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(ready_url, timeout=5) as response:
            code = response.status
    except urllib.error.HTTPError as error:
        with error:
            code = error.code
    except OSError as error:
        return Result("tunnel", False, f"connector active; readiness not answered ({type(error).__name__})")
    return Result("tunnel", code == 200, f"connector active; readiness HTTP {code}")


def check_edge(url: str) -> Result:
    """Ask the application's liveness the way a consumer does: through Cloudflare, Access and the tunnel."""

    planned = planned_stop()
    if planned:
        return Result("edge", True, f"not judged: {planned}")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in PING_SCHEMES or not parts.hostname or parts.username is not None:
        return Result("edge", False, "--edge-url is not one https URL with no login")
    try:
        trusted(EDGE_ACCESS_FILE, private=True)
        token = json.loads(EDGE_ACCESS_FILE.read_text(encoding="utf-8"))
        headers = {
            "CF-Access-Client-Id": str(token["client_id"]),
            "CF-Access-Client-Secret": str(token["client_secret"]),
        }
    except Unusable as error:
        return Result("edge", False, str(error))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return Result("edge", False, f"{EDGE_ACCESS_FILE} is not usable ({type(error).__name__})")
    # The zone's Browser Integrity Check refuses urllib's own User-Agent.
    request = urllib.request.Request(url, headers={"User-Agent": "scrapeyard-health", **headers})
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
        with opener.open(request, timeout=SEND_SECONDS) as response:
            code, body = response.status, response.read(4096)
    except urllib.error.HTTPError as error:
        with error:
            code, body = error.code, b""
    except Exception as error:  # noqa: BLE001 - its text can hold the URL
        return Result("edge", False, f"the public name did not answer ({type(error).__name__})")
    try:
        status = json.loads(body).get("status") if code == 200 else None
    except (ValueError, AttributeError):
        status = None
    detail = f"the public name answered HTTP {code} through Access and the tunnel"
    return Result("edge", status == "ok", detail if code != 200 else f"{detail}, status {status}")


def check_expiries(expiries: list[str], warn_days: float, now: float) -> Result:
    """Fail from warn_days before a credential the host relies on expires."""

    soonest: list[tuple[int, str]] = []
    for entry in expiries:
        label, _sep, date = entry.partition("=")
        try:
            if not label or not re.fullmatch(r"[A-Za-z0-9._-]+", label):
                raise ValueError
            ends = datetime.date.fromisoformat(date) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else None
            if ends is None:
                raise ValueError
        except ValueError:
            return Result("expiry", False, f"--expires {entry!r} is not LABEL=YYYY-MM-DD")
        today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
        soonest.append(((ends - today).days, f"{label} {date}"))
    soonest.sort()
    due = [f"{what} ({'expired' if days <= 0 else f'in {days} days'})" for days, what in soonest if days <= warn_days]
    if due:
        return Result("expiry", False, "renew: " + "; ".join(due))
    days, what = soonest[0]
    return Result("expiry", True, f"{len(soonest)} recorded; the first, {what}, in {days} days")


def check_all(args: argparse.Namespace, state: dict[str, Any], new_state: dict[str, Any]) -> list[Result]:
    now = time.time()
    results: list[Result] = []
    try:
        docker = unit_state(DOCKER_UNIT)
        if docker not in DOCKER_ASKABLE:
            raise RuntimeError(f"{DOCKER_UNIT} is {docker}, so Docker was not asked")
        found = containers(project_name())
    except (OSError, ValueError, RuntimeError) as error:
        found = {}
        results.append(Result("deployment", False, f"its containers could not be found: {error}"))
    checks: list[tuple[str, Callable[[], Result]]] = [
        ("application", lambda: overdue(check_application(found))),
        ("disk", lambda: check_disk(args.disk_min_free_percent, args.disk_min_free_gib)),
        ("backup", lambda: check_backup(args.backup_max_age_hours, now, state, new_state)),
        ("policy-watch", lambda: check_policy_watch(state, new_state, now)),
        ("units", check_units),
        ("restarts", lambda: check_restarts(found, state, new_state)),
        ("tunnel", lambda: check_tunnel(args.tunnel_ready_url)),
    ]
    if args.edge_url:
        checks.append(("edge", lambda: overdue(check_edge(args.edge_url))))
    if args.expires:
        checks.append(("expiry", lambda: check_expiries(args.expires, args.expiry_warn_days, now)))
    for name, check in checks:
        try:
            results.append(check())
        except Exception as error:  # noqa: BLE001 - one broken check must not hide the others
            results.append(Result(name, False, f"the check itself failed ({type(error).__name__})"))
    return results


def report(results: list[Result], label: str) -> str:
    failing = [result for result in results if not result.ok and not result.warning]
    head = f"{label}: " + (
        f"FAILING ({len(failing)} of {len(results)} checks)" if failing else f"OK ({len(results)} checks)"
    )
    def mark(result: Result) -> str:
        return "ok  " if result.ok else "WARN" if result.warning else "FAIL"

    lines = [head] + [f"{mark(result)} {result.name}: {result.detail}" for result in results]
    return "\n".join(lines)[:REPORT_LIMIT]


def trusted(path: Path, *, private: bool) -> None:
    """Refuse a file that anyone but root could have written, or could read when private."""

    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise Unusable(f"{path} is not a regular file")
    if info.st_uid != TRUSTED_OWNER or info.st_mode & (0o077 if private else 0o022):
        raise Unusable(
            f"{path} must be root's alone"
            + (" (mode 600)" if private else ", writable by root only")
        )
    # Whoever can write to a directory above it can replace it. Both the
    # path as written and the one it resolves to are walked.
    for directory in {path.parent, path.parent.resolve()}:
        while True:
            dinfo = directory.stat()
            if dinfo.st_uid != TRUSTED_OWNER or dinfo.st_mode & 0o022:
                raise Unusable(f"{directory} is not root's alone, so {path.name} in it cannot be trusted")
            if directory == TRUSTED_ROOT or directory == directory.parent:
                break
            directory = directory.parent


def ping_targets() -> list[tuple[str, str]]:
    trusted(PING_FILE, private=True)
    targets: list[tuple[str, str]] = []
    for number, line in enumerate(PING_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind, _space, url = line.partition(" ")
        url = url.strip()
        try:
            parts = urllib.parse.urlsplit(url)
            usable = (
                kind in {"healthchecks", "ntfy"}
                and parts.scheme in PING_SCHEMES
                and bool(parts.hostname)
                and parts.username is None
                and (parts.port is None or parts.port > 0)
            )
        except ValueError:
            usable = False
        if not usable or any(character.isspace() or ord(character) < 32 for character in url):
            # The line may hold a token: only its number is said.
            raise Unusable(
                f"{PING_FILE} line {number} is not `healthchecks https://...` or `ntfy https://...`"
                " (one URL with no login, spaces or comment)"
            )
        targets.append((kind, url))
    return targets


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer to report, never a place to send the report."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def send(url: str, body: str, headers: dict[str, str]) -> str:
    try:
        request = urllib.request.Request(
            url, data=body.encode(), headers={"User-Agent": "scrapeyard-health", **headers}, method="POST"
        )
        opener = urllib.request.build_opener(_NoRedirects)
        with opener.open(request, timeout=SEND_SECONDS) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        with error:
            return f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001 - its text can hold the URL, which is a credential
        return type(error).__name__


@dataclass
class Hooks:
    result: Result
    targets: list[tuple[str, str]]
    command: bool


def installed_hooks() -> Hooks:
    """What the report can be sent through, judged before anything is sent."""

    targets: list[tuple[str, str]] = []
    command = False
    problems: list[str] = []
    names: list[str] = []
    if PING_FILE.exists() or PING_FILE.is_symlink():
        try:
            targets = ping_targets()
            names += sorted({kind for kind, _url in targets})
        except (Unusable, OSError, UnicodeDecodeError) as error:
            problems.append(f"{error}; nothing is sent through it")
        except ValueError:
            problems.append(f"{PING_FILE} could not be read; nothing is sent through it")
    if HOOK_FILE.exists() or HOOK_FILE.is_symlink():
        try:
            trusted(HOOK_FILE, private=False)
            if not os.access(HOOK_FILE, os.X_OK):
                raise Unusable(f"{HOOK_FILE} is not executable")
            command = True
            names.append("command")
        except (Unusable, OSError) as error:
            problems.append(f"{error}; it is not run")
    if problems:
        return Hooks(Result("hooks", False, "; ".join(problems)), targets, command)
    detail = ", ".join(names) if names else "none installed; nothing is sent"
    return Hooks(Result("hooks", True, detail), targets, command)


def deliver(
    hooks: Hooks,
    status: str,
    changed: bool,
    text: str,
    state: dict[str, Any],
    new_state: dict[str, Any],
    dry_run: bool,
) -> list[str]:
    """Hand the report to what is installed; return lines saying what was done."""

    said: list[str] = []
    now = time.time()
    # Each topic keeps its own state, by its place in the file: the URL is a
    # credential, and the status file is readable by anyone on the host.
    sent = state.get("ntfy") if isinstance(state.get("ntfy"), dict) else {}
    ntfy: dict[str, Any] = {}
    failed: list[str] = []
    for place, (kind, url) in enumerate(hooks.targets, 1):
        # The URL is a credential of its own: only its host is said.
        host = urllib.parse.urlsplit(url).hostname
        if kind == "healthchecks":
            target = url if status == "ok" else url.rstrip("/") + "/fail"
            outcome = "not sent (dry run)" if dry_run else send(target, text, {"Content-Type": "text/plain"})
            said.append(f"healthchecks ping ({status}) to {host}: {outcome}")
            if not dry_run and not outcome.startswith("HTTP 2"):
                failed.append(f"healthchecks ping to {host}: {outcome}")
            continue
        key = f"line-{place}"
        last = sent.get(key) if isinstance(sent.get(key), dict) else {}
        ntfy[key] = last
        due = last.get("status") != status or (
            status == "fail" and now - float(last.get("sent_at") or 0) >= NTFY_REMINDER_SECONDS
        )
        if not due:
            continue
        headers = {
            "Title": "Scrapeyard host: " + ("failing" if status == "fail" else "all checks pass"),
            "Priority": "high" if status == "fail" else "default",
            "Tags": "warning" if status == "fail" else "white_check_mark",
        }
        outcome = "not sent (dry run)" if dry_run else send(url, text, headers)
        said.append(f"ntfy message ({status}) to {host}: {outcome}")
        if not dry_run and outcome.startswith("HTTP 2"):
            ntfy[key] = {"status": status, "sent_at": now}
        elif not dry_run:
            failed.append(f"ntfy message to {host}: {outcome}")
    new_state["ntfy"] = ntfy
    if hooks.command:
        mode = "changed" if changed else "unchanged"
        if dry_run:
            said.append(f"{HOOK_FILE} {status} {mode}: not run (dry run)")
        else:
            try:
                done = subprocess.run(
                    [str(HOOK_FILE), status, mode],
                    input=text + "\n",
                    capture_output=True,
                    text=True,
                    timeout=COMMAND_SECONDS,
                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
                    check=False,
                )
                # What it printed may hold its own secrets: only its status is said.
                said.append(f"{HOOK_FILE} {status} {mode}: exit status {done.returncode}")
                if done.returncode != 0:
                    failed.append(f"{HOOK_FILE.name}: exit status {done.returncode}")
            except (OSError, subprocess.TimeoutExpired) as error:
                said.append(f"{HOOK_FILE} {status} {mode}: {type(error).__name__}")
                failed.append(f"{HOOK_FILE.name}: {type(error).__name__}")
    # Said by the next run, through whatever does work.
    new_state["delivery_failed"] = failed
    if not hooks.targets and not hooks.command:
        said.append(f"Nothing sent; with a hook installed, this would have been: {text.splitlines()[0]}")
    return said


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) and state.get("format") == STATE_FORMAT else {}


def save_state(new_state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    partial = STATE_FILE.with_name(STATE_FILE.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(new_state, out, indent=2, sort_keys=True)
        out.write("\n")
    os.replace(partial, STATE_FILE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="send nothing and keep the status file")
    parser.add_argument("--disk-min-free-percent", type=float, default=10.0)
    parser.add_argument("--disk-min-free-gib", type=float, default=2.0)
    parser.add_argument("--backup-max-age-hours", type=float, default=26.0)
    parser.add_argument("--tunnel-ready-url", help="the connector's own readiness, such as http://127.0.0.1:20241/ready")
    parser.add_argument(
        "--edge-url",
        help=f"the public /health, such as https://scrapeyard.example.net/health, asked with the Access token in {EDGE_ACCESS_FILE}",
    )
    parser.add_argument(
        "--expires",
        action="append",
        metavar="LABEL=YYYY-MM-DD",
        help="a credential the host relies on and the UTC day it expires; repeatable",
    )
    parser.add_argument("--expiry-warn-days", type=float, default=30.0)
    parser.add_argument(
        "--label",
        default="Scrapeyard host",
        help="what the report calls this host; the host name is not sent, since it can name the address",
    )
    args = parser.parse_args(argv)
    state = load_state()
    try:
        return run_once(args, state)
    except Exception as error:  # noqa: BLE001 - its text could hold a URL or a setting
        print(f"The check itself failed ({type(error).__name__}); the next run reports it", flush=True)
        if not args.dry_run:
            try:
                save_state({**state, "format": STATE_FORMAT, "crashed": type(error).__name__})
            except OSError:
                pass
        return 1


def run_once(args: argparse.Namespace, state: dict[str, Any]) -> int:
    new_state: dict[str, Any] = {"format": STATE_FORMAT}
    hooks = installed_hooks()
    results = [*check_all(args, state, new_state), hooks.result]
    if state.get("crashed"):
        # A run that ended before it could report: said once, by this one.
        results.append(Result("previous-run", False, f"ended with {state['crashed']} before it reported"))
    if state.get("delivery_failed"):
        # Said through whatever works, and not a failure of the host: one
        # missed ping would otherwise alert, and recover, on every channel.
        results.append(
            Result("delivery", False, "last run: " + "; ".join(state["delivery_failed"]), warning=True)
        )
    status = "ok" if all(result.ok or result.warning for result in results) else "fail"
    changed = state.get("status") != status
    text = report(results, args.label)
    print(text, flush=True)
    for line in deliver(hooks, status, changed, text, state, new_state, args.dry_run):
        print(line, flush=True)
    if not args.dry_run:
        new_state.update({
            "checked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": status,
            "results": [asdict(result) for result in results],
        })
        save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
