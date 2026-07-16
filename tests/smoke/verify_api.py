"""Exercise the real Scrapeyard HTTP API against the controlled fixture site."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PUBLIC_ORIGIN = "http://fixture.public.test:8080"
PRIVATE_ORIGIN = "http://fixture.private.test:8080"
PRIVATE_LITERAL_ORIGIN = "http://10.77.14.2:8080"
TERMINAL_STATUSES = {"complete", "partial", "failed", "cancelled"}


class SmokeFailure(RuntimeError):
    pass


def _request(
    url: str,
    *,
    api_key: str | None = None,
    data: bytes | None = None,
    content_type: str | None = None,
    timeout: float = 10,
) -> tuple[int, Any]:
    headers: dict[str, str] = {}
    if api_key is not None:
        headers["X-API-Key"] = api_key
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return response.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            value = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            value = payload.decode(errors="replace")
        return exc.code, value


def _write_diagnostic(directory: Path, name: str, value: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _execution_block() -> str:
    return """execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
retry:
  max_attempts: 1
  backoff: fixed
  backoff_max: 0
"""


def basic_config(name: str, path: str, *, private_target: bool = False) -> str:
    origin = PRIVATE_LITERAL_ORIGIN if private_target else PUBLIC_ORIGIN
    return f"""project: item14-smoke
name: {name}
{_execution_block()}target:
  url: {origin}{path}
  fetcher: basic
  selectors:
    static: "#static-value"
    redirected: "#redirect-marker"
validation:
  required_fields: [static, redirected]
  min_results: 1
  on_empty: fail
"""


def browser_config(name: str, fetcher: str, *, stealth: bool = False) -> str:
    click_timeout_ms = 30000 if fetcher == "stealthy" else 10000
    return f"""project: item14-smoke
name: {name}
{_execution_block()}target:
  url: {PUBLIC_ORIGIN}/dynamic
  fetcher: {fetcher}
  browser:
    timeout_ms: 45000
    disable_resources: false
    network_idle: false
    stealth: {str(stealth).lower()}
    actions:
      - type: wait_for_selector
        selector: "#js-value"
        timeout_ms: 10000
      - type: click
        selector: "#accept-consent"
        timeout_ms: {click_timeout_ms}
        wait_for_selector: "#consent-value"
      - type: scroll
        times: 2
        pixels: 1200
        wait_ms: 100
      - type: wait_for_selector
        selector: "#scroll-value"
        timeout_ms: 10000
      - type: repeat_click
        selector: "#load-more"
        max_times: 2
        wait_ms: 100
      - type: wait_for_selector
        selector: "#loaded-ready"
        timeout_ms: 10000
  selectors:
    javascript: "#js-value"
    consent: "#consent-value"
    scroll: "#scroll-value"
    load_more: "#loaded-ready"
    loaded_count: "#loaded-count"
validation:
  required_fields: [javascript, consent, scroll, load_more, loaded_count]
  min_results: 1
  on_empty: fail
"""


def unsafe_subrequest_config() -> str:
    return f"""project: item14-smoke
name: unsafe-browser-subrequest
{_execution_block()}target:
  url: {PUBLIC_ORIGIN}/unsafe-subrequest
  fetcher: dynamic
  browser:
    timeout_ms: 30000
    disable_resources: false
    actions:
      - type: click
        selector: "#trigger-unsafe"
        timeout_ms: 10000
      - type: wait_for_selector
        selector: "#subrequest-finished"
        timeout_ms: 10000
  selectors:
    safe: "#safe-value"
    subrequest: "#subrequest-finished"
validation:
  required_fields: [safe, subrequest]
  min_results: 1
  on_empty: fail
"""


def unsafe_browser_redirect_config() -> str:
    return f"""project: item14-smoke
name: unsafe-browser-redirect
{_execution_block()}target:
  url: {PUBLIC_ORIGIN}/redirect-unsafe
  fetcher: dynamic
  browser:
    timeout_ms: 30000
    disable_resources: false
  selectors:
    protected: "#protected-value"
