#!/usr/bin/env python3
"""Create, validate, and restore a quiesced Scrapeyard backup set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

FORMAT = "scrapeyard-backup-v1"
DATABASES = ("jobs.db", "errors.db", "results_meta.db")
SNAPSHOT_ORDER = (
    "stop API ingress and scheduler",
    "drain/stop workers and webhook dispatcher",
    "close SQLite connections",
    "persist Redis AOF with WAITAOF/SAVE",
    "snapshot jobs.db, errors.db, results_meta.db",
    "snapshot results and adaptive artifacts",
    "write and verify manifest",
)
REQUIRED_TABLES = {
    "jobs.db": {"jobs", "job_runs", "webhook_deliveries", "schema_migrations"},
    "errors.db": {"errors", "schema_migrations"},
    "results_meta.db": {"results_meta", "schema_migrations"},
}


class BackupError(RuntimeError):
    """A classified backup-set contract failure."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise BackupError(f"symlink is not allowed in backup set: {path}")
        if path.is_file():
            files.append(path)
    return files


def sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src,
        sqlite3.connect(destination) as dst,
    ):
        src.backup(dst)


def create_backup(data_root: Path, output: Path, *, quiesced: bool) -> dict[str, Any]:
    if not quiesced:
        raise BackupError("creation requires --quiesced after the documented shutdown order")
    if output.exists():
        raise BackupError(f"backup destination already exists: {output}")
    for database in DATABASES:
        source = data_root / "db" / database
        if not source.is_file():
            raise BackupError(f"required database is missing: {source}")
    for directory in ("results", "adaptive"):
        if not (data_root / directory).is_dir():
            raise BackupError(f"required artifact directory is missing: {data_root / directory}")

    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        payload = stage / "payload"
        for database in DATABASES:
            sqlite_snapshot(data_root / "db" / database, payload / "db" / database)
        for directory in ("results", "adaptive"):
            shutil.copytree(data_root / directory, payload / directory)

        entries = [
            {
                "path": path.relative_to(payload).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in regular_files(payload)
        ]
        manifest = {
            "format": FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_data_root": str(data_root.resolve()),
            "snapshot_order": list(SNAPSHOT_ORDER),
            "required_databases": list(DATABASES),
            "files": entries,
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        validate_backup(stage)
        os.replace(stage, output)
        return manifest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def load_manifest(backup: Path) -> dict[str, Any]:
    try:
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is missing or invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupError("unsupported or missing backup manifest format")
    if manifest.get("required_databases") != list(DATABASES):
        raise BackupError("backup database inventory is incomplete or reordered")
    if manifest.get("snapshot_order") != list(SNAPSHOT_ORDER):
        raise BackupError("backup snapshot order contract is missing or invalid")
    return manifest


def _safe_manifest_path(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise BackupError("manifest file path must be a string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise BackupError(f"unsafe manifest path: {value!r}")
    return path


def _source_data_root(manifest: dict[str, Any]) -> Path:
    raw_root = manifest.get("source_data_root")
    if not isinstance(raw_root, str) or not raw_root:
        raise BackupError("backup source data root is missing or invalid")
    root = PurePosixPath(raw_root)
    if not root.is_absolute() or ".." in root.parts:
        raise BackupError("backup source data root must be an absolute safe path")
    return Path(*root.parts)


def _sqlite_contract(payload: Path, *, source_data_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for database in DATABASES:
        path = payload / "db" / database
        if not path.is_file():
            raise BackupError(f"required database absent from payload: {database}")
        with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as db:
            integrity = db.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise BackupError(f"SQLite integrity check failed for {database}: {integrity}")
            tables = {
                row[0]
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            missing = REQUIRED_TABLES[database] - tables
            if missing:
                raise BackupError(f"{database} is missing tables: {sorted(missing)}")
            for table in REQUIRED_TABLES[database] - {"schema_migrations"}:
                counts[f"{database}:{table}"] = int(
                    db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )

    jobs_db = payload / "db" / "jobs.db"
    results_db = payload / "db" / "results_meta.db"
    errors_db = payload / "db" / "errors.db"
    with sqlite3.connect(f"file:{jobs_db}?mode=ro&immutable=1", uri=True) as jobs:
        job_run_pairs = {
            (row[0], row[1])
            for row in jobs.execute("SELECT job_id, run_id FROM job_runs")
        }
    with sqlite3.connect(f"file:{results_db}?mode=ro&immutable=1", uri=True) as results:
        for job_id, run_id, file_path in results.execute(
            "SELECT job_id, run_id, file_path FROM results_meta"
        ):
            # Results can intentionally outlive deleted job/run metadata when
            # DELETE /jobs/{id}?delete_results=false is used. In that state the
            # result metadata and artifact remain the authoritative retained copy.
            relative = _artifact_relative_path(
                str(file_path),
                "results",
                data_root=source_data_root,
            )
            if not (payload / "results" / relative / "results.json").is_file():
                raise BackupError(f"result artifact is missing for {job_id}/{run_id}")
    with sqlite3.connect(f"file:{errors_db}?mode=ro&immutable=1", uri=True) as errors:
        for job_id, run_id in errors.execute("SELECT job_id, run_id FROM errors"):
            if (job_id, run_id) not in job_run_pairs:
                raise BackupError(f"error row has no matching job/run: {job_id}/{run_id}")
    return counts


def _artifact_relative_path(
    file_path: str,
    directory: str,
    *,
    data_root: Path,
) -> Path:
    pure = PurePosixPath(file_path)
    expected_root = PurePosixPath(data_root.as_posix()) / directory
    try:
        relative = pure.relative_to(expected_root)
    except ValueError as exc:
        raise BackupError(
            f"metadata path is outside {expected_root}: {file_path!r}"
        ) from exc
    if pure.is_absolute() is False or ".." in relative.parts or not relative.parts:
        raise BackupError(f"unsafe metadata artifact path: {file_path!r}")
    return Path(*relative.parts)


def validate_backup(backup: Path) -> dict[str, Any]:
    manifest = load_manifest(backup)
    source_data_root = _source_data_root(manifest)
    payload = backup / "payload"
    if not payload.is_dir():
        raise BackupError("backup payload directory is missing")
    declared: dict[str, dict[str, Any]] = {}
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BackupError("manifest files inventory must be a list")
    for entry in files:
        if not isinstance(entry, dict):
            raise BackupError("manifest file entry must be an object")
        relative = _safe_manifest_path(entry.get("path")).as_posix()
        if relative in declared:
            raise BackupError(f"duplicate manifest path: {relative}")
        declared[relative] = entry

    actual = {path.relative_to(payload).as_posix(): path for path in regular_files(payload)}
    if set(actual) != set(declared):
        raise BackupError(
            "backup inventory mismatch: "
            f"missing={sorted(set(declared) - set(actual))} "
            f"unexpected={sorted(set(actual) - set(declared))}"
        )
    for relative, path in actual.items():
        entry = declared[relative]
        if entry.get("bytes") != path.stat().st_size:
            raise BackupError(f"backup size mismatch: {relative}")
        if entry.get("sha256") != sha256(path):
            raise BackupError(f"backup checksum mismatch: {relative}")
    manifest["row_counts"] = _sqlite_contract(
        payload,
        source_data_root=source_data_root,
    )
    return manifest


def restore_backup(backup: Path, data_root: Path) -> dict[str, Any]:
    manifest = validate_backup(backup)
    source_data_root = _source_data_root(manifest)
    data_root.mkdir(parents=True, exist_ok=True)
    existing = list(data_root.iterdir())
    allowed_empty = {"db", "results", "adaptive", "logs"}
    if any(
        child.name not in allowed_empty
        or child.is_symlink()
        or not child.is_dir()
        or any(child.iterdir())
        for child in existing
    ):
        raise BackupError(f"restore destination is not empty: {data_root}")
    stage = data_root / f".restore-{os.getpid()}"
    if stage.exists():
        raise BackupError(f"restore staging path already exists: {stage}")
    installed: list[Path] = []
    original_modes: dict[str, int] = {}
    try:
        shutil.copytree(backup / "payload", stage)
        _sqlite_contract(stage, source_data_root=source_data_root)
        # A fresh production image initializes these empty mount points before
        # the named volume is first used. Preserve their modes for rollback and
        # keep empty logs in place.
        for name in ("db", "results", "adaptive"):
            path = data_root / name
            if path.exists():
                original_modes[name] = path.stat().st_mode & 0o7777
                path.rmdir()
        for child in stage.iterdir():
            destination = data_root / child.name
            os.replace(child, destination)
            installed.append(destination)
        stage.rmdir()
        _sqlite_contract(data_root, source_data_root=source_data_root)
    except BaseException:
        for destination in reversed(installed):
            shutil.rmtree(destination, ignore_errors=True)
        for name, mode in original_modes.items():
            path = data_root / name
            if not path.exists():
                path.mkdir(mode=mode)
                path.chmod(mode)
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return manifest


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--data-root", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--quiesced", action="store_true")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--backup", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--data-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "create":
            result = create_backup(args.data_root, args.output, quiesced=args.quiesced)
        elif args.command == "validate":
            result = validate_backup(args.backup)
        else:
            result = restore_backup(args.backup, args.data_root)
    except BackupError as exc:
        print(f"backup_contract_failure: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
