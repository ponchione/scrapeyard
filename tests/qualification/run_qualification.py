#!/usr/bin/env python3
"""Drive Item 15 recovery, Redis, restore, load, and soak qualification phases."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

PUBLIC_ORIGIN = "http://fixture.public.test:8080"
TERMINAL = {"complete", "partial", "failed", "cancelled"}
CRASH_EXPECTATIONS = {
    "after_enqueue_before_claim": "complete_after_redis_redelivery",
    "after_claim_run_creation": "failed_stale_run",
    "during_target_execution": "failed_stale_run",
    "after_result_artifact_write": "failed_with_orphan_removed",
    "during_run_finalization": "failed_with_recoverable_result",
    "during_webhook_intent_transaction": "failed_with_reconciled_webhook_intent",
    "after_terminal_state_before_delivery_ack": "complete_with_single_delivery",
}


class QualificationFailure(RuntimeError):
    pass


@dataclass
class Thresholds:
    read_p95_ms: float = 750.0
    recovery_seconds: float = 30.0
    drain_seconds: float = 180.0
    memory_peak_mib: float = 6144.0
    memory_growth_mib: float = 512.0
    disk_growth_mib: float = 1024.0
    db_growth_mib: float = 64.0
    task_growth: int = 4
    cpu_percent: float = 400.0


@dataclass
class Report:
    profile: str
    intended_host: str = "4 CPU / 8 GiB RAM / 15 GiB free disk"
    thresholds: dict[str, Any] = field(default_factory=dict)
    phases: dict[str, Any] = field(default_factory=dict)
    latency_ms: list[float] = field(default_factory=list)
    resource_samples: list[dict[str, Any]] = field(default_factory=list)
    started_monotonic: float = field(default_factory=time.monotonic)


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.api = args.api_url.rstrip("/")
        self.fixture = args.fixture_url.rstrip("/")
        self.api_key = args.api_key_file.read_text(encoding="utf-8").strip()
        self.diagnostics = args.diagnostics_dir
        self.diagnostics.mkdir(parents=True, exist_ok=True)
        self.profile = args.profile
        self.soak_seconds = args.soak_seconds
        self.phase_timeout = args.phase_timeout
        self.backup_dir = args.backup_dir
        self.repo_root = args.repo_root
        self.compose_args = args.compose_arg
        self.thresholds = Thresholds(**json.loads(args.thresholds_json))
        self.report = Report(
            profile=self.profile,
            thresholds=asdict(self.thresholds),
        )

    def compose(self, *args: str, timeout: float | None = None, check: bool = True) -> str:
        command = ["docker", "compose", *self.compose_args, *args]
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            text=True,
            capture_output=True,
            timeout=timeout or self.phase_timeout,
        )
        if check and result.returncode != 0:
            raise QualificationFailure(
                f"compose_command_failure command={args!r} exit={result.returncode} "
                f"stderr={result.stderr[-2000:]}"
            )
        return result.stdout

    def request(
        self,
        path_or_url: str,
        *,
        data: bytes | None = None,
        authenticated: bool = True,
        content_type: str | None = None,
        timeout: float = 10,
        record_latency: bool = False,
    ) -> tuple[int, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.api}{path_or_url}"
        headers: dict[str, str] = {}
        if authenticated:
            headers["X-API-Key"] = self.api_key
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=data, headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
                value = json.loads(body) if body else None
                return response.status, value
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                value = json.loads(body) if body else None
            except json.JSONDecodeError:
                value = body.decode(errors="replace")
            return exc.code, value
        finally:
            if record_latency:
                self.report.latency_ms.append((time.monotonic() - started) * 1000)

    def wait_health(self, *, healthy: bool = True, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or self.thresholds.recovery_seconds)
        last: tuple[int, Any] | None = None
        while time.monotonic() < deadline:
            try:
                # The public /health endpoint is intentionally minimal. Release
                # qualification needs the protected dependency/capacity shape.
                last = self.request("/health/ready", timeout=3)
            except OSError:
                time.sleep(0.25)
                continue
            if healthy and last[0] == 200:
                return last[1]
            if not healthy and last[0] == 503:
                return last[1]
            time.sleep(0.25)
        raise QualificationFailure(
            f"health_convergence_failure expected_healthy={healthy} last={last}"
        )

    def submit(self, config: str, *, timeout: float = 15) -> str:
        status, payload = self.request(
            "/scrape",
            data=config.encode(),
            content_type="application/x-yaml",
            timeout=timeout,
            record_latency=True,
        )
        if status not in {200, 202} or not isinstance(payload, dict) or not payload.get("job_id"):
            raise QualificationFailure(f"submission_failure status={status} payload={payload}")
        return str(payload["job_id"])

    def wait_job(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (timeout or self.thresholds.drain_seconds)
        last: Any = None
        while time.monotonic() < deadline:
            status, last = self.request(f"/jobs/{job_id}", record_latency=True)
            if status == 200 and last.get("status") in TERMINAL:
                return last
            self.sample_resources()
            time.sleep(0.35)
        raise QualificationFailure(f"job_convergence_failure job_id={job_id} last={last}")

    def find_job(self, project: str) -> dict[str, Any]:
        status, jobs = self.request(
            f"/jobs?project={urllib.parse.quote(project)}&limit=100",
            record_latency=True,
        )
        if status != 200 or not isinstance(jobs, list) or len(jobs) != 1:
            raise QualificationFailure(f"job_lookup_failure project={project} payload={jobs}")
        return jobs[0]

    def fixture_stats(self) -> dict[str, Any]:
        status, payload = self.request(f"{self.fixture}/__stats", authenticated=False)
        if status != 200:
            raise QualificationFailure(f"fixture_stats_failure status={status}")
        return payload

    def sample_resources(self) -> dict[str, Any]:
        try:
            health_status, health = self.request("/health/ready", timeout=2)
        except OSError:
            health_status, health = 0, {}
        container = self.compose("ps", "-q", "scrapeyard", timeout=10).strip()
        sample: dict[str, Any] = {
            "at_seconds": round(time.monotonic() - self.report.started_monotonic, 3),
            "health_status": health_status,
            "workers": health.get("workers", {}) if isinstance(health, dict) else {},
        }
        if container:
            raw = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}", container],
                text=True,
                capture_output=True,
                timeout=10,
            ).stdout.strip()
            if raw:
                stats = json.loads(raw)
                sample["cpu_percent"] = _percent(stats.get("CPUPerc", "0"))
                sample["memory_mib"] = _memory_mib(stats.get("MemUsage", "0B / 0B"))
        self.report.resource_samples.append(sample)
        return sample

    def runtime_growth(self) -> dict[str, int]:
        script = (
            "import glob,json,os,pathlib; "
            "db=sum(p.stat().st_size for p in pathlib.Path('/data/db').glob('*') if p.is_file()); "
            "res=sum(p.stat().st_size for p in pathlib.Path('/data/results').rglob('*') if p.is_file()); "
            "ad=sum(p.stat().st_size for p in pathlib.Path('/data/adaptive').rglob('*') if p.is_file()); "
            "print(json.dumps({'db_bytes':db,'result_bytes':res,'adaptive_bytes':ad,"
            "'artifact_files':sum(1 for p in pathlib.Path('/data/results').rglob('*') if p.is_file()),"
            "'adaptive_files':sum(1 for p in pathlib.Path('/data/adaptive').rglob('*') if p.is_file()),"
            "'tasks':len(glob.glob('/proc/1/task/*')),'fds':len(glob.glob('/proc/1/fd/*')),"
            "'sqlite_fds':sum(1 for p in glob.glob('/proc/1/fd/*') if '.db' in os.path.realpath(p)),"
            "'tcp_connections':sum(max(0,len(pathlib.Path(p).read_text().splitlines())-1) "
            "for p in ('/proc/1/net/tcp','/proc/1/net/tcp6'))}))"
        )
        output = self.compose(
            "exec", "-T", "--user", "scrapeyard", "scrapeyard", "python", "-c", script
        )
        return json.loads(output)

    def phase_recovery(self) -> None:
        observations: list[dict[str, Any]] = []
        for point, expectation in CRASH_EXPECTATIONS.items():
            project = f"item15-recovery-{point.replace('_', '-')}"
            case = point.replace("_", "-")
            os.environ["SCRAPEYARD_QUALIFICATION_CRASH_POINT"] = point
            self.compose("up", "-d", "--no-deps", "--force-recreate", "scrapeyard")
            self.wait_health()
            self.compose(
                "exec",
                "-T",
                "scrapeyard",
                "sh",
                "-c",
                "printf 'scrapeyard-item15-local-qualification-v1\\n' > "
                "/run/scrapeyard-qualification/enabled",
            )
            config = basic_config(project, case, "/static", webhook_case=case)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            pending = executor.submit(self.submit, config, timeout=self.phase_timeout)
            marker = f"/run/scrapeyard-qualification/reached-{point}"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = subprocess.run(
                    ["docker", "compose", *self.compose_args, "exec", "-T", "scrapeyard", "test", "-f", marker],
                    cwd=self.repo_root,
                    capture_output=True,
                )
                if result.returncode == 0:
                    break
                time.sleep(0.1)
            else:
                executor.shutdown(wait=False, cancel_futures=True)
                raise QualificationFailure(f"crash_checkpoint_timeout point={point}")

            self.compose("kill", "-s", "SIGKILL", "scrapeyard", timeout=15)
            try:
                pending.result(timeout=5)
            except Exception:
                pass
            executor.shutdown(wait=False, cancel_futures=True)
            if point != "after_enqueue_before_claim" and point != "after_terminal_state_before_delivery_ack":
                time.sleep(7)
            os.environ["SCRAPEYARD_QUALIFICATION_CRASH_POINT"] = ""
            recovery_started = time.monotonic()
            self.compose("up", "-d", "--no-deps", "--force-recreate", "scrapeyard")
            self.wait_health()
            summary = self.find_job(project)
            detail = self.wait_job(summary["job_id"])
            recovery_seconds = time.monotonic() - recovery_started
            if recovery_seconds > self.thresholds.recovery_seconds:
                raise QualificationFailure(
                    f"recovery_threshold_exceeded point={point} seconds={recovery_seconds:.3f}"
                )
            expected_status = (
                "complete"
                if point in {
                    "after_enqueue_before_claim",
                    "after_terminal_state_before_delivery_ack",
                }
                else "failed"
            )
            if detail["status"] != expected_status:
                raise QualificationFailure(
                    f"crash_convergence_failure point={point} expected={expected_status} "
                    f"actual={detail['status']}"
                )
            result_status, result = self.request(f"/results/{summary['job_id']}")
            if point == "after_result_artifact_write" and result_status != 404:
                raise QualificationFailure("result-write crash retained unindexed result metadata")
            stats = self.fixture_stats()
            webhook_attempts = stats.get("webhook_attempts", {}).get(case, 0)
            if point == "after_terminal_state_before_delivery_ack":
                delivery_deadline = time.monotonic() + self.thresholds.recovery_seconds
                while webhook_attempts != 1 and time.monotonic() < delivery_deadline:
                    time.sleep(0.2)
                    stats = self.fixture_stats()
                    webhook_attempts = stats.get("webhook_attempts", {}).get(case, 0)
                if webhook_attempts != 1:
                    raise QualificationFailure(
                        f"terminal acknowledgement crash delivered {webhook_attempts} times"
                    )
            observations.append(
                {
                    "point": point,
                    "expected_convergence": expectation,
                    "job_status": detail["status"],
                    "run_status": detail["runs"][0]["status"],
                    "result_http_status": result_status,
                    "webhook_attempts": webhook_attempts,
                    "recovery_seconds": round(recovery_seconds, 3),
                }
            )
            # A killed arq delivery intentionally retains its in-progress lock
            # until the queue timeout. Isolate crash points so four stale base
            # queue members cannot head-of-line block the fifth scenario.
            self.compose("down", "-v", "--remove-orphans", timeout=120)
            self.compose("up", "-d", "--no-build", timeout=120)
            self.wait_health(timeout=60)
        self.report.phases["recovery"] = observations

    def phase_redis_restart(self) -> None:
        jobs: list[tuple[str, str]] = []
        for index in range(10):
            priority = ("high", "normal", "low")[index % 3]
            case = f"redis-{index}"
            jobs.append(
                (
                    self.submit(
                        basic_config(
                            "item15-redis",
                            case,
                            f"/delay?seconds=10&case={case}",
                            priority=priority,
                        )
                    ),
                    priority,
                )
            )
        deadline = time.monotonic() + 20
        before = None
        while time.monotonic() < deadline:
            before = self.wait_health()
            workers = before["workers"]
            if workers["active_tasks"] >= 2 and sum(workers["queue_depths"].values()) >= 4:
                break
            time.sleep(0.2)
        else:
            raise QualificationFailure(f"redis_restart_setup_failure health={before}")

        persistence = self.compose(
            "exec", "-T", "redis", "redis-cli", "WAITAOF", "1", "1", "5000", check=False
        ).strip()
        self.compose("kill", "-s", "SIGKILL", "redis", timeout=15)
        time.sleep(8)
        unavailable = self.wait_health(healthy=False, timeout=15)
        restart_started = time.monotonic()
        self.compose("start", "redis")
        # A prolonged disconnect terminates arq's control loop even though the
        # API process can reconnect for health probes. The qualified recovery
        # procedure restarts the single Scrapeyard process after Redis is back.
        time.sleep(1)
        self.compose("stop", "-t", "15", "scrapeyard", timeout=30)
        self.compose("start", "scrapeyard", timeout=30)
        self.wait_health(timeout=self.thresholds.recovery_seconds)
        redis_recovery = time.monotonic() - restart_started
        completed = [self.wait_job(job_id) for job_id, _ in jobs]
        drain_seconds = time.monotonic() - restart_started
        if drain_seconds > self.thresholds.drain_seconds:
            raise QualificationFailure(f"redis_drain_threshold_exceeded seconds={drain_seconds}")
        for detail in completed:
            if detail["status"] not in {"complete", "failed"} or detail["run_count"] != 1:
                raise QualificationFailure(f"redis_ownership_convergence_failure detail={detail}")
        depths = self.wait_health()["workers"]["queue_depths"]
        if any(depths.values()):
            raise QualificationFailure(f"redis_queue_not_drained depths={depths}")
        target_requests = int(self.fixture_stats().get("requests", {}).get("/delay", 0))
        if target_requests != len(jobs):
            raise QualificationFailure(
                f"redis_duplicate_execution_failure requests={target_requests} jobs={len(jobs)}"
            )
        self.report.phases["redis_restart"] = {
            "aof_policy": "appendonly yes; appendfsync everysec; aof-use-rdb-preamble yes",
            "waitaof": persistence,
            "queued_jobs": len(jobs),
            "mixed_priorities": sorted({priority for _, priority in jobs}),
            "unavailable_health": unavailable["status"],
            "recovery_seconds": round(redis_recovery, 3),
            "application_restart_required": True,
            "drain_seconds": round(drain_seconds, 3),
            "terminal_statuses": _counts(item["status"] for item in completed),
            "max_run_count": max(item["run_count"] for item in completed),
            "target_requests": target_requests,
            "final_queue_depths": depths,
        }

    def phase_load(self) -> None:
        start_growth = self.runtime_growth()
        basic_jobs: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            futures = [
                executor.submit(
                    self.submit,
                    basic_config(
                        "item15-load-basic",
                        f"basic-{index}",
                        f"/delay?seconds=1&case=basic-{index}",
                    ),
                )
                for index in range(12)
            ]
            basic_jobs = [future.result() for future in futures]
        max_active = 0
        max_depth = 0
        while True:
            health = self.wait_health()
            workers = health["workers"]
            max_active = max(max_active, workers["active_tasks"])
            max_depth = max(max_depth, sum(workers["queue_depths"].values()))
            statuses = [self.request(f"/jobs/{job_id}", record_latency=True)[1]["status"] for job_id in basic_jobs]
            if all(status in TERMINAL for status in statuses):
                break
            time.sleep(0.15)
        if max_active > 4:
            raise QualificationFailure(f"worker_concurrency_bypass active={max_active}")

        browser_modes = (
            ("dynamic", False),
            ("dynamic", True),
            ("stealthy", False),
            ("dynamic", False),
        )
        browser_jobs = [
            self.submit(
                browser_config(
                    "item15-load-browser",
                    f"browser-{index}",
                    fetcher=fetcher,
                    stealth=stealth,
                )
            )
            for index, (fetcher, stealth) in enumerate(browser_modes)
        ]
        max_browsers = 0
        while True:
            health = self.wait_health()
            max_browsers = max(max_browsers, health["workers"]["active_browsers"])
            statuses = [self.request(f"/jobs/{job_id}")[1]["status"] for job_id in browser_jobs]
            if all(status in TERMINAL for status in statuses):
                break
            self.sample_resources()
            time.sleep(0.2)
        if max_browsers > 2:
            raise QualificationFailure(f"browser_concurrency_bypass active={max_browsers}")
        for job_id in browser_jobs:
            if self.wait_job(job_id)["status"] != "complete":
                raise QualificationFailure(f"real_browser_load_failed job_id={job_id}")

        near_record = self.wait_job(
            self.submit(large_config("near-record-limit", count=990, value_size=8))
        )
        near_serialized = self.wait_job(
            self.submit(large_config("near-serialized-limit", count=350, value_size=4500))
        )
        if (
            near_record["status"] != "complete"
            or near_record["runs"][0]["record_count"] != 990
        ):
            raise QualificationFailure(f"near_record_limit_failure detail={near_record}")
        if (
            near_serialized["status"] != "complete"
            or near_serialized["runs"][0]["record_count"] != 350
        ):
            raise QualificationFailure(
                f"near_serialized_limit_failure detail={near_serialized}"
            )
        limit_cases = {
            "record_limit": large_config("record-limit", count=1001, value_size=1),
            "serialized_limit": large_config("serialized-limit", count=500, value_size=5000),
            "fetched_limit": large_config("fetched-limit", count=150, value_size=65536),
            "duration_limit": basic_config(
                "item15-load-limits", "duration-limit", "/delay?seconds=35&case=duration-limit"
            ),
        }
        limit_results: dict[str, str] = {}
        for name, config in limit_cases.items():
            detail = self.wait_job(self.submit(config), timeout=90)
            limit_results[name] = detail["status"]
            if detail["status"] != "failed":
                raise QualificationFailure(f"run_limit_bypass limit={name} detail={detail}")

        oversized = b"project: item15\nname: " + b"x" * 70_000
        oversized_status, _ = self.request(
            "/scrape",
            data=oversized,
            content_type="application/x-yaml",
        )
        unauthenticated_status, _ = self.request("/jobs", authenticated=False)
        if oversized_status != 413 or unauthenticated_status != 401:
            raise QualificationFailure(
                f"api_guard_failure oversized={oversized_status} unauth={unauthenticated_status}"
            )

        pressure_jobs = [
            self.submit(
                basic_config(
                    "item15-read-pressure",
                    f"pressure-{index}",
                    f"/delay?seconds=1&case=pressure-{index}",
                )
            )
            for index in range(8)
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
            reads = [
                executor.submit(self.request, "/jobs?limit=100", record_latency=True)
                for _ in range(80)
            ]
            read_statuses = [future.result()[0] for future in reads]
        if set(read_statuses) != {200}:
            raise QualificationFailure(f"authenticated_read_failure statuses={_counts(read_statuses)}")
        for job_id in pressure_jobs:
            self.wait_job(job_id)

        p95 = percentile(self.report.latency_ms, 95)
        if p95 > self.thresholds.read_p95_ms:
            raise QualificationFailure(
                f"latency_threshold_exceeded percentile=p95 observed_ms={p95:.3f} "
                f"threshold_ms={self.thresholds.read_p95_ms}"
            )
        end_growth = self.runtime_growth()
        disk_growth = sum(end_growth[key] - start_growth[key] for key in ("db_bytes", "result_bytes", "adaptive_bytes"))
        if disk_growth > self.thresholds.disk_growth_mib * 1024 * 1024:
            raise QualificationFailure(
                f"load_disk_growth_exceeded growth_bytes={disk_growth} "
                f"threshold_mib={self.thresholds.disk_growth_mib}"
            )
        self.report.phases["load"] = {
            "basic_jobs": len(basic_jobs),
            "browser_jobs": len(browser_jobs),
            "browser_modes": [
                f"{fetcher}{'-stealth' if stealth else ''}"
                for fetcher, stealth in browser_modes
            ],
            "max_worker_concurrency": max_active,
            "max_browser_concurrency": max_browsers,
            "max_queue_depth": max_depth,
            "near_record_limit_records": 990,
            "near_serialized_limit_records": 350,
            "limit_terminal_statuses": limit_results,
            "oversized_request_status": oversized_status,
            "unauthenticated_read_status": unauthenticated_status,
            "authenticated_pressure_reads": len(read_statuses),
            "latency_p50_ms": round(percentile(self.report.latency_ms, 50), 3),
            "latency_p95_ms": round(p95, 3),
            "disk_growth_bytes": disk_growth,
        }

    def phase_soak(self) -> None:
        before = self.runtime_growth()
        before_sample = self.sample_resources()
        schedule_name = f"scheduler-{int(time.time())}"
        schedule_config = scheduled_config(schedule_name)
        status, created = self.request(
            "/jobs",
            data=schedule_config.encode(),
            content_type="application/x-yaml",
        )
        if status != 201:
            raise QualificationFailure(f"schedule_creation_failure status={status} payload={created}")
        scheduled_job = created["job_id"]

        retry_job = self.submit(
            basic_config(
                "item15-soak",
                "webhook-retry",
                "/static",
                webhook_case="eventual",
                webhook_failures=2,
            )
        )
        failed_job = self.submit(
            basic_config(
                "item15-soak",
                "webhook-terminal",
                "/static",
                webhook_case="terminal",
                webhook_failures=99,
            )
        )
        self.wait_job(retry_job)
        self.wait_job(failed_job)
        self.compose(
            "exec", "-T", "--user", "scrapeyard", "scrapeyard", "sh", "-c",
            "mkdir -p /data/results/item15-orphan/job/run && "
            "printf orphan > /data/results/item15-orphan/job/run/results.json && "
            "touch -d '2 minutes ago' /data/results/item15-orphan/job/run/results.json "
            "/data/results/item15-orphan/job/run /data/results/item15-orphan/job",
        )

        samples: list[dict[str, Any]] = []
        deadline = time.monotonic() + self.soak_seconds
        while time.monotonic() < deadline:
            samples.append(self.sample_resources())
            time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
        scheduled = self.request(f"/jobs/{scheduled_job}")[1]
        minimum_fires = max(2, math.floor(self.soak_seconds / 60) - 1)
        if scheduled["run_count"] < minimum_fires:
            raise QualificationFailure(
                f"scheduler_soak_failure runs={scheduled['run_count']} minimum={minimum_fires}"
            )
        retained_results = int(
            self.compose(
                "exec", "-T", "--user", "scrapeyard", "scrapeyard", "python", "-c",
                "import sqlite3; "
                f"db=sqlite3.connect('/data/db/results_meta.db'); "
                f"print(db.execute(\"SELECT COUNT(*) FROM results_meta WHERE job_id = ?\", "
                f"('{scheduled_job}',)).fetchone()[0])",
            ).strip()
        )
        if retained_results > 4 or (self.profile == "full" and retained_results != 4):
            raise QualificationFailure(
                f"scheduled_result_retention_failure retained={retained_results} maximum=4"
            )

        stats = self.fixture_stats()
        eventual = stats.get("webhook_attempts", {}).get("eventual", 0)
        terminal = stats.get("webhook_attempts", {}).get("terminal", 0)
        delivered = stats.get("webhook_delivered", {}).get("eventual", 0)
        if (eventual, delivered, terminal) != (3, 1, 3):
            raise QualificationFailure(
                f"webhook_soak_failure eventual={eventual} delivered={delivered} terminal={terminal}"
            )
        orphan_exists = self.compose(
            "exec", "-T", "scrapeyard", "test", "-e", "/data/results/item15-orphan/job/run",
            check=False,
        )
        if orphan_exists.strip():
            # `test` has no stdout; use a second classified probe below.
            pass
        probe = subprocess.run(
            ["docker", "compose", *self.compose_args, "exec", "-T", "scrapeyard", "test", "-e", "/data/results/item15-orphan/job/run"],
            cwd=self.repo_root,
        )
        if probe.returncode == 0:
            raise QualificationFailure("orphan_cleanup_failure path_still_exists=true")

        after = self.runtime_growth()
        after_sample = self.sample_resources()
        memory_values = [sample.get("memory_mib", 0.0) for sample in samples]
        memory_growth = after_sample.get("memory_mib", 0.0) - before_sample.get("memory_mib", 0.0)
        db_growth = after["db_bytes"] - before["db_bytes"]
        task_growth = after["tasks"] - before["tasks"]
        if memory_growth > self.thresholds.memory_growth_mib:
            raise QualificationFailure(f"soak_memory_growth_exceeded growth_mib={memory_growth}")
        if db_growth > self.thresholds.db_growth_mib * 1024 * 1024:
            raise QualificationFailure(f"soak_database_growth_exceeded growth_bytes={db_growth}")
        if task_growth > self.thresholds.task_growth:
            raise QualificationFailure(f"soak_task_growth_exceeded growth={task_growth}")
        self.report.phases["soak"] = {
            "duration_seconds": self.soak_seconds,
            "scheduler_runs": scheduled["run_count"],
            "scheduler_results_retained": retained_results,
            "webhook_eventual_attempts": eventual,
            "webhook_terminal_attempts": terminal,
            "webhook_deliveries": delivered,
            "orphan_removed": True,
            "db_growth_bytes": db_growth,
            "result_growth_bytes": after["result_bytes"] - before["result_bytes"],
            "adaptive_growth_bytes": after["adaptive_bytes"] - before["adaptive_bytes"],
            "task_growth": task_growth,
            "fd_growth": after["fds"] - before["fds"],
            "sqlite_fd_growth": after["sqlite_fds"] - before["sqlite_fds"],
            "tcp_connection_growth": after["tcp_connections"] - before["tcp_connections"],
            "artifact_file_growth": after["artifact_files"] - before["artifact_files"],
            "adaptive_file_growth": after["adaptive_files"] - before["adaptive_files"],
            "memory_growth_mib": round(memory_growth, 3),
            "memory_peak_mib": round(max(memory_values or [0.0]), 3),
        }

    def phase_backup_restore(self) -> None:
        known_success = self.submit(
            basic_config(
                "item15-backup",
                "known-success",
                "/static",
                webhook_case="backup-known",
                adaptive=True,
            )
        )
        known_failure = self.submit(
            basic_config("item15-backup", "known-error", "/redirect-unsafe")
        )
        success_detail = self.wait_job(known_success)
        failure_detail = self.wait_job(known_failure)
        schedule_status, schedule_created = self.request(
            "/jobs",
            data=scheduled_config(
                "known-schedule",
                project="item15-backup-schedule",
                enabled=False,
            ).encode(),
            content_type="application/x-yaml",
        )
        if schedule_status != 201:
            raise QualificationFailure(
                f"backup_schedule_creation_failure status={schedule_status} payload={schedule_created}"
            )
        schedule_id = schedule_created["job_id"]
        schedule_detail = self.request(f"/jobs/{schedule_id}")[1]
        schedule_detail.pop("next_run_at", None)
        adaptive_content = "item15-adaptive-known-v1\n"
        self.compose(
            "exec", "-T", "--user", "scrapeyard", "scrapeyard", "sh", "-c",
            "mkdir -p /data/adaptive/item15-backup && "
            "printf 'item15-adaptive-known-v1\\n' > "
            "/data/adaptive/item15-backup/qualification-metadata.txt",
        )
        success_result = self.request(f"/results/{known_success}")
        errors = self.request(f"/errors?job_id={known_failure}&limit=100")
        expected = {
            "success_detail": success_detail,
            "failure_detail": failure_detail,
            "schedule_detail": schedule_detail,
            "success_result": success_result,
            "errors": errors,
        }
        (self.diagnostics / "restore-expected-state.json").write_text(
            json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        self.compose("stop", "-t", "40", "scrapeyard", timeout=60)
        waitaof = self.compose(
            "exec", "-T", "redis", "redis-cli", "WAITAOF", "1", "1", "5000", check=False
        ).strip()
        self.compose("exec", "-T", "redis", "redis-cli", "SAVE", timeout=30)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        # The random host directory belongs to the invoking operator (UID/GID
        # 1000 in the qualification runner), while /data is mode-0750/0600 and
        # owned by the production UID 10001. Run as the data owner with the
        # host directory's group, and grant only that group temporary access.
        self.backup_dir.chmod(0o770)
        tool = self.repo_root / "scripts" / "qualification_backup.py"
        backup_mount = f"{self.backup_dir}:/qualification-backup"
        tool_mount = f"{tool}:/qualification_backup.py:ro"
        self.compose(
            "run", "--rm", "--no-deps", "--user", "10001:1000", "--entrypoint", "sh",
            "-v", backup_mount, "-v", tool_mount,
            "scrapeyard", "-c",
            "python /qualification_backup.py create --data-root /data "
            "--output /qualification-backup/set --quiesced "
            "&& chmod -R g+rwX /qualification-backup/set",
            timeout=120,
        )
        validation = subprocess.run(
            [sys.executable, str(tool), "validate", "--backup", str(self.backup_dir / "set")],
            text=True,
            capture_output=True,
            timeout=60,
        )
        if validation.returncode != 0:
            raise QualificationFailure(
                f"backup_validation_failure: {validation.stderr.strip()}"
            )
        manifest = json.loads(validation.stdout)

        self.compose("down", "-v", "--remove-orphans", timeout=120)
        self.compose(
            "run", "--rm", "--no-deps", "--user", "10001:1000", "--entrypoint", "python",
            "-v", backup_mount, "-v", tool_mount,
            "scrapeyard", "/qualification_backup.py", "restore",
            "--backup", "/qualification-backup/set", "--data-root", "/data",
            timeout=120,
        )
        os.environ["SCRAPEYARD_QUALIFICATION_CRASH_POINT"] = ""
        self.compose("up", "-d", "--no-build", timeout=120)
        self.wait_health(timeout=60)
        observed = {
            "success_detail": self.request(f"/jobs/{known_success}")[1],
            "failure_detail": self.request(f"/jobs/{known_failure}")[1],
            "success_result": self.request(f"/results/{known_success}"),
            "errors": self.request(f"/errors?job_id={known_failure}&limit=100"),
        }
        observed_schedule = self.request(f"/jobs/{schedule_id}")[1]
        observed_schedule.pop("next_run_at", None)
        observed["schedule_detail"] = observed_schedule
        adaptive_restored = self.compose(
            "exec", "-T", "--user", "scrapeyard", "scrapeyard", "cat",
            "/data/adaptive/item15-backup/qualification-metadata.txt",
        )
        if adaptive_restored != adaptive_content:
            raise QualificationFailure("restored adaptive metadata mismatch")
        if observed != expected:
            (self.diagnostics / "restore-observed-state.json").write_text(
                json.dumps(observed, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            raise QualificationFailure("restored_state_mismatch; see bounded state diagnostics")
        adaptive_files = [
            entry for entry in manifest["files"] if entry["path"].startswith("adaptive/")
        ]
        self.report.phases["backup_restore"] = {
            "snapshot_order": manifest["snapshot_order"],
            "databases": manifest["required_databases"],
            "manifest_files": len(manifest["files"]),
            "manifest_bytes": sum(entry["bytes"] for entry in manifest["files"]),
            "row_counts": manifest["row_counts"],
            "adaptive_artifacts": len(adaptive_files),
            "redis_waitaof": waitaof,
            "fresh_restore_exact_match": True,
            "known_jobs": [known_success, known_failure],
            "known_schedule": schedule_id,
            "adaptive_exact_match": True,
        }

    def finalize(self) -> None:
        samples = self.report.resource_samples
        peak_memory = max((item.get("memory_mib", 0.0) for item in samples), default=0.0)
        peak_cpu = max((item.get("cpu_percent", 0.0) for item in samples), default=0.0)
        if peak_memory > self.thresholds.memory_peak_mib:
            raise QualificationFailure(
                f"memory_threshold_exceeded observed_mib={peak_memory} "
                f"threshold_mib={self.thresholds.memory_peak_mib}"
            )
        if peak_cpu > self.thresholds.cpu_percent + 25:
            raise QualificationFailure(
                f"cpu_threshold_exceeded observed_percent={peak_cpu} "
                f"threshold_percent={self.thresholds.cpu_percent} tolerance=25"
            )
        summary = {
            **asdict(self.report),
            "total_seconds": round(time.monotonic() - self.report.started_monotonic, 3),
            "latency_p50_ms": round(percentile(self.report.latency_ms, 50), 3),
            "latency_p95_ms": round(percentile(self.report.latency_ms, 95), 3),
            "peak_memory_mib": round(peak_memory, 3),
            "peak_cpu_percent": round(peak_cpu, 3),
        }
        summary.pop("started_monotonic", None)
        (self.diagnostics / "qualification-report.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        console_summary = dict(summary)
        console_summary.pop("latency_ms", None)
        console_summary.pop("resource_samples", None)
        print(json.dumps(console_summary, indent=2, sort_keys=True))


def execution_block(priority: str = "normal") -> str:
    return f"""execution:
  mode: async
  concurrency: 1
  delay_between: 0
  domain_rate_limit: 0
  priority: {priority}
