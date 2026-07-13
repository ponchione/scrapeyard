"""Local filesystem + SQLite implementation of the ResultStore protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import stat
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from scrapeyard.common.budgets import BudgetExceeded, BudgetLimitName, RunBudget
from scrapeyard.common.dt import parse_dt
from scrapeyard.common.ids import generate_run_id
from scrapeyard.common.paths import safe_join
from scrapeyard.common.qualification import qualification_checkpoint
from scrapeyard.common.time import utc_now
from scrapeyard.storage.database import db_transaction, get_db
from scrapeyard.storage.filesystem import (
    cleanup_safe_to_thread,
    ensure_directory,
    read_bytes_file_no_follow,
    read_json_file_no_follow,
    remove_directories,
    serialize_json_bytes,
    write_bytes_file,
)
from scrapeyard.storage.result_queries import (
    EXCESS_RESULTS_PER_JOB_QUERY,
    EXPIRED_RESULTS_QUERY,
    JOB_RESULTS_DELETE_QUERY,
    build_result_lookup_query,
)
from scrapeyard.storage.types import (
    ReconciliationOperationFailure,
    ResultArtifactFailure,
    ResultArtifactFailureKind,
    ResultMetadata,
    ResultPayload,
    ResultReconciliationReport,
    SaveResultMeta,
)

logger = logging.getLogger(__name__)
_RESULT_RUN_DIR_DEPTH = 3
_DELETE_ID_BATCH_SIZE = 500
_ATOMIC_TEMP_PATTERN = re.compile(
    r"^\.(?:results\.json|dynamic-main\.png|stealthy-main\.png)\."
    r"[1-9][0-9]*\.[0-9a-f]{32}\.tmp$"
)


@dataclass(frozen=True, slots=True)
class _RunIdentity:
    project: str
    job_name: str
    run_id: str

    @property
    def identifier(self) -> str:
        return "/".join(part[:80] for part in (self.project, self.job_name, self.run_id))


@dataclass(frozen=True, slots=True)
class _RemovalCandidate:
    identity: _RunIdentity
    path: Path
    size: int


@dataclass(slots=True)
class _ReconciliationState:
    metadata_rows_inspected: int = 0
    valid_artifacts: int = 0
    missing_result_files: int = 0
    corrupt_result_files: int = 0
    unreadable_result_files: int = 0
    unsafe_metadata_paths: int = 0
    filesystem_run_directories_inspected: int = 0
    malformed_entries_ignored: int = 0
    orphan_candidates: int = 0
    recent_candidates_skipped: int = 0
    active_run_candidates_skipped: int = 0
    metadata_race_candidates_skipped: int = 0
    active_run_race_candidates_skipped: int = 0
    stale_temporary_candidates: int = 0
    directories_would_remove: int = 0
    files_would_remove: int = 0
    directories_removed: int = 0
    files_removed: int = 0
    removed_bytes: int = 0
    artifact_failures: list[ResultArtifactFailure] = field(default_factory=list)
    operation_failures: list[ReconciliationOperationFailure] = field(default_factory=list)

    def report(self, *, dry_run: bool) -> ResultReconciliationReport:
        return ResultReconciliationReport(
            dry_run=dry_run,
            metadata_rows_inspected=self.metadata_rows_inspected,
            valid_artifacts=self.valid_artifacts,
            missing_result_files=self.missing_result_files,
            corrupt_result_files=self.corrupt_result_files,
            unreadable_result_files=self.unreadable_result_files,
            unsafe_metadata_paths=self.unsafe_metadata_paths,
            filesystem_run_directories_inspected=(self.filesystem_run_directories_inspected),
            malformed_entries_ignored=self.malformed_entries_ignored,
            orphan_candidates=self.orphan_candidates,
            recent_candidates_skipped=self.recent_candidates_skipped,
            active_run_candidates_skipped=self.active_run_candidates_skipped,
            metadata_race_candidates_skipped=self.metadata_race_candidates_skipped,
            active_run_race_candidates_skipped=(self.active_run_race_candidates_skipped),
            stale_temporary_candidates=self.stale_temporary_candidates,
            directories_would_remove=self.directories_would_remove,
            files_would_remove=self.files_would_remove,
            directories_removed=self.directories_removed,
            files_removed=self.files_removed,
            removed_bytes=self.removed_bytes,
            artifact_failures=tuple(self.artifact_failures),
            operation_failures=tuple(self.operation_failures),
        )


class LocalResultStore:
    """Stores scrape results on the local filesystem with metadata in SQLite.

    Parameters
    ----------
    results_dir:
        Root directory for result files.
    job_lookup:
        Async callable that takes a ``job_id`` and returns ``(project, job_name)``.
    """

    def __init__(
        self,
        results_dir: str,
        job_lookup: Callable[[str], Awaitable[tuple[str, str]]],
        active_run_lookup: Callable[[str, str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._results_dir = Path(results_dir)
        self._job_lookup = job_lookup
        self._active_run_lookup = active_run_lookup
        self._save_lock = asyncio.Lock()

    def _checked_result_dir(self, file_path: str) -> Path:
        path = Path(file_path)
        if ".." in path.parts:
            raise ValueError(f"Unsafe result path outside results_dir: {file_path!r}")
        root = self._results_dir.resolve(strict=False)
        resolved = Path(os.path.abspath(path))
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Unsafe result path outside results_dir: {file_path!r}") from exc
        if len(relative.parts) != _RESULT_RUN_DIR_DEPTH:
            raise ValueError(
                f"Unsafe result path must identify a project/job/run directory: {file_path!r}"
            )
        current = root
        for part in relative.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode):
                raise ValueError(
                    f"Unsafe symlinked result path may resolve outside results_dir: {file_path!r}"
                )
        return resolved

    def _checked_result_dirs(self, rows: Sequence[Mapping[str, Any]]) -> list[Path]:
        paths: list[Path] = []
        for row in rows:
            file_path = str(row["file_path"])
            try:
                paths.append(self._checked_result_dir(file_path))
            except ValueError:
                logger.warning("Skipping unsafe result directory during cleanup: %r", file_path)
        return paths

    @staticmethod
    async def _delete_metadata_ids(
        db: Any,
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        ids = [row["id"] for row in rows]
        for start in range(0, len(ids), _DELETE_ID_BATCH_SIZE):
            batch = ids[start : start + _DELETE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            await db.execute(
                f"DELETE FROM results_meta WHERE id IN ({placeholders})",
                batch,
            )

    async def _delete_by_ids(
        self,
        db: Any,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        if not rows:
            return 0

        await self._delete_metadata_ids(db, rows)
        await db.commit()
        # Delete files after metadata so a crash leaves orphaned files
        # (recoverable) rather than orphaned metadata rows pointing to
        # missing files.
        await asyncio.to_thread(remove_directories, self._checked_result_dirs(rows))
        return len(rows)

    async def _delete_files_then_ids(
        self,
        db: Any,
        rows: Sequence[Mapping[str, Any]],
    ) -> int:
        """Delete explicit lifecycle artifacts before their resumable metadata.

        Unlike general retention cleanup, cancellation/deletion must be safely
        retryable after a filesystem fault. Keeping metadata until every
        contained directory removal succeeds preserves the paths for a retry.
        """

        if not rows:
            return 0
        paths = [self._checked_result_dir(str(row["file_path"])) for row in rows]
        await cleanup_safe_to_thread(remove_directories, paths)
        await self._delete_metadata_ids(db, rows)
        await db.commit()
        return len(rows)

    async def save_result(
        self,
        job_id: str,
        data: Any,
        *,
        run_id: str | None = None,
        status: str = "complete",
        record_count: int | None = None,
        budget: RunBudget | None = None,
        max_serialized_bytes: int | None = None,
    ) -> SaveResultMeta:
        project, job_name = await self._job_lookup(job_id)
        run_id = run_id or generate_run_id()
        payload = await cleanup_safe_to_thread(serialize_json_bytes, data)
        if budget is not None:
            budget.enforce_serialized_result_bytes(len(payload))
        elif max_serialized_bytes is not None and len(payload) > max_serialized_bytes:
            raise BudgetExceeded(
                BudgetLimitName.serialized_result_bytes,
                max_serialized_bytes,
                len(payload),
            )

        run_dir = self._checked_result_dir(
            str(safe_join(self._results_dir, project, job_name, run_id))
        )
        async with self._save_lock:
            await cleanup_safe_to_thread(ensure_directory, run_dir)
            path = run_dir / "results.json"
            try:
                previous_payload = await cleanup_safe_to_thread(
                    read_bytes_file_no_follow,
                    path,
                )
            except FileNotFoundError:
                previous_payload = None

            metadata_committed = False
            try:
                await cleanup_safe_to_thread(write_bytes_file, path, payload)
                qualification_checkpoint("after_result_artifact_write")
                if budget is not None:
                    budget.check_deadline()

                async with get_db("results_meta.db") as db, db_transaction(db):
                    # Single atomic statement — the UNIQUE index on (job_id, run_id)
                    # lets INSERT OR REPLACE handle the upsert without a separate DELETE.
                    await db.execute(
                        """INSERT OR REPLACE INTO results_meta
                               (job_id, project, run_id, status, record_count,
                                file_path, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            job_id,
                            project,
                            run_id,
                            status,
                            record_count,
                            str(run_dir),
                            utc_now().isoformat(),
                        ),
                    )
                metadata_committed = True
                if budget is not None:
                    budget.check_deadline()
            except BaseException:
                if not metadata_committed:
                    if previous_payload is None:
                        with suppress(FileNotFoundError):
                            path.unlink()
                    else:
                        await cleanup_safe_to_thread(
                            write_bytes_file,
                            path,
                            previous_payload,
                        )
                raise

        return SaveResultMeta(
            run_id=run_id,
            file_path=str(run_dir),
            record_count=record_count,
            serialized_bytes=len(payload),
        )

    async def get_result(
        self,
        job_id: str,
        run_id: str | None = None,
    ) -> ResultPayload:
        sql, params = build_result_lookup_query(job_id, run_id)

        async with get_db("results_meta.db") as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()

        if row is None:
            raise KeyError(
                f"No results found for job {job_id!r}" + (f" run {run_id!r}" if run_id else "")
            )

        result_run_id = row["run_id"]
        status = row["status"]
        file_path = row["file_path"]

        path = self._checked_result_dir(str(file_path)) / "results.json"
        data = await asyncio.to_thread(read_json_file_no_follow, path)
        return ResultPayload(run_id=result_run_id, data=data, status=status)

    async def get_result_metadata(
        self,
        job_id: str,
        run_id: str,
    ) -> ResultMetadata | None:
        """Return one run's metadata without opening its filesystem artifact."""

        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                """SELECT job_id, run_id, status, record_count, file_path, created_at
                   FROM results_meta
                   WHERE job_id = ? AND run_id = ?""",
                (job_id, run_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        created_at = parse_dt(cast(str | None, row["created_at"]))
        if created_at is None:
            raise ValueError(
                "Stored result metadata is missing created_at "
                f"for job_id={job_id!r} run_id={run_id!r}"
            )
        return ResultMetadata(
            job_id=str(row["job_id"]),
            run_id=str(row["run_id"]),
            status=str(row["status"]),
            record_count=cast(int | None, row["record_count"]),
            file_path=str(row["file_path"]),
            created_at=created_at,
        )

    async def delete_results(self, job_id: str) -> None:
        """Delete all job results with retryable filesystem-first ordering."""
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(JOB_RESULTS_DELETE_QUERY, (job_id,))
            rows = cast(list[Mapping[str, Any]], await cursor.fetchall())
            await self._delete_files_then_ids(db, rows)

    async def delete_result(self, job_id: str, run_id: str) -> bool:
        """Delete one run artifact, including an unindexed browser-debug directory."""
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                """SELECT id, file_path
                   FROM results_meta
                   WHERE job_id = ? AND run_id = ?""",
                (job_id, run_id),
            )
            rows = cast(list[Mapping[str, Any]], await cursor.fetchall())
            if await self._delete_files_then_ids(db, rows):
                return True

        project, job_name = await self._job_lookup(job_id)
        run_dir = self._checked_result_dir(
            str(safe_join(self._results_dir, project, job_name, run_id))
        )
        existed = run_dir.is_dir() and not run_dir.is_symlink()
        await cleanup_safe_to_thread(remove_directories, [run_dir])
        return existed

    @staticmethod
    def _record_artifact_failure(
        state: _ReconciliationState,
        row: Mapping[str, Any],
        kind: ResultArtifactFailureKind,
        error_type: str | None = None,
    ) -> None:
        if kind is ResultArtifactFailureKind.missing:
            state.missing_result_files += 1
        elif kind is ResultArtifactFailureKind.corrupt:
            state.corrupt_result_files += 1
        elif kind is ResultArtifactFailureKind.unreadable:
            state.unreadable_result_files += 1
        else:
            state.unsafe_metadata_paths += 1
        state.artifact_failures.append(
            ResultArtifactFailure(
                job_id=str(row["job_id"])[:128],
                run_id=str(row["run_id"])[:128],
                kind=kind,
                error_type=error_type,
            )
        )

    def _validate_metadata_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        state: _ReconciliationState,
    ) -> set[Path]:
        referenced: set[Path] = set()
        for row in rows:
            state.metadata_rows_inspected += 1
            try:
                run_dir = self._checked_result_dir(str(row["file_path"]))
            except (OSError, ValueError) as exc:
                self._record_artifact_failure(
                    state,
                    row,
                    ResultArtifactFailureKind.unsafe,
                    type(exc).__name__,
                )
                continue
            referenced.add(run_dir)
            result_path = run_dir / "results.json"
            try:
                run_mode = run_dir.lstat().st_mode
                result_mode = result_path.lstat().st_mode
            except FileNotFoundError as exc:
                self._record_artifact_failure(
                    state,
                    row,
                    ResultArtifactFailureKind.missing,
                    type(exc).__name__,
                )
                continue
            except OSError as exc:
                self._record_artifact_failure(
                    state,
                    row,
                    ResultArtifactFailureKind.unreadable,
                    type(exc).__name__,
                )
                continue
            if not stat.S_ISDIR(run_mode) or not stat.S_ISREG(result_mode):
                self._record_artifact_failure(
                    state,
                    row,
                    ResultArtifactFailureKind.unsafe,
                )
                continue
            try:
                read_json_file_no_follow(result_path)
            except FileNotFoundError as exc:
                self._record_artifact_failure(
                    state,
                    row,
                    ResultArtifactFailureKind.missing,
                    type(exc).__name__,
                )
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._record_artifact_failure(
                    state,
                    row,
                    ResultArtifactFailureKind.corrupt,
                    type(exc).__name__,
                )
            except OSError as exc:
                self._record_artifact_failure(
                    state,
                    row,
                    ResultArtifactFailureKind.unreadable,
                    type(exc).__name__,
                )
            else:
                state.valid_artifacts += 1
        return referenced

    @staticmethod
    def _directory_entries(
        directory: Path,
    ) -> list[tuple[str, Path, os.stat_result]]:
        """List one directory through a non-following descriptor."""

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
        try:
            with os.scandir(descriptor) as entries:
                result = [
                    (entry.name, directory / entry.name, entry.stat(follow_symlinks=False))
                    for entry in entries
                ]
        finally:
            os.close(descriptor)
        result.sort(key=lambda item: item[0])
        return result

    @staticmethod
    def _tree_snapshot(run_dir: Path) -> tuple[int, float, list[tuple[Path, int, float]]]:
        """Return regular bytes, newest mtime, and known atomic temps, non-following."""

        root_stat = run_dir.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise ValueError("run candidate is not a regular directory")
        total_bytes = 0
        newest_mtime = root_stat.st_mtime
        temporary_files: list[tuple[Path, int, float]] = []
        pending = [run_dir]
        while pending:
            directory = pending.pop()
            for entry_name, entry_path, entry_stat in LocalResultStore._directory_entries(
                directory
            ):
                newest_mtime = max(newest_mtime, entry_stat.st_mtime)
                mode = entry_stat.st_mode
                if stat.S_ISLNK(mode):
                    continue
                if stat.S_ISDIR(mode):
                    pending.append(entry_path)
                elif stat.S_ISREG(mode):
                    total_bytes += entry_stat.st_size
                    if _ATOMIC_TEMP_PATTERN.fullmatch(entry_name):
                        temporary_files.append(
                            (entry_path, entry_stat.st_size, entry_stat.st_mtime)
                        )
        temporary_files.sort(key=lambda item: str(item[0]))
        return total_bytes, newest_mtime, temporary_files

    def _scan_artifacts(
        self,
        rows: Sequence[Mapping[str, Any]],
        cutoff_timestamp: float,
    ) -> tuple[_ReconciliationState, list[_RemovalCandidate], list[_RemovalCandidate]]:
        state = _ReconciliationState()
        referenced = self._validate_metadata_rows(rows, state)
        run_candidates: list[_RemovalCandidate] = []
        temp_candidates: list[_RemovalCandidate] = []
        root = self._results_dir.resolve(strict=False)
        try:
            project_entries = self._directory_entries(root)
        except FileNotFoundError:
            return state, run_candidates, temp_candidates
        except OSError as exc:
            state.operation_failures.append(
                ReconciliationOperationFailure("scan_root", ".", type(exc).__name__)
            )
            return state, run_candidates, temp_candidates

        for project_name, project_path, project_stat in project_entries:
            try:
                if not stat.S_ISDIR(project_stat.st_mode) or stat.S_ISLNK(project_stat.st_mode):
                    state.malformed_entries_ignored += 1
                    continue
                job_entries = self._directory_entries(project_path)
            except OSError as exc:
                state.operation_failures.append(
                    ReconciliationOperationFailure(
                        "scan_project", project_name[:80], type(exc).__name__
                    )
                )
                continue
            for job_name, job_path, job_stat in job_entries:
                project_job = f"{project_name[:80]}/{job_name[:80]}"
                try:
                    if not stat.S_ISDIR(job_stat.st_mode) or stat.S_ISLNK(job_stat.st_mode):
                        state.malformed_entries_ignored += 1
                        continue
                    run_entries = self._directory_entries(job_path)
                except OSError as exc:
                    state.operation_failures.append(
                        ReconciliationOperationFailure("scan_job", project_job, type(exc).__name__)
                    )
                    continue
                for run_name, run_path, run_stat in run_entries:
                    identity = _RunIdentity(project_name, job_name, run_name)
                    try:
                        if not stat.S_ISDIR(run_stat.st_mode) or stat.S_ISLNK(run_stat.st_mode):
                            state.malformed_entries_ignored += 1
                            continue
                        run_dir = self._checked_result_dir(str(run_path))
                        total_bytes, newest_mtime, temporary_files = self._tree_snapshot(run_dir)
                    except (OSError, ValueError) as exc:
                        state.operation_failures.append(
                            ReconciliationOperationFailure(
                                "scan_run", identity.identifier, type(exc).__name__
                            )
                        )
                        continue
                    state.filesystem_run_directories_inspected += 1
                    is_referenced = run_dir in referenced
                    run_is_recent = newest_mtime >= cutoff_timestamp
                    orphan_is_eligible = not is_referenced and not run_is_recent
                    if not is_referenced:
                        if run_is_recent:
                            state.recent_candidates_skipped += 1
                        else:
                            state.orphan_candidates += 1
                            run_candidates.append(_RemovalCandidate(identity, run_dir, total_bytes))
                    for temp_path, temp_size, temp_mtime in temporary_files:
                        if temp_mtime >= cutoff_timestamp or run_is_recent:
                            state.recent_candidates_skipped += 1
                            continue
                        state.stale_temporary_candidates += 1
                        if not orphan_is_eligible:
                            temp_candidates.append(
                                _RemovalCandidate(identity, temp_path, temp_size)
                            )
        return state, run_candidates, temp_candidates

    async def _active(self, identity: _RunIdentity) -> bool:
        if self._active_run_lookup is None:
            return False
        return await self._active_run_lookup(
            identity.project,
            identity.job_name,
            identity.run_id,
        )

    async def _metadata_references(self, candidate: _RemovalCandidate) -> bool:
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                """SELECT file_path FROM results_meta
                   WHERE project = ? AND run_id = ?""",
                (candidate.identity.project, candidate.identity.run_id),
            )
            rows = cast(list[Mapping[str, Any]], await cursor.fetchall())
        for row in rows:
            try:
                if self._checked_result_dir(str(row["file_path"])) == candidate.path:
                    return True
            except (OSError, ValueError):
                continue
        return False

    def _remove_run_candidate(
        self,
        candidate: _RemovalCandidate,
        cutoff_timestamp: float,
    ) -> tuple[str, int]:
        path = self._checked_result_dir(str(candidate.path))
        try:
            size, newest_mtime, _temporary_files = self._tree_snapshot(path)
        except FileNotFoundError:
            return "missing", 0
        if newest_mtime >= cutoff_timestamp:
            return "recent", 0
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("recursive removal is not symlink-safe")
        shutil.rmtree(path)
        return "removed", size

    def _remove_temp_candidate(
        self,
        candidate: _RemovalCandidate,
        cutoff_timestamp: float,
    ) -> tuple[str, int]:
        run_dir = self._checked_result_dir(
            str(
                self._results_dir
                / candidate.identity.project
                / candidate.identity.job_name
                / candidate.identity.run_id
            )
        )
        try:
            _size, newest_mtime, _temporary_files = self._tree_snapshot(run_dir)
            relative = candidate.path.relative_to(run_dir)
            if not relative.parts or ".." in relative.parts:
                raise ValueError("unsafe temporary path")
            current = run_dir
            for part in relative.parts[:-1]:
                current = current / part
                mode = current.lstat().st_mode
                if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                    raise ValueError("symlinked temporary parent")
            temp_stat = candidate.path.lstat()
        except FileNotFoundError:
            return "missing", 0
        if (
            not _ATOMIC_TEMP_PATTERN.fullmatch(candidate.path.name)
            or not stat.S_ISREG(temp_stat.st_mode)
            or stat.S_ISLNK(temp_stat.st_mode)
        ):
            raise ValueError("unsafe temporary candidate")
        if newest_mtime >= cutoff_timestamp or temp_stat.st_mtime >= cutoff_timestamp:
            return "recent", 0
        candidate.path.unlink()
        return "removed", temp_stat.st_size

    async def reconcile_artifacts(
        self,
        *,
        grace_seconds: int,
        dry_run: bool,
        now: datetime | None = None,
    ) -> ResultReconciliationReport:
        """Validate metadata and remove only stale, unowned, contained artifacts."""

        if grace_seconds < 1:
            raise ValueError("grace_seconds must be at least 1")
        observed_at = now or utc_now()
        cutoff_timestamp = (observed_at - timedelta(seconds=grace_seconds)).timestamp()
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                "SELECT job_id, run_id, file_path FROM results_meta ORDER BY id"
            )
            rows = cast(list[Mapping[str, Any]], await cursor.fetchall())
        state, run_candidates, temp_candidates = await cleanup_safe_to_thread(
            self._scan_artifacts,
            rows,
            cutoff_timestamp,
        )

        async def active_or_failed(candidate: _RemovalCandidate, action: str) -> bool:
            try:
                return await self._active(candidate.identity)
            except Exception as exc:
                state.operation_failures.append(
                    ReconciliationOperationFailure(
                        action, candidate.identity.identifier, type(exc).__name__
                    )
                )
                return True

        for candidate in run_candidates:
            if await active_or_failed(candidate, "check_active_run"):
                state.active_run_candidates_skipped += 1
                continue
            state.directories_would_remove += 1
            if dry_run:
                continue
            if await self._metadata_references(candidate):
                state.metadata_race_candidates_skipped += 1
                continue
            if await active_or_failed(candidate, "recheck_active_run"):
                state.active_run_race_candidates_skipped += 1
                continue
            try:
                outcome, removed_bytes = await cleanup_safe_to_thread(
                    self._remove_run_candidate,
                    candidate,
                    cutoff_timestamp,
                )
            except Exception as exc:
                state.operation_failures.append(
                    ReconciliationOperationFailure(
                        "remove_run", candidate.identity.identifier, type(exc).__name__
                    )
                )
                continue
            if outcome == "removed":
                state.directories_removed += 1
                state.removed_bytes += removed_bytes
            elif outcome == "recent":
                state.recent_candidates_skipped += 1

        for candidate in temp_candidates:
            if await active_or_failed(candidate, "check_active_temp"):
                state.active_run_candidates_skipped += 1
                continue
            state.files_would_remove += 1
            if dry_run:
                continue
            if await active_or_failed(candidate, "recheck_active_temp"):
                state.active_run_race_candidates_skipped += 1
                continue
            try:
                outcome, removed_bytes = await cleanup_safe_to_thread(
                    self._remove_temp_candidate,
                    candidate,
                    cutoff_timestamp,
                )
            except Exception as exc:
                state.operation_failures.append(
                    ReconciliationOperationFailure(
                        "remove_temp", candidate.identity.identifier, type(exc).__name__
                    )
                )
                continue
            if outcome == "removed":
                state.files_removed += 1
                state.removed_bytes += removed_bytes
            elif outcome == "recent":
                state.recent_candidates_skipped += 1
        return state.report(dry_run=dry_run)

    async def delete_expired(self, retention_days: int) -> int:
        """Delete results older than *retention_days*. Returns count deleted."""
        cutoff = (utc_now() - timedelta(days=retention_days)).isoformat()
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(EXPIRED_RESULTS_QUERY, (cutoff,))
            rows = cast(list[Mapping[str, Any]], await cursor.fetchall())
            return await self._delete_by_ids(db, rows)

    async def prune_excess_per_job(self, max_results_per_job: int) -> int:
        """Delete result runs exceeding the per-job retention limit."""
        async with get_db("results_meta.db") as db:
            cursor = await db.execute(
                EXCESS_RESULTS_PER_JOB_QUERY,
                (max_results_per_job,),
            )
            rows = cast(list[Mapping[str, Any]], await cursor.fetchall())
            return await self._delete_by_ids(db, rows)
