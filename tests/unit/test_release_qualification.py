from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts.qualification_backup import BackupError, create_backup, restore_backup, validate_backup
from scrapeyard.common.qualification import (
    QUALIFICATION_CRASH_POINTS,
    QUALIFICATION_SENTINEL_CONTENT,
    qualification_checkpoint,
)
from scrapeyard.common.settings import ServiceSettings
from tests.qualification.run_qualification import (
    CRASH_EXPECTATIONS,
    Thresholds,
    percentile,
)


def _yaml(path: str) -> dict:
    return yaml.load(Path(path).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _minimal_data(root: Path) -> None:
    db_dir = root / "db"
    result_dir = root / "results" / "project" / "job" / "run-1"
    adaptive_dir = root / "adaptive" / "project"
    db_dir.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    adaptive_dir.mkdir(parents=True)
    (result_dir / "results.json").write_text('{"known":"value"}\n', encoding="utf-8")
    (adaptive_dir / "selectors.json").write_text('{"selector":"h1"}\n', encoding="utf-8")

    with sqlite3.connect(db_dir / "jobs.db") as db:
        db.executescript(
            """
            CREATE TABLE jobs (job_id TEXT PRIMARY KEY);
            CREATE TABLE job_runs (run_id TEXT PRIMARY KEY, job_id TEXT NOT NULL);
            CREATE TABLE webhook_deliveries (delivery_id TEXT PRIMARY KEY);
            CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY);
            INSERT INTO jobs VALUES ('job-1');
            INSERT INTO job_runs VALUES ('run-1', 'job-1');
            INSERT INTO webhook_deliveries VALUES ('delivery-1');
            """
        )
    with sqlite3.connect(db_dir / "errors.db") as db:
        db.executescript(
            """
            CREATE TABLE errors (job_id TEXT, run_id TEXT);
            CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY);
            INSERT INTO errors VALUES ('job-1', 'run-1');
            """
        )
    with sqlite3.connect(db_dir / "results_meta.db") as db:
        db.executescript(
            """
            CREATE TABLE results_meta (job_id TEXT, run_id TEXT, file_path TEXT);
            CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY);
            """
        )
        db.execute(
            "INSERT INTO results_meta VALUES (?, ?, ?)",
            (
                "job-1",
                "run-1",
                str(result_dir.resolve()),
            ),
        )


def test_qualification_checkpoints_are_complete_disabled_and_sentinel_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert set(CRASH_EXPECTATIONS) == QUALIFICATION_CRASH_POINTS
    monkeypatch.setattr(
        "scrapeyard.common.qualification.get_settings",
        lambda: SimpleNamespace(
            qualification_mode=False,
            qualification_crash_point="after_enqueue_before_claim",
            qualification_marker_dir=str(tmp_path),
        ),
    )
    qualification_checkpoint("after_enqueue_before_claim")
    assert not list(tmp_path.iterdir())

    monkeypatch.setattr(
        "scrapeyard.common.qualification.get_settings",
        lambda: SimpleNamespace(
            qualification_mode=True,
            qualification_crash_point="after_enqueue_before_claim",
            qualification_marker_dir=str(tmp_path),
        ),
    )
    with pytest.raises(RuntimeError, match="sentinel is missing"):
        qualification_checkpoint("after_enqueue_before_claim")
    (tmp_path / "enabled").write_text(QUALIFICATION_SENTINEL_CONTENT, encoding="utf-8")
    (tmp_path / "release-after_enqueue_before_claim").touch()
    qualification_checkpoint("after_enqueue_before_claim")
    assert (tmp_path / "reached-after_enqueue_before_claim").is_file()


def test_qualification_settings_reject_silent_or_unknown_activation() -> None:
    with pytest.raises(ValueError, match="requires qualification_mode"):
        ServiceSettings(qualification_crash_point="during_target_execution")
    with pytest.raises(ValueError, match="supported local checkpoint"):
        ServiceSettings(qualification_mode=True, qualification_crash_point="unknown")


def test_backup_manifest_round_trip_and_inconsistency_detection(tmp_path: Path) -> None:
    data = tmp_path / "data"
    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    _minimal_data(data)
    manifest = create_backup(data, backup, quiesced=True)
    validated = validate_backup(backup)
    assert manifest["required_databases"] == ["jobs.db", "errors.db", "results_meta.db"]
    assert validated["row_counts"]["jobs.db:jobs"] == 1
    restore_backup(backup, restored)
    assert (restored / "results/project/job/run-1/results.json").read_bytes() == (
        data / "results/project/job/run-1/results.json"
    ).read_bytes()
    assert (restored / "adaptive/project/selectors.json").read_bytes() == (
        data / "adaptive/project/selectors.json"
    ).read_bytes()

    manifest_path = backup / "manifest.json"
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["files"] = value["files"][:-1]
    manifest_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(BackupError, match="inventory mismatch"):
        validate_backup(backup)


def test_backup_requires_quiescing_and_restore_requires_fresh_destination(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _minimal_data(data)
    with pytest.raises(BackupError, match="--quiesced"):
        create_backup(data, tmp_path / "backup", quiesced=False)
    create_backup(data, tmp_path / "backup", quiesced=True)
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "existing").touch()
    with pytest.raises(BackupError, match="not empty"):
        restore_backup(tmp_path / "backup", destination)


def test_backup_accepts_results_intentionally_retained_after_job_deletion(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    backup = tmp_path / "backup"
    _minimal_data(data)
    with sqlite3.connect(data / "db/jobs.db") as db:
        db.execute("DELETE FROM job_runs")
        db.execute("DELETE FROM jobs")
    with sqlite3.connect(data / "db/errors.db") as db:
        db.execute("DELETE FROM errors")

    create_backup(data, backup, quiesced=True)

    validated = validate_backup(backup)
    assert validated["row_counts"]["results_meta.db:results_meta"] == 1


def test_backup_rejects_result_metadata_outside_declared_data_root(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    _minimal_data(data)
    with sqlite3.connect(data / "db/results_meta.db") as db:
        db.execute(
            "UPDATE results_meta SET file_path = ?",
            ("/tmp/attacker/results/project/job/run-1",),
        )

    with pytest.raises(BackupError, match="metadata path is outside"):
        create_backup(data, tmp_path / "backup", quiesced=True)

    assert not (tmp_path / "backup").exists()


def test_restore_rolls_back_partial_directory_install_for_safe_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    backup = tmp_path / "backup"
    destination = tmp_path / "restored"
    _minimal_data(data)
    create_backup(data, backup, quiesced=True)
    destination.mkdir()
    for name in ("db", "results", "adaptive", "logs"):
        (destination / name).mkdir(mode=0o750)

    original_replace = os.replace
    calls = 0

    def _fail_second_install(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated install failure")
        original_replace(source, target)

    monkeypatch.setattr("scripts.qualification_backup.os.replace", _fail_second_install)
    with pytest.raises(OSError, match="simulated install failure"):
        restore_backup(backup, destination)

    assert {child.name for child in destination.iterdir()} == {
        "db",
        "results",
        "adaptive",
        "logs",
    }
    assert all(not any((destination / name).iterdir()) for name in ("db", "results", "adaptive", "logs"))

    monkeypatch.setattr("scripts.qualification_backup.os.replace", original_replace)
    restore_backup(backup, destination)
    assert (destination / "results/project/job/run-1/results.json").is_file()


def test_threshold_percentiles_are_nearest_rank_and_profile_is_host_sized() -> None:
    assert percentile([1, 2, 3, 4, 100], 95) == 100
    thresholds = Thresholds()
    assert thresholds.cpu_percent == 400
    assert thresholds.memory_peak_mib == 6144
    assert thresholds.read_p95_ms == 750
    assert thresholds.memory_growth_mib < thresholds.memory_peak_mib


def test_qualification_compose_inherits_security_and_declares_aof_and_limits() -> None:
    production = _yaml("docker-compose.yml")
    smoke = _yaml("docker-compose.smoke.yml")
    qualification = _yaml("docker-compose.qualification.yml")
    production_app = production["services"]["scrapeyard"]
    assert production_app["security_opt"] == [
        "no-new-privileges:true",
        "apparmor:scrapeyard-chromium",
        "seccomp:./security/seccomp/chromium.json",
    ]
    assert production_app["cap_add"] == ["SYS_CHROOT"]
    assert production_app["read_only"] == "true"
    assert production_app["cap_drop"] == ["ALL"]
    assert smoke["networks"]["fixture-public"]["internal"] == "true"
    redis_command = qualification["services"]["redis"]["command"]
    assert redis_command == [
        "redis-server",
        "--appendonly",
        "yes",
        "--appendfsync",
        "everysec",
        "--aof-use-rdb-preamble",
        "yes",
    ]
    app = qualification["services"]["scrapeyard"]
    assert app["cpus"].endswith("4}")
    assert app["mem_limit"].endswith("8g}")
    assert app["environment"]["SCRAPEYARD_WORKERS_MAX_CONCURRENT"] == "4"
    assert app["environment"]["SCRAPEYARD_WORKERS_MAX_BROWSERS"] == "2"
    assert app["environment"]["SCRAPEYARD_RUN_MAX_EXTRACTED_RECORDS"] == "1000"
    assert app["environment"]["SCRAPEYARD_RUN_MAX_SERIALIZED_RESULT_BYTES"] == "2097152"


def test_runner_has_scoped_cleanup_signal_timeout_and_secret_scan_contracts() -> None:
    runner = Path("scripts/run_release_qualification.sh")
    subprocess.run([str(runner), "--help"], check=True, capture_output=True, text=True)
    text = runner.read_text(encoding="utf-8")
    for contract in (
        "trap on_exit EXIT",
        "trap 'exit 130' INT TERM HUP",
        "down -v --remove-orphans",
        "--filter \"label=com.docker.compose.project=$PROJECT\"",
        'timeout "$GLOBAL_TIMEOUT"',
        'timeout "$BUILD_TIMEOUT"',
        'SCRAPEYARD_EXIT_CODE" == "0" || "$SCRAPEYARD_EXIT_CODE" == "143"',
        "scan_diagnostics",
        "SQLite format 3",
        "SCRAPEYARD_BIND_ADDRESS=\"127.0.0.1\"",
    ):
        assert contract in text
    for destructive in ("docker system prune", "docker builder prune", "volume prune", "network prune"):
        assert destructive not in text

    harness = Path("tests/qualification/run_qualification.py").read_text(encoding="utf-8")
    assert 'last = self.request("/health/ready", timeout=3)' in harness
    assert 'last = self.request("/health", authenticated=False' not in harness
    assert harness.count('"--user", "10001:1000"') == 2
    assert '"--user", "1000:1000"' not in harness
    assert "chmod -R g+rwX /qualification-backup/set" in harness


def test_release_workflow_triggers_permissions_jobs_and_no_mutable_cache() -> None:
    workflow = _yaml(".github/workflows/release-qualification.yml")
    assert set(workflow["on"]) == {"pull_request", "schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"]["schedule"] == [{"cron": "41 7 * * 6"}]
    assert "github.event_name == 'pull_request'" in workflow["concurrency"]["cancel-in-progress"]
    assert set(workflow["jobs"]) == {"quick-recovery-restore", "full-load-soak"}
    qualification_paths = set(workflow["on"]["pull_request"]["paths"])
    assert "src/scrapeyard/**" in qualification_paths
    assert "sql/**" in qualification_paths
    quick = workflow["jobs"]["quick-recovery-restore"]
    full = workflow["jobs"]["full-load-soak"]
    assert quick["timeout-minutes"] == "60"
    assert full["timeout-minutes"] == "120"
    quick_commands = "\n".join(step.get("run", "") for step in quick["steps"])
    full_commands = "\n".join(step.get("run", "") for step in full["steps"])
    assert "--profile quick --no-cache" in quick_commands
    assert "--profile full --no-cache" in full_commands
    for job in (quick, full):
        assert not any(step.get("uses", "").startswith("actions/cache@") for step in job["steps"])
        artifact = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        assert artifact["if"] == "always()"
        assert artifact["with"]["retention-days"] == "7"