retry:
  max_attempts: 1
  backoff: fixed
  backoff_max: 0
"""


def basic_config(
    project: str,
    name: str,
    path: str,
    *,
    priority: str = "normal",
    webhook_case: str | None = None,
    webhook_failures: int = 0,
    adaptive: bool = False,
) -> str:
    webhook = ""
    if webhook_case is not None:
        webhook = f"""webhook:
  url: "{PUBLIC_ORIGIN}/webhook?case={webhook_case}&failures={webhook_failures}"
  on: [complete, partial, failed]
"""
    return f"""project: {project}
name: {name}
adaptive: {str(adaptive).lower()}
{execution_block(priority)}{webhook}target:
  url: {PUBLIC_ORIGIN}{path}
  fetcher: basic
  selectors:
    value: ".value::text"
    static: "#static-value::text"
validation:
  min_results: 1
  on_empty: fail
"""


def browser_config(
    project: str,
    name: str,
    *,
    fetcher: str = "dynamic",
    stealth: bool = False,
) -> str:
    return f"""project: {project}
name: {name}
{execution_block()}target:
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
  selectors:
    value: "#js-value::text"
validation:
  required_fields: [value]
  min_results: 1
  on_empty: fail
"""


def large_config(name: str, *, count: int, value_size: int) -> str:
    return f"""project: item15-load-large
