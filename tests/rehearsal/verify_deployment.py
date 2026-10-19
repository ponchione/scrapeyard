"""Check a deployed release through its API, and that a known run outlives a redeploy."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, cast

PROJECT = "release-rehearsal"
TERMINAL_STATUSES = {"complete", "partial", "failed", "cancelled"}
IDEMPOTENCY_KEY = "release-rehearsal-known-run"
# The fields of a result that Eyebox depends on. A rollback runs an older
# binary over the newer one's data, so only these are compared between releases.
KNOWN_FIELDS = ("job_id", "run_id", "status", "project", "name", "config_hash", "completed_at")

# The rehearsal has no operator proxy to fetch through. A `.invalid` name never
# resolves (RFC 6761), so the target is refused before any connection is made
# and the run ends `failed`. That is still a stored run and result to read back.
KNOWN_CONFIG = f"""project: {PROJECT}
name: known-run
execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
retry:
  max_attempts: 1
  backoff: fixed
  backoff_max: 0
target:
  url: http://scrapeyard-rehearsal.invalid/
  fetcher: basic
  selectors:
    title: "h1"
"""

# Deployment secrets the rehearsal's settings define and allow for its project:
# a job may name the first, and the second is allowed but never defined.
SECRET_NAME = "SCRAPEYARD_SECRET_REHEARSAL_PROXY"
MISSING_SECRET_NAME = "SCRAPEYARD_SECRET_REHEARSAL_MISSING"


def secret_reference_config(name: str) -> str:
    """The known job, sent through a proxy named by a deployment secret."""

    return KNOWN_CONFIG.replace(
        "name: known-run\n", f"name: secret-reference\nproxy:\n  url: ${{{name}}}\n"
    )


class RehearsalFailure(RuntimeError):
    pass


def request(
    url: str,
    *,
    api_key: str | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10,
) -> tuple[int, Any, dict[str, str]]:
    sent = dict(headers or {})
    if api_key is not None:
        sent["X-API-Key"] = api_key
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=sent), timeout=timeout
        ) as response:
            status, payload, received = response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        # Closed, as from Python 3.14 an unclosed one warns when collected.
        with exc:
            status, payload, received = exc.code, exc.read(), exc.headers
    try:
        value = json.loads(payload) if payload else None
    except json.JSONDecodeError:
        value = payload.decode(errors="replace")
    return status, value, {name.lower(): text for name, text in received.items()}


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RehearsalFailure(message)


def submit(
    api_url: str,
    client_key: str,
    idempotency_key: str = IDEMPOTENCY_KEY,
    config: str = KNOWN_CONFIG,
) -> tuple[dict[str, Any], dict[str, str]]:
    status, submission, headers = request(
        f"{api_url}/scrape",
        api_key=client_key,
        data=config.encode(),
        headers={"Content-Type": "application/x-yaml", "Idempotency-Key": idempotency_key},
    )
    expect(status == 202, f"submission: expected HTTP 202, got {status}: {submission}")
    expect(
        isinstance(submission, dict) and bool(submission.get("job_id") and submission.get("run_id")),
        f"submission omitted job_id or run_id: {submission}",
    )
    return submission, headers


def known_result(api_url: str, client_key: str, job_id: str, run_id: str) -> dict[str, Any]:
    status, result, _headers = request(
        f"{api_url}/results/{job_id}?run_id={run_id}&latest=false", api_key=client_key
    )
    expect(status == 200 and isinstance(result, dict), f"result read: HTTP {status}: {result}")
    return {field: result.get(field) for field in KNOWN_FIELDS}


def await_run(
    api_url: str, client_key: str, job_id: str, run_id: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    run: Any = None
    while time.monotonic() < deadline:
        status, run, _headers = request(
            f"{api_url}/jobs/{job_id}/runs/{run_id}", api_key=client_key
        )
        expect(status == 200 and isinstance(run, dict), f"run read: HTTP {status}: {run}")
        if run.get("status") in TERMINAL_STATUSES:
            return cast(dict[str, Any], run)
        time.sleep(0.5)
    raise RehearsalFailure(f"run {run_id} did not finish within {timeout:.0f}s: {run}")


def record(api_url: str, client_key: str, known_path: Path, timeout: float) -> dict[str, Any]:
    submission, _headers = submit(api_url, client_key)
    job_id, run_id = submission["job_id"], submission["run_id"]
    await_run(api_url, client_key, job_id, run_id, timeout)
    known = known_result(api_url, client_key, job_id, run_id)
    expect(known["status"] == "failed", f"known run should fail before any fetch: {known}")
    expect(known["project"] == PROJECT, f"known run has the wrong project: {known}")
    known_path.write_text(json.dumps(known, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return known


def run_new(api_url: str, client_key: str, timeout: float) -> dict[str, Any]:
    """Submit under a key never used before, and wait for the worker to finish it."""
    submission, headers = submit(
        api_url, client_key, f"release-rehearsal-{uuid.uuid4().hex}"
    )
    expect(
        headers.get("idempotency-replayed") != "true",
        f"a new submission was answered as a replay: {submission}",
    )
    run = await_run(api_url, client_key, submission["job_id"], submission["run_id"], timeout)
    return {"job_id": run.get("job_id"), "run_id": run.get("run_id"), "status": run.get("status")}


def secret_reference(
    api_url: str, client_key: str, secret_value: str, timeout: float
) -> dict[str, Any]:
    """A job that names a deployment secret runs with its value, and never shows it.

    The value is resolved from the application's environment when the job is
    validated, so a reference the container cannot resolve is refused at once.
    The control names a secret that is allowed and was never defined.
    """
    refused_status, refused, _headers = request(
        f"{api_url}/scrape",
        api_key=client_key,
        data=secret_reference_config(MISSING_SECRET_NAME).encode(),
        headers={
            "Content-Type": "application/x-yaml",
            "Idempotency-Key": f"release-rehearsal-{uuid.uuid4().hex}",
        },
    )
    expect(
        refused_status == 422
        and f"Missing deployment secret reference {MISSING_SECRET_NAME}" in str(refused),
        f"a job naming an undefined secret: expected HTTP 422 naming it, got {refused_status}: {refused}",
    )
    submission, _headers = submit(
        api_url,
        client_key,
        f"release-rehearsal-{uuid.uuid4().hex}",
        secret_reference_config(SECRET_NAME),
    )
    job_id, run_id = submission["job_id"], submission["run_id"]
    run = await_run(api_url, client_key, job_id, run_id, timeout)
    reads = {"run": run}
    for name, path in (
        ("result", f"/results/{job_id}?run_id={run_id}&latest=false"),
        ("job", f"/jobs/{job_id}"),
    ):
        status, body, _headers = request(f"{api_url}{path}", api_key=client_key)
        expect(status == 200, f"{name} read: HTTP {status}: {body}")
        reads[name] = body
    # The whole value, and its password alone, which a proxy login can leak
    # by itself.
    shown = [secret_value, *filter(None, [urllib.parse.urlsplit(secret_value).password])]
    for name, body in reads.items():
        text = json.dumps(body)
        expect(
            not any(part in text for part in shown),
            f"the deployment secret's value appeared in the {name}",
        )
    return {
        "undefined_secret_refused": refused_status,
        "run": {"run_id": run_id, "status": run.get("status")},
    }


def check(
    api_url: str,
    client_key: str,
    monitor_key: str,
    known_path: Path | None,
    ready_timeout: float = 30,
) -> dict[str, Any]:
    status, live, _headers = request(f"{api_url}/health")
    expect(status == 200 and live == {"status": "ok"}, f"liveness: HTTP {status}: {live}")
    for path in ("/health/ready", "/jobs"):
        status, body, _headers = request(f"{api_url}{path}")
        expect(status == 401, f"{path} without a credential: expected 401, got {status}: {body}")
    # The client credential mirrors Eyebox's: submit and read in one project.
    status, body, _headers = request(f"{api_url}/health/ready", api_key=client_key)
    expect(status == 403, f"readiness with the client credential: expected 403, got {status}")
    status, body, _headers = request(
        f"{api_url}/scrape",
        api_key=monitor_key,
        data=KNOWN_CONFIG.encode(),
        headers={"Content-Type": "application/x-yaml", "Idempotency-Key": "release-rehearsal-monitor"},
    )
    expect(status == 403, f"submission with the monitoring credential: expected 403, got {status}")
    # Background work reports ready a moment after the process starts serving.
    deadline = time.monotonic() + ready_timeout
    while True:
        status, ready, _headers = request(f"{api_url}/health/ready", api_key=monitor_key)
        if status == 200 and isinstance(ready, dict) and ready.get("status") == "ok":
            break
        expect(time.monotonic() < deadline, f"readiness: HTTP {status}: {ready}")
        time.sleep(1)
    report: dict[str, Any] = {"readiness": ready["status"], "workers": ready.get("workers")}
    if known_path is not None:
        known = json.loads(known_path.read_text(encoding="utf-8"))
        observed = known_result(api_url, client_key, known["job_id"], known["run_id"])
        expect(observed == known, f"known run changed: recorded {known}, read {observed}")
        # The same key and bytes must replay the stored submission, not start a
        # second run, for as long as the idempotency record is retained.
        submission, headers = submit(api_url, client_key)
        expect(
            (submission["job_id"], submission["run_id"]) == (known["job_id"], known["run_id"])
            and headers.get("idempotency-replayed") == "true",
            f"submission was not replayed: {submission}",
        )
        report["known_run"] = observed
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "check", "run", "secret-reference"))
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--client-key-file", type=Path, required=True)
    parser.add_argument("--monitor-key-file", type=Path, required=True)
    parser.add_argument("--known", type=Path, help="recorded known run (written by record)")
    parser.add_argument("--job-timeout", type=float, default=120)
    parser.add_argument(
        "--secret-file", type=Path, help=f"the value of {SECRET_NAME} (secret-reference)"
    )
    args = parser.parse_args()
    client_key = args.client_key_file.read_text(encoding="utf-8").strip()
    monitor_key = args.monitor_key_file.read_text(encoding="utf-8").strip()
    try:
        if args.action == "record":
            if args.known is None:
                parser.error("record requires --known")
            report = record(args.api_url, client_key, args.known, args.job_timeout)
        elif args.action == "run":
            report = run_new(args.api_url, client_key, args.job_timeout)
        elif args.action == "secret-reference":
            if args.secret_file is None:
                parser.error("secret-reference requires --secret-file")
            secret_value = args.secret_file.read_text(encoding="utf-8").strip()
            report = secret_reference(args.api_url, client_key, secret_value, args.job_timeout)
        else:
            report = check(args.api_url, client_key, monitor_key, args.known)
    except (RehearsalFailure, OSError) as exc:
        print(f"deployment rehearsal: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