"""


def _submit_and_poll(
    api_url: str,
    api_key: str,
    config: str,
    diagnostics: Path,
    label: str,
    deadline_seconds: float,
) -> dict[str, Any]:
    status, submission = _request(
        f"{api_url}/scrape",
        api_key=api_key,
        data=config.encode(),
        content_type="application/x-yaml",
    )
    _write_diagnostic(diagnostics, f"{label}-submission", submission)
    if status != 202:
        raise SmokeFailure(f"{label}: expected queued HTTP 202, got {status}: {submission}")
    job_id = submission.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise SmokeFailure(f"{label}: submission omitted job_id")

    deadline = time.monotonic() + deadline_seconds
    last_status: int | None = None
    last_payload: Any = None
    while time.monotonic() < deadline:
        last_status, last_payload = _request(
            f"{api_url}/results/{job_id}",
            api_key=api_key,
        )
        if last_status == 200 and isinstance(last_payload, dict):
            result_status = last_payload.get("status")
            if result_status in TERMINAL_STATUSES:
                _write_diagnostic(diagnostics, f"{label}-result", last_payload)
                job_status, job_payload = _request(
                    f"{api_url}/jobs/{job_id}",
                    api_key=api_key,
                )
                _write_diagnostic(diagnostics, f"{label}-job", job_payload)
                if job_status != 200:
                    raise SmokeFailure(
                        f"{label}: failed to retrieve job diagnostic: {job_status}: {job_payload}"
                    )
                return last_payload
        time.sleep(0.5)
    _write_diagnostic(
        diagnostics,
        f"{label}-timeout",
        {"http_status": last_status, "payload": last_payload},
    )
    raise SmokeFailure(f"{label}: timed out waiting for job {job_id}")


def _target(payload: dict[str, Any], label: str) -> dict[str, Any]:
    targets = payload.get("targets")
    if not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict):
        raise SmokeFailure(f"{label}: expected one target diagnostic")
    return targets[0]


def _record(payload: dict[str, Any], label: str) -> dict[str, Any]:
    grouped = payload.get("results")
    if not isinstance(grouped, dict) or len(grouped) != 1:
        raise SmokeFailure(f"{label}: expected one grouped result")
    group = next(iter(grouped.values()))
    data = group.get("data") if isinstance(group, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise SmokeFailure(f"{label}: expected one extracted record")
    return data[0]


def _assert_success(payload: dict[str, Any], label: str) -> None:
    target = _target(payload, label)
    if payload.get("status") != "complete" or target.get("status") != "success":
        raise SmokeFailure(f"{label}: expected complete/success, got {payload.get('status')}/{target}")


def _assert_expected_fields(record: dict[str, Any], expected: dict[str, str], label: str) -> None:
    observed = {field: record.get(field) for field in expected}
    if observed != expected:
        raise SmokeFailure(f"{label}: extraction mismatch: {observed}; full record: {record}")


def verify_successful_modes(
    api_url: str,
    api_key: str,
    diagnostics: Path,
    deadline_seconds: float,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    basic = _submit_and_poll(
        api_url,
        api_key,
        basic_config("basic-safe-redirect", "/redirect-safe"),
        diagnostics,
        "basic",
        deadline_seconds,
    )
    _assert_success(basic, "basic")
    basic_record = _record(basic, "basic")
    expected_basic = {"static": "static-ok", "redirected": "safe-redirect-ok"}
    _assert_expected_fields(basic_record, expected_basic, "basic")
    observations.append({"mode": "basic", "record": expected_basic})

    modes = (
        ("dynamic", "dynamic", False),
        ("dynamic-stealth", "dynamic", True),
        ("stealthy", "stealthy", False),
    )
    expected_browser = {
        "javascript": "javascript-ok",
        "consent": "consent-ok",
        "scroll": "scroll-ok",
        "load_more": "load-more-ok",
        "loaded_count": "2",
    }
    for label, fetcher, stealth in modes:
        payload = _submit_and_poll(
            api_url,
            api_key,
            browser_config(label, fetcher, stealth=stealth),
            diagnostics,
            label,
            deadline_seconds,
        )
        _assert_success(payload, label)
        record = _record(payload, label)
        _assert_expected_fields(record, expected_browser, label)
        debug = _target(payload, label).get("debug")
        if not isinstance(debug, dict) or debug.get("fetcher") != fetcher:
            raise SmokeFailure(f"{label}: fetcher diagnostic mismatch: {debug}")
        if "screenshot_path" not in debug or debug["screenshot_path"] is not None:
            raise SmokeFailure(f"{label}: internal screenshot path was exposed: {debug}")
        settings = debug.get("browser_settings")
        if fetcher == "dynamic" and (
            not isinstance(settings, dict) or settings.get("stealth") is not stealth
        ):
            raise SmokeFailure(f"{label}: stealth diagnostic mismatch: {settings}")
        observations.append(
            {
                "mode": label,
                "record": expected_browser,
                "screenshot_path_redacted": True,
            }
        )
        print(f"verified {label}: {record}", flush=True)
    return observations


def verify_ssrf_defenses(
    api_url: str,
    fixture_url: str,
    api_key: str,
    diagnostics: Path,
    deadline_seconds: float,
) -> dict[str, Any]:
    status, reset = _request(f"{fixture_url}/__reset")
    if status != 200 or reset.get("protected_total") != 0:
        raise SmokeFailure(f"fixture reset failed: {status}: {reset}")

    status, rejected = _request(
        f"{api_url}/scrape",
        api_key=api_key,
        data=basic_config("direct-private-rejected", "/protected", private_target=True).encode(),
        content_type="application/x-yaml",
    )
    _write_diagnostic(diagnostics, "direct-private-rejection", rejected)
    if status != 422 or "non-public" not in str(rejected):
        raise SmokeFailure(f"direct private target was not rejected: {status}: {rejected}")

    subrequest = _submit_and_poll(
        api_url,
        api_key,
        unsafe_subrequest_config(),
        diagnostics,
        "unsafe-subrequest",
        deadline_seconds,
    )
    _assert_success(subrequest, "unsafe-subrequest")
    subrequest_record = _record(subrequest, "unsafe-subrequest")
    _assert_expected_fields(subrequest_record, {
        "safe": "safe-page-ok",
        "subrequest": "blocked",
    }, "unsafe-subrequest")
    debug = _target(subrequest, "unsafe-subrequest").get("debug")
    failures = debug.get("request_failures", []) if isinstance(debug, dict) else []
    if not any(PRIVATE_ORIGIN in str(failure.get("url", "")) for failure in failures if isinstance(failure, dict)):
        raise SmokeFailure(f"unsafe-subrequest: missing classified private request failure: {debug}")

    for label, config in (
        ("unsafe-basic-redirect", basic_config("unsafe-basic-redirect", "/redirect-unsafe")),
        ("unsafe-browser-redirect", unsafe_browser_redirect_config()),
    ):
        payload = _submit_and_poll(
            api_url,
            api_key,
            config,
            diagnostics,
            label,
            deadline_seconds,
        )
        target = _target(payload, label)
        if payload.get("status") != "failed" or target.get("status") != "failed":
            raise SmokeFailure(f"{label}: unsafe redirect unexpectedly succeeded: {target}")
        if label == "unsafe-basic-redirect":
            detail = str(target.get("error_detail") or "")
            if "UnsafeURLError" not in detail or "non-public" not in detail:
                raise SmokeFailure(f"{label}: missing URL-guard diagnostic: {target}")
        else:
            blocked = (target.get("debug") or {}).get("blocked_requests", [])
            route_blocked = any(
                PRIVATE_ORIGIN in str(item.get("url", ""))
                for item in blocked
                if isinstance(item, dict)
            )
            detail = str(target.get("error_detail") or "")
            # Chromium does not re-dispatch Playwright's route callback for
            # every redirect after route.continue_(). The connected-IP egress
            # policy is therefore the authoritative redirect/rebinding guard.
            # Accept either the defense-in-depth route rejection or the
            # firewall's refused connection, then prove below that the private
            # fixture endpoint received no request.
            network_blocked = (
                target.get("error_type") == "browser_error"
                and "ERR_CONNECTION_REFUSED" in detail
            )
            if not route_blocked and not network_blocked:
                raise SmokeFailure(f"{label}: missing browser network rejection: {target}")

    status, stats = _request(f"{fixture_url}/__stats")
    _write_diagnostic(diagnostics, "fixture-protected-stats", stats)
    if (
        status != 200
        or not isinstance(stats, dict)
        or stats.get("protected_cases") != {}
        or stats.get("protected_total") != 0
    ):
        raise SmokeFailure(f"protected fixture endpoint was reached: {status}: {stats}")
    error_status, errors = _request(
        f"{api_url}/errors?project=item14-smoke&limit=100",
        api_key=api_key,
    )
    _write_diagnostic(diagnostics, "ssrf-error-records", errors)
    if error_status != 200 or not isinstance(errors, list) or len(errors) < 2:
        raise SmokeFailure(f"failed to retrieve classified SSRF errors: {error_status}: {errors}")
    print("verified SSRF defenses: protected endpoint count remained zero", flush=True)
    return stats


def run(args: argparse.Namespace) -> dict[str, Any]:
    api_key = Path(args.api_key_file).read_text(encoding="utf-8").strip()
    if not api_key:
        raise SmokeFailure("API key file is empty")
    diagnostics = Path(args.diagnostics_dir)

    health_status, health = _request(f"{args.api_url}/health")
    _write_diagnostic(diagnostics, "health", health)
    if health_status != 200 or health.get("status") != "ok":
        raise SmokeFailure(f"service health failed: {health_status}: {health}")

    unauthenticated_status, unauthenticated = _request(f"{args.api_url}/jobs")
    _write_diagnostic(diagnostics, "unauthenticated-api", unauthenticated)
    if unauthenticated_status != 401:
        raise SmokeFailure(
            f"API authentication is not enforced: {unauthenticated_status}: {unauthenticated}"
        )

    modes = verify_successful_modes(
        args.api_url,
        api_key,
        diagnostics,
        args.job_timeout,
    )
    stats = verify_ssrf_defenses(
        args.api_url,
        args.fixture_url,
        api_key,
        diagnostics,
        args.job_timeout,
    )
    summary = {"modes": modes, "protected_endpoint": stats}
    _write_diagnostic(diagnostics, "smoke-summary", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--fixture-url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--diagnostics-dir", required=True)
    parser.add_argument("--job-timeout", type=float, default=120)
    args = parser.parse_args()
    try:
        summary = run(args)
    except (OSError, urllib.error.URLError, SmokeFailure) as exc:
        print(f"container/browser smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