name: {name}
{execution_block()}target:
  url: {PUBLIC_ORIGIN}/large?count={count}&value_size={value_size}
  fetcher: basic
  item_selector: ".record"
  selectors:
    index: ".index::text"
    value: ".value::text"
validation:
  min_results: 1
  on_empty: fail
output:
  group_by: merge
"""


def scheduled_config(
    name: str,
    *,
    project: str = "item15-soak",
    enabled: bool = True,
) -> str:
    return f"""project: {project}
name: {name}
schedule:
  cron: "* * * * *"
  enabled: {str(enabled).lower()}
{execution_block()}target:
  url: {PUBLIC_ORIGIN}/static
  fetcher: basic
  selectors:
    static: "#static-value::text"
validation:
  required_fields: [static]
  min_results: 1
  on_empty: fail
"""


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((value / 100) * len(ordered)) - 1)
    return ordered[index]


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _percent(value: str) -> float:
    return float(value.strip().rstrip("%").replace(",", ".") or 0)


def _memory_mib(value: str) -> float:
    used = value.split("/", 1)[0].strip()
    match = re.fullmatch(r"([0-9.]+)([KMGTP]?i?B)", used)
    if not match:
        return 0.0
    amount = float(match.group(1))
    unit = match.group(2)
    factors = {"B": 1 / 1024**2, "kB": 1 / 1024, "KiB": 1 / 1024, "MB": 1, "MiB": 1, "GB": 1024, "GiB": 1024, "TB": 1024**2, "TiB": 1024**2}
    return amount * factors.get(unit, 0)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--fixture-url", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--diagnostics-dir", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--profile", choices=("quick", "full"), required=True)
    parser.add_argument(
        "--only-phase",
        choices=("all", "recovery", "redis_restart", "load", "soak", "backup_restore"),
        default="all",
    )
    parser.add_argument("--soak-seconds", type=int, required=True)
    parser.add_argument("--phase-timeout", type=int, default=300)
    parser.add_argument("--thresholds-json", required=True)
    parser.add_argument("--compose-arg", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    harness = Harness(args)
    phases: tuple[tuple[str, Callable[[], None]], ...] = (
        ("recovery", harness.phase_recovery),
        ("redis_restart", harness.phase_redis_restart),
        ("load", harness.phase_load),
        ("soak", harness.phase_soak),
        ("backup_restore", harness.phase_backup_restore),
    )
    if args.only_phase != "all":
        phases = tuple(item for item in phases if item[0] == args.only_phase)
    try:
        for name, phase in phases:
            print(f"qualification phase_start={name}", flush=True)
            started = time.monotonic()
            phase()
            print(
                f"qualification phase_complete={name} seconds={time.monotonic() - started:.3f}",
                flush=True,
            )
        harness.finalize()
    except (QualificationFailure, subprocess.SubprocessError, OSError) as exc:
        failure = {
            "classification": type(exc).__name__,
            "message": str(exc),
            "completed_phases": sorted(harness.report.phases),
        }
        (harness.diagnostics / "qualification-failure.json").write_text(
            json.dumps(failure, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"qualification_failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
